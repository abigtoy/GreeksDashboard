# 期权监控看板（VolTrading）— 重构架构与实施规范

> 定位：CTP 实时行情/持仓 + 本地结算单成本对齐 + 内存计算引擎 + 预聚合只读快照 + 前端纯渲染
>
> 核心原则：单进程双线程解耦、内存快照读写分离（无锁只读）、后端算好前端只画、数据单向流动。

---

## 一、系统总体架构与数据流

```
[ 外部数据源 ]
  ├── CTP 行情接口 (MD API) ──► 内存 Tick 字典
  ├── CTP 交易接口 (TD API) ──► 内存 Position 列表
  └── 本地结算单 (JSON)       ──► 真实开仓加权成本字典 settlement_cost_dict
                 │
                 ▼
[ 内存计算 Worker 线程 (每秒定时) ]
  1. 代码归一化与标的映射 (CFFEX IO→IF, MO→IM, HO→IH)
  2. adjust_price 测算（PCP/盘口/mid_price 三级）
  3. implied_vol_bisection：市场报价反推 IV（如无可用值则跳过期权 Greeks）
  4. 标准 Black-76 单合约 Greeks 计算（含期货特判分支）
  5. Cash Greeks 换算（统一量纲）
  6. PnL 三口径 (pnl_daily / pnl_today / pnl_history)
  7. 阈值判断与样式打标 (delta_tag / gamma_tag / pnl_tag)
  8. 树形层级预聚合：L1 品种 -> L2 月份 -> L3 合约
                 │
                 ▼ 原子替换 (Atomic Swap)
[ 全局内存只读快照 (_dashboard_snapshot，不可变对象）]
                 │
                 ▼ 纯内存读取 (<2ms, 无锁零竞争)
[ Flask Web API ]
  └── GET /api/dashboard ──► 完整快照 JSON
  └── GET /api/ctp/status
  └── POST /api/ctp/connect
  └── POST /api/ctp/disconnect
                 │
                 ▼
[ 前端纯渲染引擎 (dashboard11.js) ]
  - 维护纯 UI 状态 (展开折叠 Set、本地排序、筛选)
  - 增量/全量 DOM 更新，无任何业务运算、无正则解析、无 Greeks 汇总
```

---

## 二、结算单加权开仓成本加载

### 2.1 settlement_cost_dict 格式

```python
# Key: "{symbol}_{'多'|'空'}"
# Value: VWAP 加权开仓成本
settlement_cost_dict = {
    "IF2609_多": 4210.0,
    "IO2609-C-4000_空": 38.5,
    "IC2609_空": 8048.6,
}
```

### 2.2 双策略加载（严禁单条直接覆写）

```python
from collections import defaultdict

def load_settlement_cost(settlement_json: dict) -> dict:
    settlement_dict = {}

    # ========== 策略1: 嗅探券商预计算的汇总均价 ==========
    summary_list = (
        settlement_json.get('positions')
        or settlement_json.get('positions_summary')
        or []
    )
    for item in summary_list:
        sym = item.get('instrument') or item.get('symbol') or item.get('instrument_id')
        raw_dir = item.get('bs') or item.get('direction') or item.get('side') or ''
        direction = '多' if raw_dir in ('买', '多', 'B', '1', 'Buy') else '空'
        vwap = (
            item.get('avg_open_price') or item.get('open_price_avg')
            or item.get('vwap') or item.get('open_price') or item.get('price')
        )
        if sym and vwap and float(vwap) > 0:
            settlement_dict[f"{sym}_{direction}"] = round(float(vwap), 4)

    # ========== 策略2: 逐笔明细加权自算兜底 ==========
    details = settlement_json.get('positions_detail') or []
    detail_calc = defaultdict(lambda: {"total_cost": 0.0, "total_vol": 0})

    for item in details:
        sym = item.get('instrument') or item.get('symbol') or item.get('instrument_id')
        if not sym:
            continue
        raw_dir = item.get('bs') or item.get('direction') or item.get('side') or ''
        direction = '多' if raw_dir in ('买', '多', 'B', '1', 'Buy') else '空'
        price = item.get('open_price') or item.get('price') or item.get('trade_price') or 0.0
        vol = item.get('volume') or item.get('vol') or item.get('qty') or 0
        try:
            p, v = float(price), int(vol)
            if p > 0 and v > 0:
                key = f"{sym}_{direction}"
                detail_calc[key]["total_cost"] += p * v
                detail_calc[key]["total_vol"] += v
        except (ValueError, TypeError):
            continue

    for key, data in detail_calc.items():
        if data["total_vol"] > 0:
            settlement_dict[key] = round(data["total_cost"] / data["total_vol"], 4)

    return settlement_dict
```

**方向映射：** `'买'/'多'/'B'/'1'/'Buy'` → `'多'`；其余 → `'空'`

---

## 三、核心计算与业务规则

### 3.1 标的期货与期权代码归一化映射

| 品种 | CFFEX 映射 | 期货乘数 | 期权乘数 |
|------|-----------|---------|---------|
| 沪深300 | IO* → IF* | IF=300元/点 | 100元/点 |
| 中证1000 | MO* → IM* | IM=200元/点 | 100元/点 |
| 上证50 | HO* → IH* | IH=300元/点 | 100元/点 |
| 商品（铜等） | 无需映射 | 各品种固定乘数 | 各品种固定乘数 |

**商品乘数速查：** cu=5, au=1000, ag=15, ru=10, m=10, jm=60, rb=10, i=100

### 3.2 adjust_price 界定（盘中实时，与结算单完全解耦）

1. **深度实值（ITM）**：利用 PCP 平价公式与 OTM 腿时间价值反推
2. **深度虚值（OTM）/ 盘口宽价差**：根据微观盘口挂单量与动态价差过滤
3. **正常流动性合约**：mid_price 或最新成交价

# ==================== 3.3 标准 Black-76 与 IV 反推纯函数 ====================
import math
from scipy.stats import norm

def black76(IV: float, F: float, K: float, T: float, r: float = 0.02, cp: int = 1):
    """
    Black-76 模型单张 Greeks 计算。
    cp: 1=Call, -1=Put
    T: 最小截断 0.5/250 防止到期日除零
    """
    T = max(T, 0.5 / 250.0)
    v = IV / 100.0 if IV > 1.0 else max(IV, 0.001)  # 防呆：自动兼容百分比与小数
    sqrtT = math.sqrt(T)

    d1 = (math.log(F / K) + 0.5 * v * v * T) / (v * sqrtT)
    d2 = d1 - v * sqrtT

    exp_rt = math.exp(-r * T)
    delta = cp * exp_rt * norm.cdf(cp * d1)
    gamma = exp_rt * norm.pdf(d1) / (F * v * sqrtT)
    vega  = F * exp_rt * norm.pdf(d1) * sqrtT * 0.01  # 标的每变动 1 Vol (0.01)

    # 标准 Black-76 Theta (日历日衰减):
    term1 = - (F * exp_rt * norm.pdf(d1) * v) / (2.0 * sqrtT)
    term2 = - cp * r * F * exp_rt * norm.cdf(cp * d1)
    term3 =   cp * r * K * exp_rt * norm.cdf(cp * d2)
    theta = (term1 + term2 + term3) / 365.0

    return {"delta": delta, "gamma": gamma, "vega": vega, "theta": theta}


def implied_vol_bisection(price: float, F: float, K: float, T: float,
                           r: float = 0.02, cp: int = 1, tol: float = 0.0001) -> float:
    """二分法反推 IV，返回小数形式（如 0.165）。收敛失败时返回 0.20 作为兜底。"""
    if price <= 0 or F <= 0 or K <= 0:
        return 0.20
    v_low, v_high = 0.001, 5.0
    for _ in range(30):
        v_mid = (v_low + v_high) * 0.5
        p = cp * math.exp(-r * T) * (
            F * norm.cdf(cp * ((math.log(F / K) + 0.5 * v_mid * v_mid * T) / (v_mid * math.sqrt(T))))
            - K * norm.cdf(cp * ((math.log(F / K) + 0.5 * v_mid * v_mid * T) / (v_mid * math.sqrt(T)) - v_mid * math.sqrt(T)))
        )
        if abs(p - price) < tol:
            return v_mid
        if p < price:
            v_low = v_mid
        else:
            v_high = v_mid
    return (v_low + v_high) * 0.5


# ==================== 3.4 持仓 Greeks 与 Cash Greeks（兼容期货与期权） ====================
def calc_greeks(tick: dict, position: dict, contract: dict) -> dict:
    direction_sign = 1 if position['direction'] in ('long', '多') else -1
    vol = position['volume']
    size = contract['size']
    F = tick['underlying_price']

    # --- 分支 1: 期货合约 ---
    if contract.get('product_type') == 'FUTURES' or not contract.get('option_type'):
        pos_delta = vol * direction_sign * 1.0
        return {
            "delta": 1.0, "gamma": 0.0, "vega": 0.0, "theta": 0.0,
            "pos_delta": pos_delta, "pos_gamma": 0.0, "pos_vega": 0.0, "pos_theta": 0.0,
            "deltacash": round(pos_delta * F * size),
            "gammacash": 0, "vegacash": 0, "thetacash": 0
        }

    # --- 分支 2: 期权合约 ---
    g = black76(
        IV=tick['iv'],
        F=F,
        K=contract['strike'],
        T=contract['ttm'],
        cp=1 if contract['option_type'] in ('C', 'CALL', 'call') else -1
    )

    pos_delta = g['delta'] * direction_sign * vol
    pos_gamma = g['gamma'] * direction_sign * vol
    pos_vega  = g['vega']  * direction_sign * vol
    # 空头 Theta 为正现金流获利，多头为负损耗
    pos_theta = -g['theta'] * vol if direction_sign == -1 else g['theta'] * vol

    return {
        "delta": g['delta'], "gamma": g['gamma'], "vega": g['vega'], "theta": g['theta'],
        "pos_delta": pos_delta, "pos_gamma": pos_gamma, "pos_vega": pos_vega, "pos_theta": pos_theta,
        "deltacash": round(pos_delta * F * size),
        "gammacash": round(pos_gamma * (F ** 2) * 0.01 * size),  # 1% 波动 Cash Gamma
        "vegacash":  round(pos_vega * size),
        "thetacash": round(pos_theta * size)
    }


# ==================== 3.5 PnL 三口径计算 ====================
def calc_pnl(position: dict, tick: dict, settlement_dict: dict,
             yesterday_snapshot: dict = None) -> dict:
    sym = position['symbol'].split('.')[0]
    direction_str = '多' if position['direction'] in ('long', '多') else '空'
    direction_sign = 1 if direction_str == '多' else -1
    vol = position['volume']
    size = position['size']

    # 1. 真实开仓成本
    cost_price = settlement_dict.get(f"{sym}_{direction_str}", position.get('price', 0.0))

    # 2. 历史累计浮盈 (adjust_price - 真实开仓成本)
    pnl_history = direction_sign * (tick['adjust_price'] - cost_price) * vol * size

    # 3. 当日盯市盈亏 (last_price - 昨日结算价)
    settle_price = float(position.get('price') or 0.0)
    pnl_daily = direction_sign * (tick['last_price'] - settle_price) * vol * size

    # 4. 当日盈亏 (adjust_price - 昨日收盘调整价基准)
    base_today = cost_price
    if yesterday_snapshot:
        prev_item = yesterday_snapshot.get(f"{sym}_{direction_str}", {})
        base_today = prev_item.get('adjust_price', cost_price)
    pnl_today = direction_sign * (tick['adjust_price'] - base_today) * vol * size

    return {
        "pnl_daily":   round(pnl_daily,   2),
        "pnl_today":   round(pnl_today,   2),
        "pnl_history": round(pnl_history, 2),
    }
```

### 3.6 树形聚合规则

```
L3 合约 → 归属 L2 月份（IF_2609）
L2 月份 → 归属 L1 品种（IF）

聚合指标：
  volume      = Σ|pos.volume|（绝对值，反映物理负荷）
  Greeks/PnL  = Σ 带符号代数加总（自然对冲，净方向由正负呈现）
```

---

## 四、数据结构

### 4.1 快照 JSON Schema（GET /api/dashboard 契约）

```json
{
  "status": "connected",
  "last_update": "14:25:31",
  "summary": {
    "total_deltacash":   125000,
    "total_gammacash":   -45000,
    "total_vegacash":    -12000,
    "total_thetacash":   8500,
    "total_pnl_daily":   3200,
    "total_pnl_today":   1500,
    "total_pnl_history": 86000,
    "position_count":    85
  },
  "tree": [
    {
      "key": "IF",
      "name": "沪深300（IF）",
      "type": "L1_PRODUCT",
      "metrics": {
        "volume": 85,
        "deltacash": 125000,
        "deltacash_tag": "green",
        "gammacash": -45000,
        "gammacash_tag": "yellow",
        "vegacash": -12000,
        "thetacash": 8500,
        "pnl_daily": 3200,
        "pnl_today": 1500,
        "pnl_history": 86000,
        "pnl_tag": "green"
      },
      "children": [
        {
          "key": "IF_2609",
          "name": "2609月份",
          "type": "L2_MONTH",
          "metrics": {
            "volume": 40,
            "deltacash": 16000,
            "deltacash_tag": "green",
            "gammacash": -8000,
            "gammacash_tag": "yellow",
            "vegacash": -2200,
            "thetacash": 1100,
            "pnl_daily": 600,
            "pnl_today": 200,
            "pnl_history": 21000,
            "pnl_tag": "green"
          },
          "children": [
            {
              "key": "IF2609_多",
              "symbol": "IF2609",
              "direction": "多",
              "direction_raw": "long",
              "volume": 2,
              "last_price": 4230.0,
              "adjust_price": 4230.0,
              "open_price": 4210.0,
              "underlying_price": 4230.0,
              "iv": null,
              "delta": 1.0,
              "gamma": 0.0,
              "vega": 0.0,
              "theta": 0.0,
              "deltacash": 2538000,
              "gammacash": 0,
              "vegacash": 0,
              "thetacash": 0,
              "days_to_expiry": null,
              "itm": false,
              "pnl_daily": 800,
              "pnl_today": 200,
              "pnl_history": 1200,
              "delta_tag": "green",
              "gamma_tag": "green",
              "pnl_tag": "green"
            },
            {
              "key": "IO2609-C-4000_空",
              "symbol": "IO2609-C-4000",
              "direction": "空",
              "direction_raw": "short",
              "volume": 20,
              "last_price": 45.2,
              "adjust_price": 44.8,
              "open_price": 38.5,
              "underlying_price": 4230.0,
              "iv": 16.5,
              "delta": -0.35,
              "gamma": -0.0012,
              "vega": 0.023,
              "theta": -0.008,
              "deltacash": -2960100,
              "gammacash": -40536,
              "vegacash": -460,
              "thetacash": 160,
              "days_to_expiry": 25,
              "itm": false,
              "pnl_daily": -200,
              "pnl_today": -80,
              "pnl_history": 8400,
              "delta_tag": "yellow",
              "gamma_tag": "red",
              "pnl_tag": "green"
            }
          ]
        }
      ]
    }
  ]
}
```

---

## 五、前端交互设计

### 5.1 最小 UI 状态

```javascript
const UIState = {
  expandedGroups: new Set(),   // 展开的 L1 key，如 Set(['IF', 'CU'])
  expandedMonths: new Set(),  // 展开的 L2 key，如 Set(['IF_2609'])
  sortCol: 'pnl_history',     // 当前排序列
  sortAsc: false,              // 升序/降序
  filter: '',                 // 品种筛选关键字
  colConfig: {}                // 本地持久化的列隐藏/顺序/小数位配置
};
```

### 5.2 渲染机制

1. **树形遍历**：根据 tree 数组递归渲染 L1/L2/L3 行，折叠状态由 `UIState.expanded*` 直接映射为 CSS `.collapsed`
2. **纯前端视图排序**：点击列头直接对 L1 数组及 children 做内存级排序，**<1ms，禁止发带排序参数的后端请求**
3. **样式注入**：直接读取数据节点自带的 `*_tag` 字段，追加对应 CSS class（如 `.delta-yellow`, `.pnl-green`）

### 5.3 CSS 样式映射

```css
.delta-green, .gamma-green, .pnl-green { color: #4ade80; }
.delta-yellow, .gamma-yellow            { color: #fbbf24; }
.delta-red, .gamma-red, .pnl-red        { color: #f87171; }
.pnl-yellow                                { color: #fbbf24; }
```

---

## 六、API 端点

| 路由 | 方法 | 功能 |
|------|------|------|
| `/api/dashboard` | GET | 获取完整聚合快照数据 |
| `/api/ctp/status` | GET | CTP 连接状态心跳 |
| `/api/ctp/connect` | POST | 启动 CTP 引擎 |
| `/api/ctp/disconnect` | POST | 安全关闭连接 |

---

## 七、文件结构与实施计划

```
C:/Quant_2026/期货执行策略/vnpy接口封装/
├── pricing.py              # Phase 1: Black-76 + implied_vol_bisection 纯函数
├── settlement.py           # Phase 1: 结算单双策略加载
├── risk_engine.py          # Phase 1: Greeks + PnL + 树形聚合 + 标签
├── vnpy_api_server.py      # Phase 2: Worker 线程 + 原子快照 + Flask 路由
├── static/
│   └── dashboard11.js      # Phase 3: 极简渲染器
└── templates/
    └── dashboard.html       # Phase 3: JS 引用改为 dashboard11.js
```

**Phase 1（纯函数模块）：**
1. `pricing.py` — Black-76 Greeks 纯函数，无任何外部依赖
2. `settlement.py` — 结算单双策略加载
3. `risk_engine.py` — PnL 三口径 + Cash Greeks + 树形聚合 + 标签注入

**Phase 2（服务集成）：**
4. `vnpy_api_server.py` — Worker 线程 + 原子快照 + Flask 路由（保持现有 CTP 连接逻辑不变）

**Phase 3（前端）：**
5. `dashboard11.js` — 极简渲染，替换现有 dashboard8.js

---

## 八、红线要求（交付 AI Agent 时必须遵守）

1. **红线一（禁止修改库源码）**：严禁修改 `C:\veighna_studio\site-packages`。持仓开仓成本全面改由本地结算单补全。
2. **红线二（纯函数解耦）**：`pricing.py` 与 `risk_engine.py` 严禁引入任何 Flask 或 CTP 模块，必须是无状态纯函数。
3. **红线三（严禁前端碰业务）**：dashboard11.js 严禁正则解析合约代码，严禁计算 Greeks/汇总，所有字段由后端预聚合。
4. **红线四（异步读写分离）**：Worker 生成完整树后通过单个指针赋值（Atomic Swap）更新快照。Flask `/api/dashboard` 禁止在请求上下文中做任何循环迭代计算。

---

## 九、阈值打标规则

```python
THRESHOLDS = {
    "delta":   {"warning": 50000,  "danger": 100000},
    "gamma":   {"warning": 30000,  "danger": 60000},
    "pnl":     {"danger": -50000},
}

def tag_delta(dc):
    a = abs(dc)
    if a >= THRESHOLDS["delta"]["danger"]:  return "red"
    if a >= THRESHOLDS["delta"]["warning"]: return "yellow"
    return "green"

def tag_gamma(gc):
    a = abs(gc)
    if a >= THRESHOLDS["gamma"]["danger"]:  return "red"
    if a >= THRESHOLDS["gamma"]["warning"]: return "yellow"
    return "green"

def tag_pnl(pnl):
    if pnl < THRESHOLDS["pnl"]["danger"]: return "red"
    if pnl < 0: return "yellow"
    return "green"
```

---

## 附录：结算单数据链路修复（2026-09-14）

### A.1 根因：TradingDate vs TradingDay 字段名错误

**问题现象：** 无论查询哪天的历史结算单，CTP 接口始终返回当天结算单内容。

**根因：** vnpy CTP gateway 发送的请求字段名与 CTP struct 不匹配：

```
vnpy gateway 发送（错误）:
  {"BrokerID": "...", "InvestorID": "...", "TradingDate": "20260909"}

CTP struct CThostFtdcQrySettlementInfoField 实际字段名:
  {"BrokerID": "...", "InvestorID": "...", "TradingDay": "20260909"}
```

字段名对不上 → CTP 忽略未知字段 → 默认查当天结算单。

**修复位置：** `C:/veighna_studio/Lib/site-packages/vnpy_ctp/gateway/ctp_gateway.py` L544

```python
# 改前（错误）
ctp_req: dict = {
    "BrokerID": self.brokerid,
    "InvestorID": self.userid,
    "TradingDate": getattr(self, 'trading_day', '') or ""  # ← 字段名错
}

# 改后（正确）
ctp_req: dict = {
    "BrokerID": self.brokerid,
    "InvestorID": self.userid,
    "TradingDay": getattr(self, "trading_day", "") or ""   # ← 字段名对
}
```

**验证方法：** 上线后连接 CTP，POST 指定历史日期（如 `{"trading_date": "20260911"}`），
观察返回的结算单文件内容日期是否与请求日期一致。

### A.2 结算单数据链路架构

```
[CTP 接口]
  └── reqQrySettlementInfo(TradingDay=YYYYMMDD)
        │
        ▼ 文件流（ctp_gateway.py 分包接收）
  ctp_settlement_{YYYYMMDD}.txt  （原始文本，追加写）
        │
        ▼
[save_settlement.py]  定时/手动触发
        │
        ▼
[parse_settlement_full.py]  解析 txt → full_{YYYYMMDD}.json
        │
        ▼
[settlement.py]  SettlementManager 加载 full_*.json
        │
        ▼ settlement_cost_dict = {"symbol_多": vwap, "symbol_空": vwap, ...}
        │
        ▼
[risk_engine.py]  用 settlement_cost_dict 替换 position.price（持仓均价）
        │
        ▼ 正确 Greeks（基于真实开仓成本）
```

### A.3 SettlementManager 类设计

```python
class SettlementManager:
    """结算单管理器：增量扫描 + 内存索引"""

    def __init__(self, settlement_dir: str):
        self.settlement_dir = settlement_dir
        self._cost_cache: dict[str, float] = {}  # "symbol_多" → VWAP

    def load_today(self):
        """加载当日结算单（连接 CTP 后自动触发）"""
        today = datetime.now().strftime("%Y%m%d")
        return self._load_file(today)

    def load_date(self, trading_date: str) -> bool:
        """加载指定日期结算单（YYYYMMDD）"""
        return self._load_file(trading_date)

    def _load_file(self, date: str) -> bool:
        """扫描目录找 full_{date}.json，有则解析并更新缓存"""
        # 1. 扫目录找 full_{date}.json
        # 2. parse_settlement_full.py 格式解析
        # 3. 策略1: 嗅探 positions_summary 汇总均价
        # 4. 策略2: 逐笔明细加权自算兜底
        # 5. 更新 self._cost_cache
        # 6. 返回是否成功

    def get_cost(self, symbol: str, direction: str) -> float | None:
        """查询指定合约+方向的开仓成本，None 表示无结算单数据"""
        return self._cost_cache.get(f"{symbol}_{direction}")

    def get_all_costs(self) -> dict[str, float]:
        """返回完整成本字典（供 risk_engine 批量替换）"""
        return self._cost_cache.copy()
```

**Key 设计决策：**
- 内存只读缓存，无锁访问
- 双策略加载（汇总均价优先，逐笔明细兜底）
- 与 CTP 连接解耦：CTP 断线不影响已加载的结算单数据
- 增量扫描：只加载目录中存在的日期，不主动下载

### A.4 结算单注入 Greeks 计算的时机

```
risk_engine.py 的 _fetch_settlement() 现状：
 settlement_cost_dict = {}  ← 空字典，Greeks 永远用 position.price

改造后：
  def _fetch_settlement(self):
      sm = self.settlement_manager  # 全局单例
      return sm.get_all_costs()     # 有结算单数据则返回真实 VWAP，无则 {}
```

**注入逻辑：**
```python
cost = settlement_cost_dict.get(f"{sym}_{direction}")
if cost is not None:
    price = cost          # 有结算单 → 用真实 VWAP
else:
    price = pos["price"]  # 无结算单 → 降级为持仓均价（原有逻辑）
```

### A.5 待上线验证清单

- [ ] CTP 连上后，POST `/api/debug/trigger_settlement` 触发当天结算单下载
- [ ] 检查 `C:/qproj/结算单/ctp_settlement_{today}.txt` 是否为当日新内容
- [ ] 检查 `C:/qproj/结算单/full_{today}.json` 是否解析成功
- [ ] Greeks 看板 Delta/Gamma 数值是否与昨日结算单吻合
- [ ] 历史日期（如 20260911）查询是否返回正确日期的结算单

---

*文档版本：v5 | 交付级 | 2026-09-14*
