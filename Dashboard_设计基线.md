# VolTrading Dashboard — 设计基线（Baseline）

> **基线版本**：v1.5-baseline
> **建立日期**：2026-09-11
> **最后更新**：2026-09-18（PnL 基准可见性 + 成交账本落盘 + 今开仓基准 + 废弃「盯日盈亏」列）
> **状态**：v1.5 已落码并于 13:25 重启上线；待 2026-09-18 收盘出首份 `close_snapshot_20260918.json`，2026-09-19 起以昨收基准对表老系统
> **用途**：作为后续重构的唯一权威参考。本文档已吸收《Dashboard_重构设计.md》v4 的全部内容，并合并了旧版 `DESIGN.md` 中缺失的 IV 链路与 adjust_price 逻辑，同时标注了命名统一修正与已知缺口。
>
> **v1.5 更新说明（2026-09-18）**：
> - **废弃「盯日盈亏」`pnl_daily`**：口径与 `pnl_today` 重叠且全链路（calc_pnl / 列配置 / 前端 / 汇总卡 / 迁移工具）无独立消费方，整条删除。PnL 从三口径改为**两口径** `pnl_today` / `pnl_history`
> - **F1 基准可见性**：每条 L3 腿新增 `price_basis`（当日盈亏基准来源）与 `cost_basis`（开仓成本来源），聚合层新增 `summary.pnl_basis_counts`；Worker 出现非昨收基准腿时告警一次（§3.7）
> - **F2 成交账本落盘**：`快照/trade_ledger.json`（§3.8）。重启后按原值重放，当日已实现盈亏不再归零；`trading_day` 以 **CTP TradingDay** 为分区键，切日清零，本机 clock 不参与账务
> - **F3 方向枚举修正**：原实现用 `str(Direction.LONG)` 匹配 `('long','Long','B'…)`，永不命中 → 所有成交恒判 short、已实现盈亏符号全反。改为取 `.value`（`多`/`空`）先判买卖侧，再由 offset 推头寸方向
> - **F4 今开仓腿基准 = 开仓价**：账本加权开仓价 `today_open_cost` 进 `calc_pnl`，无昨结/昨收时以其为基准；昨结与今开并存的混合腿标 `+today_open_mixed`（单一基准拆不开今昨手数，留告警待核）
> - **平仓成本阶梯定稿**：平昨 昨结算 → 今开加权 → 结算单开仓均价；平今 今开加权 → 昨结算 → 结算单开仓均价；全无基准则记 0 并告警（§3.8）
> - **缺口 B7（P0，已修）**：`_register_trade_event` 取 `eng.event_engine` 恒抛 `AttributeError`，EVENT_TRADE 从未注册成功 → 已实现盈亏通道长期死路。改挂 `eng.main_engine.event_engine`，2026-09-18 13:25 重启后日志确认注册成功
>
> **v1.4 更新说明（2026-09-18）**：
> - 快照由「每交易日 N/A/P 三份 + 30min dirty 覆盖」简化为「**每交易日仅 15:00 收盘一份**」`close_snapshot_{TradingDay}.json`
> - 废弃同日 P>A>N 回退链与跨业务日找旧快照的回退（两者都曾让盘中价冒充昨收基准）
> - 新增收盘 Mark 取数口径：14:55–15:00 时间等差采样 `adjust_price` 做**算术平均**，**不用末成交价、不做成交量加权、重复样本不去重**（§三-B.2）
> - `pnl_today` 基准优先级定稿：T-1 收盘快照 Mark → T-1 结算价（仅整份快照缺失时）→ None（§3.7）
> - 取消样本数降级门槛：样本少亦照用，只有整份快照缺失才降级
> - 新增缺口 B4：期货腿无独立 Mark（等于 last_price），远月非主力有偏差，本版本裁决为暂不修
>
> **v1.3 更新说明（2026-09-17）**：
> - P0-1~10 / P1-1~12 / P2-1~4 共 26 条审查问题逐条修正，详见审查意见 `审查意见260917.md`
> - 3.7 PnL 公式重写（补全 direction_sign、守恒逻辑、已实现通道）
> - 3.5 标记 DEPRECATED（仅保留签名，删除正文）
> - trade_cache 账本模型重定义（分组键/去重键/OffsetFlag 归因）
> - 结算成本改为 positions_detail 单路径 VWAM（废弃 positions_summary）
> - 快照原子写 + 空仓规范落盘 + TradingDay 绑定
> - B1/B2 状态修正为"修复中/待数值验证"
>
> **v1.2 更新说明（2026-09-14 下午）**：
> - G2 结算单链路全面升级：SettlementManager（增量同步 + 日期标记 + CTP 下载）
> - CTP 字段名修复：`TradingDate` → `TradingDay`（ctp_gateway.py / vnpy_api_server.py）
> - 新增日期边界规则：20:00 前最新结算单=昨日，20:00 后=今日
> - 结算单目录清空（全是假数据），下次 CTP 连接时全量重拉
>
> **v1.1 更新说明（2026-09-14）**：
> - G1/G2/G3/G4/G5 全部标记为 FIXED
> - G3 从三级升为四级 adjust_price（新增 ITM PCP 平价 + 嵌套腿递归）
> - 新增 Greeks 口径铁律（§3.4.1）
> - 新增 cp_from_symbol 规则（§3.3）
> - 修正 gammacash 公式（确认乘数，去除冗余 0.01）
> - 统一 T 下限 0.5/365、r=0.02
> - 新增不掉线不 close 原则（§十一）

---

## 〇、资料来源

| 来源文件 | 版本/日期 | 采用内容 | 可信度 |
|---------|----------|---------|--------|
| `Dashboard_重构设计.md` | v4 / 2026-09-11 | 主体架构、红线、阈值、tree schema、API 端点、文件结构 | 高（主设计文档） |
| `vnpy接口封装_backup_20260910/DESIGN.md` | 2026-09-08 | IV 计算链路（adjust_price → bisection → tick['iv']）、adjust_price 三级规则、PnL 口径定义（历史三口径，v1.5 已收为两口径）、CFFEX 映射、合约乘数表 | 高（历史实现记录，含已验证修复） |
| `dashboard_v2/pricing.py` | 当前代码 | Black-76、`implied_vol_bisection`、`price_options_batch`、字段命名（`days_to_expiry`） | 中（实现与文档有偏差，以本文档修正后为准） |
| `dashboard_v2/risk_engine.py` | 当前代码 | `calc_greeks` / `calc_pnl` / `build_tree` / `calc_adjust_price` 签名与字段 | 中（含占位实现，待补全） |
| `dashboard_v2/settlement.py` | 当前代码 | `SettlementManager`（增量同步 + 日期标记 + CTP 下载）、`load_settlement_cost`（单路径VWAM） | 高（已实现） |
| `dashboard_v2/api_server.py` | 当前代码 | Worker 轮询、`_shared_state`、端点注册 | 中（engine 方法调用待修正，见缺口 G1） |
| `vnpy_engine.py` | 当前代码 | `VNPYEngine` 实际可用方法清单（`query_positions`/`query_tick`/`query_account` 等，**无** `is_connected`/`get_all_positions`/`connect`） | 高（接口事实） |

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
  2. adjust_price 测算（ITM/OTM/正常 三级，见 §3.2）
  3. implied_vol_bisection：用 adjust_price 反推 IV → 写入 tick['iv']
  4. 标准 Black-76 单合约 Greeks 计算（含期货特判分支）
  5. Cash Greeks 换算（统一量纲）
  6. PnL 两口径 (pnl_today / pnl_history)，各腿带 price_basis / cost_basis 标签（v1.5，§3.7）
  7. 阈值判断与样式打标 (delta_tag / gamma_tag / pnl_tag)
  8. 树形层级预聚合：L1 品种 -> L2 月份 -> L3 合约
  9. 成交回报账本：EVENT_TRADE → trade_ledger.json 落盘 + 已实现盈亏累加（v1.5，§3.8）
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

**核心原则**：单进程双线程解耦、内存快照读写分离（无锁只读）、后端算好前端只画、数据单向流动。

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

### 2.2 单路径 VWAM（positions_detail）

```python
from collections import defaultdict

def load_settlement_cost(settlement_json: dict) -> dict:
    settlement_dict = {}
    detail_calc = defaultdict(lambda: {"total_cost": 0.0, "total_vol": 0})

    # positions_detail 逐笔明细 VWAM 自算（唯一路径）
    details = settlement_json.get('positions_detail') or []
    for item in details:
        sym = (item.get('instrument') or '').strip()
        if not sym:
            continue

        raw_dir = str(item.get('bs') or '')
        direction = _parse_direction(raw_dir)   # 'long' / 'short' / None
        if direction is None:
            logger.warning(f"[结算单] 未知方向拒绝入账: bs={raw_dir!r}, sym={sym}")
            continue

        open_price = float(item.get('open_price') or 0.0)
        position   = int(item.get('position') or 0)
        if open_price <= 0 or position <= 0:
            continue

        key = f"{sym}_{direction}"
        detail_calc[key]["total_cost"] += open_price * position
        detail_calc[key]["total_vol"]  += position

    # VWAM 回填
    for key, data in detail_calc.items():
        if data["total_vol"] > 0:
            settlement_dict[key] = round(data["total_cost"] / data["total_vol"], 4)

    return settlement_dict
```

> **方向映射**：`'long'` / `'short'`（英文，与 `calc_pnl` 查找 key 格式一致）
>
> **未知方向拒绝入账**：方向字段无法映射时，该条记录拒绝入账并记录告警日志。

> ✅ **基线缺口 G2（已 FIXED）**：原 `load_settlement_sync` 扫 `parsed_*.json`，现升级为 `SettlementManager` 类，实现增量同步（补近30天缺漏 + 当天结算单自动下载）、日期标记（`settlement_meta.json`）、以及 CTP 下载→txt→`full_*.json` 全链路。详见 §二（新）。

### 2.3 SettlementManager — 增量同步架构

**触发时机：** 每次 CTP 连接成功时自动调用 `SettlementManager().sync()`

**日期边界规则：**
```
当前时间 < 20:00 → 最新有效结算单 = 昨日（结算单未生成）
当前时间 >= 20:00 → 最新有效结算单 = 今日（结算单已生成）
```

**数据流：**
```
[CTP 连接成功]
    │
    ▼
[SettlementManager.sync()]
    │
    ├─ 扫描 settlement_meta.json → 已有日期列表
    ├─ 对比近30个交易日 → 缺失日期列表
    │
    ├─ 补缺漏：逐日调用 CTP API → ctp_settlement_{date}.txt
    │              → 解析 → full_{date}.json → 入库
    │
    └─ 下载当天（20:00 后）：
           CTP API → ctp_settlement_{today}.txt
                  → 解析 → full_{today}.json → 入库
    │
    ▼
[更新 settlement_meta.json]
    { "latest": "20260913", "loaded": true }
    │
    ▼
[SettlementManager.load_costs_from_meta()]
    → 加载 latest 对应的 full_*.json
    → 写入 _cost_cache 内存字典
    │
    ▼
[risk_engine.py]
    settlement_cost_dict = sm.get_all_costs()
    → Greeks 计算使用真实 VWAP 替代持仓均价
```

**settlement_meta.json 格式：**
```json
{
  "latest": "20260913",
  "loaded": true
}
```

**SettlementManager 核心 API：**
```python
class SettlementManager:
    def sync() -> dict          # 增量同步，返回结果摘要
    def get_all_costs() -> dict # {"IF2609_多": 4210.0, ...}
    def get_cost(symbol, direction) -> float | None
    def get_latest_date() -> str | None
    def load_costs_from_meta()   # 从 meta 加载到内存（启动时调用）
```

**关键设计决策：**
- 内存只读缓存，无锁访问（Greeks 计算直接读 `_cost_cache`）
- 与 CTP 连接解耦：断线不影响已加载的结算单数据
- 增量扫描：只补缺漏日期，不重复下载已有 `full_*.json`
- 解析逻辑内嵌（`_parse_txt`），不依赖外部 `parse_settlement_full.py`

**CTP 字段名修复（重要）：**
```
vnpy gateway 发送（错误）: {"TradingDate": "20260909"}
CTP struct 实际字段名    : {"TradingDay": "20260909"}
→ 字段名不匹配 → CTP 忽略该字段 → 默认查当天（永远查不到历史）
→ 修复：两处发送端均改为 "TradingDay"
```

---

## 三、核心计算与业务规则

### 3.1 标的期货与期权代码归一化映射

| 品种 | CFFEX 映射 | 期货乘数 | 期权乘数 |
|------|-----------|---------|---------|
| 沪深300 | IO* → IF* | IF=300元/点 | 100元/点 |
| 中证1000 | MO* → IM* | IM=200元/点 | 100元/点 |
| 上证50 | HO* → IH* | IH=300元/点 | 100元/点 |
| 商品（铜等） | 无需映射 | 各品种固定乘数 | 各品种固定乘数 |

**商品乘数速查**：cu=5, au=1000, ag=15, ru=10, m=10, jm=60, rb=10, i=100

> 中金所期权前缀与期货不同（IF→IO, IM→MO），不是简单前缀替换。MO 期权的实际标的是 IM 期货，订阅和 tick 获取两处均需 MO→IM 映射。

### 3.2 adjust_price 与 IV 计算（盘中实时，与结算单完全解耦）

#### adjust_price 四级规则（来源：旧版 DESIGN.md §四.5，代码已移植于 `pricing.calc_adjust_price_4level`）

1. **正常流动性合约**：mid_price 或 last_price
2. **深度实值（ITM）**：PCP 平价公式 + OTM 腿时间价值反推
3. **深度虚值（OTM）/ 盘口宽价差**：微观盘口挂单量 + 动态价差过滤
4. **兜底**：last_price 为空 → pre_close → 上一快照

> **v1.4 定位**：`adjust_price` 是系统内**唯一**的市场估价（Mark），除 Greeks/IV 链路外，也是快照与 `pnl_today` 基准的唯一取数来源（收盘 Mark 采样口径见 §三-B.2）。期权 Mark 依赖其标的期货 Mark 构成计算链；**期货腿当前无独立 Mark**（`option_ticks` 里期货 bid/ask 被写 0，实际等于 last_price），属已知缺口 B4。
>
> 期货腿若将来补 Mark，方案为**盘中结算模式**：成交稀疏度突破阈值时，取此前数分钟按**时间距离衰减加权**的均价替代末价——与交易所结算价的构造思路一致。

#### IV 计算链路（来源：旧版 DESIGN.md §四.关键设计决策，设计文档 v4 漏写，本基线补入）

```
1. 先用上述规则算 adjust_price
2. 用 adjust_price 作为市场价格 M，送入 implied_vol_bisection 反推 IV
3. 将反推得到的 IV 写入 tick['iv']，作为 calc_greeks 的输入
4. 兜底：bisection 未收敛（NaN/不收敛）→ 同到期日 ATM IV 中位数；
         结果 > 500% → 直接截到 500%
         结果 < 1%  → 直接截到 1%
```

##### IV 反推输入校验

```
输入 price 必须满足理论上下界（否则直接拒绝反推）：

  Call: max(F - K * exp(-rT), 0) ≤ price ≤ F * exp(-rT)  （上限取指数近似）
  Put:  max(K * exp(-rT) - F, 0) ≤ price ≤ K * exp(-rT)

bid/ask 校验：
  - mid_price 反推前：检查 bid > 0 且 ask > mid_price，且 ask - bid 不超过合理阈值（如标的价的 10%）
  - 若 bid/ask 价差不合理（如超过标的价 50%），跳过该腿，标注 iv_source='invalid_spread'
  - 反推结果记录来源：iv_source 字段 = 'adjust_price' | 'atm_median' | 'invalid_spread' | 'nan_result'
```

##### ATM IV 回退（标注 iv_source）

```
当 bisection 返回 NaN 或超出 [0.01, 5.0] 时：
  1. 取同到期日所有合约的 IV 中位数作为 ATM IV
  2. 标注 iv_source = 'atm_median'
  3. 记录日志：f"IV 反推失败 {sym}，回退 ATM IV={atm_iv:.4f}"
```

> **命名统一修正 C1**：设计文档 v4 §3.5 使用 `contract['ttm']`，但 `pricing.py` 与 `api_server.py` 均使用 `days_to_expiry`。本基线统一为 **`days_to_expiry`**，所有 T 计算统一使用自然日：`T = max(days_to_expiry / 365.0, 0.5/365.0)`。

### 3.3 标准 Black-76 与 IV 反推纯函数

#### cp_from_symbol 规则（重要！）

> ⚠️ **CTP 接口 `option_type` 字段值为中文（`"看涨期权"`/`"看跌期权"`），永远不能用于 C/P 判定。** 所有 Greeks 计算和 ITM 判定必须使用 `cp_from_symbol` 从合约名字符串推断：
>
> ```python
> def cp_from_symbol(symbol: str) -> int:
>     """从合约名推断 C/P。C=1, P=-1"""
>     return 1 if symbol.rfind('C') > symbol.rfind('P') else -1
> ```
>
> 示例：`au2612C1200`→1，`au2612P880`→-1，`MO2612-C-9000`→1，`mo2612-p-9000`→-1，`sc2611C750`→1
>
> 此函数已固化于 `risk_engine.py`（L86-92），`pricing.py` 在 legs_by_group 构造时同步用 `name.rfind('C')` 判定。

#### black76 函数

```python
import math
from scipy.stats import norm

def black76(IV: float, F: float, K: float, T: float, r: float = 0.02, cp: int = 1):
    """Black-76 模型单张 Greeks 计算。cp: 1=Call, -1=Put。T: 最小截断 0.5/365。
    
    IV 输入单位：VNpy 直接返回小数形式（如 0.165 表示 16.5%），非百分比。
    若发现 IV > 1.0，说明数据源返回了百分比形式，此时除以 100 归一化，并记录一次警告日志。
    """
    # IV 单位归一化
    if IV > 1.0:
        IV = IV / 100.0  # 百分比形式归一化为小数
    v = max(IV, 0.001)
    sqrtT = math.sqrt(T)
    d1 = (math.log(F / K) + 0.5 * v * v * T) / (v * sqrtT)
    d2 = d1 - v * sqrtT
    exp_rt = math.exp(-r * T)
    delta = cp * exp_rt * norm.cdf(cp * d1)
    gamma = exp_rt * norm.pdf(d1) / (F * v * sqrtT)
    vega  = F * exp_rt * norm.pdf(d1) * sqrtT * 0.01
    term1 = - (F * exp_rt * norm.pdf(d1) * v) / (2.0 * sqrtT)
    term2 = - cp * r * F * exp_rt * norm.cdf(cp * d1)
    term3 =   cp * r * K * exp_rt * norm.cdf(cp * d2)
    theta = (term1 + term2 + term3) / 365.0
    return {"delta": delta, "gamma": gamma, "vega": vega, "theta": theta}


def implied_vol_bisection(price: float, F: float, K: float, T: float,
                           r: float = 0.02, cp: int = 1, tol: float = 0.0001) -> float:
    """二分法反推 IV。参数顺序：price, F, K, T, r, cp。返回小数形式。收敛失败返回 0.20 兜底。"""
    if price <= 0 or F <= 0 or K <= 0:
        return 0.20
    v_low, v_high = 0.001, 5.0
    for _ in range(30):
        v_mid = (v_low + v_high) * 0.5
        p = cp * math.exp(-r * T) * (
            F * norm.cdf(cp * ((math.log(F / K) + 0.5 * v_mid * v_mid * T) / (v_mid * math.sqrt(T)))) - K * norm.cdf(cp * ((math.log(F / K) + 0.5 * v_mid * v_mid * T) / (v_mid * math.sqrt(T)) - v_mid * math.sqrt(T)))
        )
        if abs(p - price) < tol:
            return v_mid
        if p < price:
            v_low = v_mid
        else:
            v_high = v_mid
    return (v_low + v_high) * 0.5
```

> ⚠️ **参数顺序陷阱**：`implied_vol_bisection(price, F, K, T, r, cp)` 中 **T 在第4位，r 在第5位**。调用时两者位置不能互换（曾经错误地互换导致 IV 算出 65% 而非正确值 25%）。

### 3.4 持仓 Greeks 与 Cash Greeks（兼容期货与期权）

#### 3.4.1 Greeks 口径铁律（v1.1 固化）

> **这是全系统最重要的口径约定，所有 Greeks 计算必须严格遵守：**

1. **可汇总列**（delta / gamma / vega / theta）：每行 = 多头原始值 × 方向sign × |手数|
   - `pos_delta = g['delta'] * (1 if direction=='long' else -1) * abs(volume)`
   - 空头时：sign=-1，|volume|=正数，结果自然为负（short call δ 负✅ short put δ 正✅）
2. **父级汇总**：直接对子级希腊值代数求和（Σ），不二次乘任何因子
3. **现金列**（deltacash / gammacash / vegacash / thetacash）：该列值 × 正因子 × size，**绝不给任何列单独翻号**
   - `deltacash = pos_delta × s × size`（pos_delta 已含符号，cash 与列同号）
   - `gammacash = pos_gamma × F² × 0.01 × size`（1% 标的价变动，**不是 0.01²**）
   - `vegacash = pos_vega × size`（pos_vega 已含符号）
   - `thetacash = pos_theta × size`（pos_theta 已含符号）
4. **期货合约**：delta = 方向 × 手数，无其他 Greeks 列

#### 3.4.2 calc_greeks 参考实现

> ⚠️ **纯函数红线**：`risk_engine.py` / `pricing.py` 中的所有计算函数（`calc_greeks` / `black76` / `implied_vol_bisection` / `calc_pnl` / `calc_adjust_price`）**禁止调用任何外部I/O**，包括但不限于：
> - `engine.query_tick()` — 严格禁止，计算所需数据全部由 Worker 层从 `ticks` 字典显式传入
> - `requests`、`open`、`socket` 等 I/O 调用
> - 违反此约束会导致 Worker 线程阻塞、报价延迟累计、死锁等严重问题

> **数据传入约定**：标的期货价（underlying_price / F）必须由 Worker 层在 `_poll_once` 中显式构造 `tick` 时填入 `tick['underlying_price']` 字段，再传给 `calc_greeks`。Worker 层从 `_engine.query_tick(symbol)` 拿到数据后写入 `tick`，而非让计算函数自行查询。

```python
def calc_greeks(tick: dict, position: dict, contract: dict) -> dict:
    direction_sign = 1 if position['direction'] in ('long', '多') else -1
    volume = abs(position['volume'])   # ← 始终取正，sign 在 direction_sign 中
    size = contract.get('size', 1)
    F = tick.get('underlying_price', 0) or tick.get('last_price', 0)

    # 分支1: 期货合约
    if contract.get('product_type') == 'FUTURES' or not contract.get('option_type'):
        pos_delta = volume * direction_sign
        return {
            "delta": pos_delta, "gamma": 0.0, "vega": 0.0, "theta": 0.0,
            "deltacash": round(pos_delta * F * size),
            "gammacash": 0, "vegacash": 0, "thetacash": 0
        }

    # 分支2: 期权合约
    iv    = tick.get('iv', 0.20)
    strike = contract.get('strike', 0)
    T      = max(contract.get('days_to_expiry', 90) / 365.0, 0.5 / 365.0)  # 下限 0.5/365
    cp    = cp_from_symbol(position.get('symbol', ''))   # ← 永远不用中文 option_type

    g = black76(iv, F, strike, T, r=0.02, cp=cp)
    pos_delta  = g['delta']  * direction_sign * volume
    pos_gamma = g['gamma']  * direction_sign * volume
    pos_vega  = g['vega']   * direction_sign * volume
    pos_theta = g['theta']  * direction_sign * volume   # ← 不单独翻号

    return {
        # 可汇总列（头寸级）
        "delta": pos_delta, "gamma": pos_gamma, "vega": pos_vega, "theta": pos_theta,
        # 现金列（该列×正因子×size）
        "deltacash":  round(pos_delta  * F         * size),
        "gammacash":  round(pos_gamma * F * F * 0.01 * size),  # F²×1%×size
        "vegacash":   round(pos_vega  * size),
        "thetacash":  round(pos_theta * size),
    }
```

> **⚠️ 曾经犯过的错误（记录以免重蹈）：**
> - ❌ `direction_sign * abs(volume)` 误写为 `abs(volume * direction_sign)`：abs 套在外面导致 sign 被吞
> - ❌ short put δ 双重取号（position['volume'] 已是负数再乘 direction_sign）
> - ❌ `gammacash = pos_gamma * F² * 0.01 * 0.01 * size`：多乘了一个 0.01，结果虚高 100 倍
> - ❌ `vegacash = -pos_vega * size`：单独给 vegacash 翻号，违反"不给任何列单独翻号"规则
> - ❌ ITM theta 多头取负（`if direction_sign==-1: theta=-g['theta']*...`），违反空头 theta 列自然为负
> - ❌ `implied_vol_bisection(price, s, k, r, ttm, cp)`：T 和 r 位置互换，算出 IV=65% 而非正确值 25%

### 3.5 PnL 口径计算（历史基础版）

> ⚠️ **DEPRECATED（已由 §3.7 替代）**：本节仅保留函数签名作历史参考，正文逻辑已被 §3.7 增强版完全替代。

```python
def calc_pnl(position: dict, contract: dict, tick: dict,
             settlement_dict: dict, yesterday_snapshot: dict = None) -> dict:
    """历史签名，请使用 §3.7 的 calc_pnl 增强版"""
    pass  # 已废弃
```

### 3.6 树形聚合规则

```
L3 合约 → 归属 L2 月份（IF_2609）
L2 月份 → 归属 L1 品种（IF）

聚合指标：
  volume      = Σ|pos.volume|（绝对值，反映物理负荷）
  Greeks/PnL  = Σ 带符号代数加总（自然对冲，净方向由正负呈现）
```

### 3.7 PnL 两口径计算（增强版：今仓/老仓双基准 + 基准可见性）

> **来源**：本节内容由 `docs/pnl设计.md` 合并入基线，并经审查意见修正 P0-1~4 后作为唯一正式规范。替代原 3.5 基础版。
> **v1.5 变更**：`pnl_daily` 整条废弃，口径收为 `pnl_today` / `pnl_history`；新增每腿 `price_basis` / `cost_basis` 标签与 `summary.pnl_basis_counts` 计数（见本节末「基准可见性」）。

#### 口径约定

所有 PnL 分项统一使用以下约定：

```
signed_qty = direction_sign * abs(volume)   # direction_sign = +1（多）/ -1（空）

pnl = signed_qty * (mark_price - base_price) * size
```

> ⚠️ **方向铁律**：平仓成交的买卖方向（买/卖）**不得**替代原持仓方向参与 PnL 计算。PnL 符号由原持仓方向决定，与本次成交是买还是卖无关。

#### 核心公式

```
pnl_today   = direction_sign * (Mark_now - Mark_T-1) * abs(vol) * size
pnl_history = pnl_realized + pnl_unrealized

pnl_realized   = direction_sign * (P_trade_close - cost_price) * abs(vol_closed) * size
pnl_unrealized = direction_sign * (Mark_now - cost_price) * abs(vol_remaining) * size
```

#### 今仓/老仓判断

```
今仓 = 合约在 full_{T-1}.json 的 positions_detail 中不存在
     且 合约在 trade_cache 中存在（今天有成交记录）

老仓 = 合约在 full_{T-1}.json 中存在
```

#### 基准价优先顺序（v1.5 定稿）

pnl_today 基准是一条**四档降级链**，逐腿取第一个可用值，取不到即 `pnl_today=None`（不显示、不入汇总、不写 NaN）：

| 档 | 基准来源 | 取数键 | `price_basis` 标签 | 适用腿 |
|---|---|---|---|---|
| 1 | T-1 收盘快照 `close_snapshot_{T-1}` 的 `adjust_price`（14:55–15:00 Mark 算术平均，叶内值须为 `price_basis="close_avg"`） | `{sym}_{direction_str}` | `prev_close_snapshot` | 昨仓今持 |
| 2 | T-1 结算单昨结算价 `settlement_prices`（**仅当整份快照缺失 / 该键不存在 / 值≤0**） | 裸合约名 `{sym}` | `prev_settlement_fallback` | 昨仓今持（降级） |
| 3 | 今日成交账本开仓加权价 `today_open_cost`（F4，昨收昨结均无 = 今开仓腿） | `{sym}_{direction_str}` | `today_open_cost` | 今开今持 |
| 4 | 无基准 | — | `none` → `pnl_today=None` | 数据缺失腿 |

其他固定标签：

| 情形 | `price_basis` | 含义 |
|---|---|---|
| 无行情推送 / `adjust_price`≤0 | `no_tick` | 早退，`pnl_today=None`、`pnl_history=0`，不污染汇总 |
| 当前手数 0（已全平） | `closed` | 两条 PnL 记 0，其已实现走 §3.8 账本通道 |
| 档 1/2 命中但账本同时有今开成交 | `prev_close_snapshot+today_open_mixed` / `prev_settlement_fallback+today_open_mixed` | 单一基准拆不开今昨手数，昨仓基准覆盖全部手数 → **数值待核**，由 §3.7「基准可见性」告警 |

pnl_history 的开仓成本 `cost_price` 是**另一条独立链**（不得与上面混用）：结算单开仓均价 `settlement_dict[{sym}_{direction_str}]`（`cost_basis="settlement_cost"`）→ 今日账本开仓加权价（`ledger_open_cost`）→ CTP 持仓均价 `position.price`（`position_price`，最后一档仅在前两者皆无时使用）。

> ⚠️ **基准铁律（v1.5）**：
> 1. 基准必须是**上一业务日的收盘截面 Mark**，不得用同日盘中价、不得跨业务日回退找更旧快照。
> 2. 基准不得用末成交价 `last_price`（期权尾盘稀疏、做市商撤单，末价无代表性）。
> 3. 样本数少**不**触发降级；只有整份快照缺失才降级结算价。
> 4. 快照基准仅覆盖白盘收盘。有夜盘的品种（sc/au/ru/cu 等），21:00–02:30 的涨跌按本口径归入**其次日**业务日的 pnl_today，与交易所「夜盘归新交易日、以昨结算起算」的盯市口径存在一次性错位（用户已裁决接受）。
> 5. 档 3「今开仓腿以开仓价为基准」与老系统盘中盯市口径一致（IF2610 今开 8 手@4464、现价 4478.2 → 34,080 元；配 IF2609 平昨 73,968 元 = 108,048，对表老系统 108,500，残差为现价采样时点噪声）。
> 6. 基准取不到就是 `None`，**不得**用 `position.price`（成本价）或任意盘中价凑一个能显示的数。

#### 四分法（pnl_today）

| | 情形 | 公式 |
|---|---|---|
| ① | 昨仓今持（未平） | `direction_sign * (Mark_now - Mark_T-1) * abs(vol) * size` |
| ② | 昨仓今平（平昨） | `direction_sign * (P_trade_close - Mark_T-1) * abs(vol_closed) * size` |
| ③ | 今仓未平（新开） | `direction_sign * (Mark_now - P_trade_open) * abs(vol) * size` |
| ④ | 今仓今平（平今） | `direction_sign * (P_trade_close - P_trade_open) * abs(vol_closed) * size` |

#### 四分法（pnl_history）

| | 情形 | 公式 |
|---|---|---|
| ① | 昨仓今持 | `direction_sign * (Mark_now - cost_price) * abs(vol) * size` |
| ② | 昨仓今平（全平） | `direction_sign * (P_trade_close - cost_price) * abs(vol_closed) * size` |
| ③ | 今仓未平 | `direction_sign * (Mark_now - P_trade_open) * abs(vol) * size` |
| ④ | 今仓今平（全平） | `direction_sign * (P_trade_close - P_trade_open) * abs(vol_closed) * size` |

#### 混合持仓处理（数量守恒 + OffsetFlag 归因）

> ⚠️ **归因优先级**：优先使用 CTP `OffsetFlag`（开/平今/平昨）；仅在字段缺失时才使用 FIFO 降级，并标注 `allocation_source`。

```
已知:
  volume_old = 昨仓初始 volume（来自 full_{T-1}.json）
  vol_open   = Σ trade_cache 中 open_close='开' 的 volume
  vol_close  = Σ trade_cache 中 open_close='平' 的 volume
  positions  = 当前持仓 volume（来自 CTP position）

Step 1: 按 OffsetFlag 分配（第一优先）
  若 CloseYesterday 成交记录存在:
    vol_老_已平 = Σ CloseYesterday volume
    vol_新_已平 = Σ CloseToday volume
  否则:
    # FIFO 降级（仅 OffsetFlag 缺失时）
    vol_老_已平 = min(volume_old, vol_close)
    vol_新_已平 = max(0, vol_close - volume_old)   # 平仓超出昨仓部分 = 今仓被平

  vol_老_剩余 = max(0, volume_old - vol_老_已平)
  vol_新_剩余 = vol_open - vol_新_已平

  # 守恒校验: vol_老_剩余 + vol_新_剩余 == abs(positions)
  assert abs(positions) == vol_老_剩余 + vol_新_剩余

Step 2: 分别计算今仓/老仓的平仓均价 VWAP
  P_trade_close_老 = Σ(CloseYesterday 或 FIFO 平老部分的成交价 × 成交量) / vol_老_已平
  P_trade_close_新 = Σ(CloseToday 或 FIFO 平新部分的成交价 × 成交量) / vol_新_已平

Step 3: 分别计算 PnL
  pnl_老_today_unrealized = direction_sign * (Mark_now - Mark_T-1) * vol_老_剩余 * size
  pnl_老_today_realized   = direction_sign * (P_trade_close_老 - Mark_T-1) * vol_老_已平 * size
  pnl_老_history          = direction_sign * (P_trade_close_老 - cost_price) * vol_老_已平 * size
                           + direction_sign * (Mark_now - cost_price) * vol_老_剩余 * size

  pnl_新_today_unrealized = direction_sign * (Mark_now - P_trade_open) * vol_新_剩余 * size
  pnl_新_today_realized   = direction_sign * (P_trade_close_新 - P_trade_open) * vol_新_已平 * size
  pnl_新_history          = pnl_新_today_realized + pnl_新_today_unrealized

Step 4: 汇总
  pnl_today   = (pnl_老_today_unrealized + pnl_老_today_realized
               + pnl_新_today_unrealized + pnl_新_today_realized)
  pnl_history = pnl_老_history + pnl_新_history
```

#### 边界情况

| 情况 | 处理方式 |
|---|---|
| 同一合约多次开仓 | 取成交量加权均价（VWAP） |
| 今开今平（当日回转） | vol_新_已平 = vol_open，`pnl_新 = pnl_新_today_realized`（全为已实现） |
| 老仓今平（部分） | vol_老_剩余 > 0，老仓同时有 realized + unrealized |
| 老仓今平（全平） | vol_老_剩余 = 0，老仓只有 realized，合约从持仓 tree 消失 |
| vnpy TdApi 重连 | trade_cache 保留（进程内），重连后追加，幂等去重（见 §3.8） |
| OffsetFlag 缺失、FIFO 降级 | 标注 `allocation_source: "fifo_fallback"`，并记录日志 |

#### 已完全平仓合约的 PnL 通道

> 全平合约从持仓 tree 消失，但其 realized PnL 必须进入当日和历史汇总。

```
实现方式：
  _realized_pnl_cache: dict[sym, running_total]  # 每笔成交实时累加
  在 /api/dashboard 返回前：
    realized_today = Σ _realized_pnl_cache[sym]
    summary["total_pnl_today"]   += realized_today
    summary["total_pnl_history"] += realized_today
    # 当前持仓只贡献 unrealized_pnl
```

#### 基准可见性（F1，v1.5 新增）

> 目的：让「这条腿的当日盈亏是哪来的」可核对。每轮 `_poll_once` 由 `build_tree` 汇总各腿标签成 `summary.pnl_basis_counts`，Worker 在计数签名变化时打一次 WARNING（不每轮刷屏）。

```
summary.pnl_basis_counts = { "prev_close_snapshot": N, "prev_settlement_fallback": N,
                             "today_open_cost": N, "none": N, "closed": N, "no_tick": N }
```

告警样例（2026-09-18 13:25 上线后实测）：
```
[pnl基准] 非昨收盘快照基准腿 计数={'closed': 3, 'prev_settlement_fallback': 34, 'none': 3}
        （快照基准 0 腿）—— 见基线 §3.7 降级链
```

**判读规则**：`prev_close_snapshot` 之外的任何非零计数都要能解释。

| 计数 | 常见原因 | 处理 |
|---|---|---|
| `prev_settlement_fallback` 全账户 | 该业务日无收盘快照（15:00 服务未运行） | 等下一交易日；连续出现要查服务是否 15:00 前被停 |
| `today_open_cost` | 今开仓腿，正常 | 无需处理 |
| `none` | 今日开仓但账本里没有该笔（成交发生在账本启用/重启之前） | 只影响当日，次日由结算单接管 |
| `closed` | 已全平腿，正常 | 已实现盈亏在 §3.8 通道 |
| `no_tick` | 未开盘 / 无行情推送 | 开盘后自动消失 |
| `+today_open_mixed` | 昨仓与今开并存，基准未拆手数 | **数值待核**，见 §十 B6 |

#### calc_pnl 增强版函数签名（v1.5 实际实现）

```python
def calc_pnl(position: dict, contract: dict, tick: dict,
             settlement_dict: dict,
             settlement_prices: dict = None,
             yesterday_snapshot: dict = None,
             received_today: dict = None,
             today_open_cost: dict = None) -> dict:
    """请通过 build_tree 统一调用，不要单独调用本函数。

    contract: 合约元数据，含 size（乘数），必须传入，VNPY PositionData 无 size 字段。
    received_today: {sym: True} 今日已收到行情推送的合约；未收到或 _is_no_tick → pnl_today=None。
    today_open_cost: {f"{sym}_{direction}" → 今日账本开仓加权价}，供今开仓腿取基准/成本。
    返回: {"pnl_today", "pnl_history", "cost_price", "price_basis", "cost_basis"}
    """
```

实现顺序（与代码逐行对应）：

```
1. 无行情（received_today 未命中 或 _is_no_tick）→ 早退 {None, 0.0, "no_tick", "n/a"}
2. vol == 0（已全平）                          → 早退 {0.0, 0.0, "closed", "n/a"}
3. cost_price：settlement_dict[{sym}_{dir}] → today_open_cost → position.price（记录 cost_basis）
4. pnl_history = direction_sign * (adjust_price - cost_price) * vol * size
5. base_today：四档降级链（见上表），记录 price_basis；命中档1/2 且账本有今开 → 追加 +today_open_mixed
6. base_today 为 None → pnl_today = None；否则 direction_sign * (adjust_price - base_today) * vol * size
```

> **注意**：结算单中的 `prev_sttl_price`（昨结算列）**永远不使用**。
> **注意**：`direction_str` 在本函数内是 `'long' / 'short'`（与结算单/账本的 `{sym}_{direction}` 键一致），不是 `'多' / '空'`。

### 3.8 CTP 成交回报与成交账本（v1.5 重写，已落码）

> **数据流**：CTP `RtnTrade` → vnpy `EVENT_TRADE` → `_on_trade()` → 内存账本 `_TRADE_CACHE` + `快照/trade_ledger.json` 落盘 + `_REALIZED_PNL_CACHE` 累加 → 每轮 `_poll_once` 注入 `summary`。
>
> ⚠️ **注册红线（B5 教训）**：`event_engine` 不是 `VNPYEngine` 的属性，它是 `run()` 里的局部变量，只挂在 `MainEngine` 上。必须写成 `eng.main_engine.event_engine.register(EVENT_TRADE, _on_trade)`。历史实现 `eng.event_engine.…` 每次连接都抛 `AttributeError` 被 `except` 吞成一条 WARNING，结果 **`_on_trade` 从未被调用过**，已实现盈亏通道长期是死代码——注册函数只 catch 不 raise 是这类静默失效的温床，新增注册类代码必须用「日志出现成功行」作为验收判据。
> 重连时 `_connect_engine` 返回全新引擎（新 EventEngine），首连与重连两个调用点各注册一次，不产生重复回调。

#### 账本分组 Key（不含 offset）

> ⚠️ `offset`（开/平）不能放入分组 Key，否则同一持仓方向的开仓和平仓记录会被割裂到不同组，无法共同参与 PnL 计算。

```python
ledger_key = (trading_day, account, exchange, symbol, position_direction)
# position_direction ∈ {'long','short'} —— 原持仓方向，与 settlement_dict 的 `{sym}_long`/`{sym}_short` 键同构
# 分组键中不含 offset；offset 保留在每条成交记录中用于归因
```

#### 去重 Key（幂等）

```python
dedup_key = f"{trading_day}_{account}_{exchange}_{trade_id}"   # 字符串形态，直接落盘
# 落 _SEEN_TRADE_IDS；命中即跳过 → CTP 重连重推同一笔不会双计
# 无 tradeid 时用 f"{trade_time}_{price}_{volume}" 合成，保证重放仍可用
```

#### 每条成交记录格式（实际 19 字段，落盘即此结构）

```json
{
  "dedup_key": "20260918_CTP|101009_CFFEX_IF2609_278350",
  "ledger_key": ["20260918", "CTP|101009", "CFFEX", "IF2609", "long"],
  "trade_id": "278350",
  "symbol": "IF2609",
  "direction": "Direction.SHORT",
  "trade_side": "short",
  "position_direction": "long",
  "open_close": "平",
  "offset_flag": "close_yesterday",
  "allocation_source": "ctp_offset",
  "price": 4491.62,
  "volume": 8,
  "trade_time": "2026-09-18 09:35:12",
  "account": "CTP|101009",
  "exchange": "CFFEX",
  "trading_day": "20260918",
  "cost_price": 4460.8,
  "cost_basis": "prev_settlement",
  "realized_pnl": 73968.0
}
```

> **字段说明**：
> - `direction`：vnpy 原始枚举串（审计留痕，**不参与判断**）；`trade_side`：解析后的买卖侧 `long`/`short`
> - `position_direction`：本笔成交所作用的原持仓方向（开仓=买卖同向，平仓=买卖反向，卖平即平多头）
> - `offset_flag`：`open` / `close_today` / `close_yesterday`；中金所等只报「平」不区分今昨 → 归 `close_yesterday`（成本阶梯会自动降级到今开加权）
> - `allocation_source`：`ctp_offset`（原生）或 `fifo_fallback`（字段缺失时由 `is_open` 推断）
> - `cost_price` / `cost_basis` / `realized_pnl`：仅平仓腿有值；**重放按落盘原值恢复，不重算**（避免重放时点昨结/行情已变）

#### 方向与开平解析（F3，实际实现）

```python
# vnpy 枚举陷阱：str(Direction.LONG) == 'Direction.LONG'，Direction.LONG.value == '多'
# 旧代码拿 str() 去匹配 ('long','Long','B'…) → 永不命中 → 所有成交恒判 short、已实现盈亏符号全反
dir_val   = getattr(trade.direction, 'value', '') or ''        # '多' / '空'
trade_side = 'short' if (dir_val == '空' or 'SHORT' in str(trade.direction).upper()) else 'long'

# 头寸方向：开仓与买卖同向；平仓反向（卖平 = 平掉多头）
position_direction = trade_side if offset_flag == 'open' else ('short' if trade_side == 'long' else 'long')
```

> ⚠️ 已实现盈亏的符号由 `position_direction`（原持仓方向）决定，与本次成交是买是卖无关（§3.7 方向铁律）。

#### 平仓成本阶梯（F2，定稿）

`pnl_realized = direction_sign × (成交价 − cost_price) × volume × size`，`size` 取合约表 `contract['size']`（合约信息未就绪时按 1 并 WARNING）。`cost_price` 按 offset 走不同优先序，逐级取第一个 >0 的值，命中来源写入 `cost_basis`：

| offset | 阶梯顺序 | `cost_basis` 取值 |
|---|---|---|
| `close_yesterday`（含中金所只报「平」） | ① T-1 昨结算价 `settlement_prices[sym]` → ② 今日账本开仓加权价 → ③ 结算单开仓均价 `settlement_dict[{sym}_{dir}]` | `prev_settlement` / `today_open_cost` / `settlement_open_cost` |
| `close_today` | ① 今日账本开仓加权价 → ② T-1 昨结算价 → ③ 结算单开仓均价 | 同上 |
| 三档全无 | 用成交价本身，`realized_pnl` 记 0 并 WARNING | `unknown_use_trade_price` |

> 平昨首选昨结算，与交易所盯市结算一致；昨收只用于**浮动**盈亏基准（§3.7 档 1），不进平仓成本阶梯。这是老系统盘中口径与官方结算单的分工，不合并。

#### 今日开仓加权价（F4 数据源）

```python
_TODAY_OPEN_ACC: dict[str, list] = {}   # f"{sym}_{position_direction}" → [Σ(价×量), Σ量]
# 开仓成交（含重放）实时累加；_open_cost_map() 出口 = {key: 加权均价}
# 两个消费方：① calc_pnl(today_open_cost=…) 作今开腿基准；② 上表平仓成本阶梯第 2/① 档
```

#### 落盘与重放（F2）

```
文件：快照/trade_ledger.json
结构：{"trading_day": "YYYYMMDD", "trades": [record…]}     # record 即 §3.8 的 19 字段
写  ：每笔成交后全量原子重写（tempfile → flush → fsync → os.replace）
      ponytail: 日内成交条数有限，全量重写最省事；上千条时改追加写 + 压缩
读  ：Worker 启动时 _load_trade_ledger(expected_day=CTP TradingDay)
      → 逐条 _replay_trade_record：按 dedup_key 幂等入内存账本、开仓累加今开加权、
        平仓 realized 按**落盘原值**恢复（不重算）
      → 文件 trading_day ≠ 当前交易日：整本丢弃，等结算单接管（日志留痕）
切日：_rollover_trading_day(td) 每轮 _poll_once 调用；td 取 CTP TradingDay，
      与内存账本日不同 → 账本/去重集/realized/今开加权全清
```

> ⚠️ **账务分区只用 CTP TradingDay**（夜盘 21:00 的成交属下一业务日）。本机 clock 仅在取不到 TradingDay 时兜底，且只影响 `trading_day` 字段，不参与任何时间平移或清理判断。
> **不再使用** `session_state.json` 存账本（v1.4 之前的设想，已废）。

#### 已实现 PnL 注入汇总

```python
total_realized = sum(_REALIZED_PNL_CACHE.values())     # key = symbol
# _poll_once 末尾（tree 建完之后）注入，键名必须对齐 _make_summary 的 total_*：
summary["total_pnl_today"]   += total_realized
summary["total_pnl_history"] += total_realized
```

> 全平合约从 tree 消失（`price_basis="closed"`、两条 PnL 记 0），其当日贡献完全来自这条通道；`build_tree` 不重复计入。

#### 已知限制

| 限制 | 影响 | 处置 |
|---|---|---|
| 账本启用（2026-09-18 13:25）之前的当日成交无来源可补 | 当日 realized 缺这些笔，次一交易日由结算单接管 | 用户裁决：不补录，无意义 |
| `trade_id` 缺失时按 时间+价+量 合成 | 同一秒同价同量的两笔会被去重吞掉 | 观测到再改（当前 CTP 均回传 tradeid） |
| 混合腿（昨仓+今开）单一基准未拆手数 | `pnl_today` 偏高/偏低 | 标 `+today_open_mixed`，见 §十 B6 |

### 3.9 开盘判断与事件驱动

#### 开盘判断规则（事件驱动）

```
任何合约当天收到第一个 tick → 标记为"已开盘"，开始计算 pnl_today
没收到 tick → pnl_today = 0
跨交易日自动清空（CTP TradingDay 变化）
```

不按品种/时间表机械判断，纯事件驱动：收到 tick = 开盘。

#### 持久化范围

| 数据 | 持久化 | 原因 |
|---|---|---|
| 持仓快照（positions_detail + adjust_price） | ✅ | 服务重启后恢复 |
| 结算成本（settlement_cost_dict） | ✅ | 本地文件，已实现 |
| CTP 成交回报（成交账本） | ✅ | `快照/trade_ledger.json`，每笔成交原子落盘；重启按原值重放，当日已实现盈亏不再归零（v1.5，§3.8） |
| 持仓合约开盘状态（opened_contracts） | ✅ | 重启后恢复哪些合约已开盘 |
| 实时行情（tick） | ❌ | 随时变化，重启后重新接收 |

### 3.A ~~session_state.json 持久化结构与重启恢复~~（DEPRECATED，从未实现）

> ⚠️ 本节是 v1.4 之前的设想，代码里不存在 `session_state.json`。成交账本的实际持久化见 §3.8「落盘与重放」（`快照/trade_ledger.json`，CTP TradingDay 分区）。`opened_contracts` 实际为进程内 `_OPENED_CONTRACTS` + `_RECEIVED_TODAY`（按行情推送日重置，不落盘）。以下内容仅存档，不作规范。

#### 数据结构

```json
{
  "trading_day": "20260916",
  "opened_contracts": {
    "IC2609": {"first_tick": "09:30:05"},
    "jm2610": {"first_tick": "21:00:12"}
  },
  "trade_cache": {
    "MO2609-P-7000": [
      {"trade_id": "278350", "direction": "买", "open_close": "开", "price": 11.600, "volume": 1, "trade_time": "20260916 09:30:12"}
    ]
  },
  "settlement_cost_dict": { ... }
}
```

#### 重启恢复逻辑

```
启动时:
  1. 加载 session_state.json
  2. 若 trading_day == 当前交易日:
       恢复 opened_contracts（已开盘合约继续算 pnl_today）
       恢复 trade_cache（昨仓降级用，今仓基准丢失只能归零）
  3. 若 trading_day != 当前交易日:
       清空 opened_contracts（新一轮交易日）
       清空 trade_cache
```

#### 盘中持久化时机

```
触发条件:
  - 持仓发生变化（position.volume 变化）
  - 成交回报到达（trade_cache 新增条目）
  - 合约首次收到 tick（opened_contracts 新增）

写入方式:
  - 异步写，不阻塞主流程
  - 每 30 分钟心跳保存（即使无变化）
```

### 3.B 附录：PnL 关键字段说明

| 字段 | 来源 | 用途 |
|---|---|---|
| `adjust_price` | 行情 tick | 当前 Mark Price |
| `close_snapshot.leaves[].adjust_price` | T-1 收盘快照 | 老仓 pnl_today **首选**基准（14:55–15:00 Mark 时间等差算术平均） |
| `close_snapshot.leaves[].price_basis` | T-1 收盘快照 | 值须为 `close_avg`，否则 reader 拒作基准 |
| `close_snapshot.leaves[].samples` | T-1 收盘快照 | 参与平均的样本数，仅供审计，不参与降级判定 |
| `settlement_price`（T-1） | full_{T-1}.json | 老仓 pnl_today 降级基准 |
| `settlement_cost` | settlement_cost_dict | 老仓 pnl_history cost_price |
| `prev_sttl_price` | full_*.json | **不使用** |
| `P_trade_open` | 成交账本 `_TODAY_OPEN_ACC`（今日加权开仓价） | 今仓 pnl_today（档 3）与 pnl_history 成本、平今成本阶梯 |
| `P_trade_close` | CTP `_on_trade` | 平仓已实现盈亏 |
| `price_basis` | `calc_pnl` 输出 | 该腿当日盈亏基准来源标签（§3.7 四档链 + `no_tick`/`closed`/`+today_open_mixed`）；聚合进 `summary.pnl_basis_counts` |
| `cost_basis` | `calc_pnl` / `_on_trade` 输出 | 开仓成本来源标签（`settlement_cost`/`ledger_open_cost`/`position_price`；平仓侧 `prev_settlement`/`today_open_cost`/`settlement_open_cost`/`unknown_use_trade_price`） |
| `today_open_cost` | Worker 账本 → `build_tree` 入参 | `{f"{sym}_{direction}": 加权开仓价}`，今开腿基准唯一来源 |
| `pnl_daily` | — | **v1.5 已删除**，不再是任何口径 |

---

## 三-B、快照引擎业务规则（v1.4 重写）

> **变更动因**：v1.3 的「N/A/P 三时段 + 30min dirty 覆盖」在实盘暴露两个问题：① 14:31 落的 P 快照是**盘中价**不是收盘截面，却被 T 日当作昨收基准；② 同日 P→A→N 回退链让**早盘盘中价冒充昨日收盘价**（实测 36/40 行基准取自 09-17 11:12 的 A 快照，pnl_today 偏差 22%）。v1.4 取消时段概念，**每业务日只在 15:00 白盘收盘落一份**，基准语义唯一。

### 三-B.1 快照时间表

每业务日一份，时刻 = 白盘收盘 15:00。夜盘不参与（夜盘涨跌归入其次日业务日）。

| 事件 | 时刻 | 动作 |
|------|------|------|
| 采样窗口开启 | 14:55:00 | 清空采样缓冲 `_mark_samples` |
| 采样 | 14:55:00–15:00:00，每轮 `_poll_once`（≈1s） | 记录每个持仓叶子当时的 `adjust_price`（Mark） |
| 写盘 | 15:00:00 之后第一轮 poll | 窗口样本取算术平均 → 写 `close_snapshot_{TradingDay}.json`，置 `_close_saved[bd]=True` |
| 重试 | 15:00–15:10 | 写盘失败在此窗口内重试；出窗仍未成功 → 该业务日无收盘快照，T+1 降级结算价 |

同一交易日数据连续继承；TradingDay 变更时重置采样缓冲与 `_close_saved`。

### 三-B.2 收盘 Mark 取数口径（核心）

`adjust_price` 是系统内**唯一**的市场估价（Mark），全链路统一使用：四级规则算它 → 反推 IV → 算 Greeks → 算 PnL → 写快照。快照基准**禁止**使用末成交价 `last_price`。

理由：① 期权尾盘成交极稀疏，几分钟 0 成交是常态，末价无信息量；② 收盘前做市商大量撤单，末笔价易被单边打成异常值。Mark 由盘口驱动、每个 tick 刷新，不依赖成交，才有「时间等差取数」的资格。

**算术平均，不加权（v1.4 定）**

```
mark_close(sym) = Σ samples / len(samples)
samples = 窗口内每轮 poll 读到的 option_ticks[vt_symbol]["adjust_price"]
```

- 时间等差（每 ≈1 秒一个样本），**不按成交量加权、不做 Δvol 差分**
- **重复样本不去重**：Mark 只在收到 tick 时更新，无成交期间连续几秒读到同一值——在算术平均里等价于按时间加权；去重会把口径变成「按事件加权」，错误
- `samples` 数量写入快照文件供事后审计，**不参与降级判定**（v1.4 裁决：样本少亦照用，只有整份快照缺失才降级）
- 采样范围 = 采样时刻的持仓叶子；期权 Mark 依赖其标的期货 Mark，该链路已存在，快照阶段只读回填结果（`api_server.py:735-738`），不重算
- 窗口边界按交易所时间的时钟分量判定；快照的账务归属一律用 **CTP TradingDay**，不得用本机 clock

**期货腿现状（已知偏差，见 §十 B4）**：期货腿不走 `price_options_batch`，`option_ticks` 里期货的 bid/ask 被写 0（`api_server.py:723-724`），`adjust_price` 实际等于 `last_price` → 远月非主力无成交时，5 分钟平均 = 同一个陈旧价重复。**用户裁决：影响有限，本版本不动。** 将来若修，方向是**盘中结算模式**——成交稀疏度突破阈值时，取此前数分钟按**时间距离衰减加权**的均价替代末价；届时采样源与 `price_basis` 字段不变，自动升级。

### 三-B.3 保存逻辑

写盘守护（全部满足才落盘）：

1. `ctp_status == connected`
2. 当前轮次时间已过 15:00 且在 15:00–15:10 重试窗口内
3. 该 TradingDay 未写过（**每日一份，写完即锁定，不覆盖不重写**）
4. 持仓非空，或虽空但**本连接内先见过非空持仓**（真空仓佐证）；否则判通信未就绪 → 不落盘

落盘细节：
- 序列化前过 `_clean_nan()`，文件中不得出现 NaN 字面量
- 原子写：`tempfile`（同目录）→ `flush` + `fsync` → `os.replace`
- 写失败记日志，不抛异常、不阻塞主流程
- Immutable：发布后的快照对象禁止原地修改（`copy.deepcopy`）

保存失败仅两类：**持仓为空且无真空仓佐证**、**写盘/序列化异常**。两者都在 15:00–15:10 窗口内重试。

### 三-B.4 快照文件命名与 Schema

```
close_snapshot_{YYYYMMDD}.json        # YYYYMMDD = CTP TradingDay
```

```jsonc
{
  "version": 4,
  "trading_date": "20260918",
  "saved_at": "2026-09-18T15:00:01+08:00",
  "window": {"start": "14:55:00", "end": "15:00:00"},
  "ctp_status": "connected",
  "snapshot_kind": "live",            // "live" | "empty"
  "data_hash": "...",
  "leaves": {                          // pnl 基准取数入口
    "IF2610_long": {
      "adjust_price": 4231.8,          // 窗口内 Mark 算术平均
      "price_basis":  "close_avg",     // reader 白名单值，非此值不作基准
      "samples":      287,             // 参与平均的样本数（仅审计）
      "last_price":   4232.0           // 参考，不作基准
    }
  },
  "raw":      { "positions": [...], "underlying_prices": {...} },
  "computed": { "tree": [...] }
}
```

### 三-B.5 读取规则（T 日的昨收基准）

只认 **T-1 业务日**的 `close_snapshot`：

```
Mark_T-1 = close_snapshot_{T-1}["leaves"][f"{sym}_{direction}"]["adjust_price"]
           文件缺失 / 读不出 / 键不存在  → T-1 结算单 settlement_price
           两者皆无（今仓）              → None（pnl_today=None，不计入汇总）
```

- **禁止跨业务日回退**找更旧快照（旧快照冒充昨收是错误基准）
- **禁止同日时段互补**（v1.3 的 P>A>N 链废弃）
- reader 只接受 `price_basis == "close_avg"` 的叶子值当基准；历史 `data_snapshot_*` 文件（末价/盘中价）一律不读，留盘审计

### 三-B.6 例外处理与容错

| 情况 | 处理 |
|------|------|
| 服务 15:00 未运行 | 该业务日无收盘快照 → T+1 全部走结算价降级 |
| 持仓为空且本连接内未见过非空 | 判数据通信未就绪 → 不落盘（防止空快照抹掉真实持仓槽位） |
| 持仓为空且本连接内见过非空 | 真空仓，照存（`snapshot_kind: "empty"`） |
| 15:00 前后该合约 0 成交 | Mark 由盘口驱动，仍有值；盘口也空时才退到 `last_price` 兜底 |
| 叶子 `adjust_price` 为 None | 该叶子不写入 `leaves`（reader 自动降级结算价）；**不再用 `position.price` 兜底**（成本价当收盘价是口径错误） |
| 磁盘写失败 | 记日志不抛异常，15:00–15:10 内重试 |
| TradingDay 切换 | 清空采样缓冲 + `_close_saved`，重置 opened_contracts / trade_cache |

---

## 四、数据结构

### 4.1 树形三层规则（基线统一定义）

| 层级 | 含义 | key 规则 | name 显示 |
|------|------|---------|----------|
| **L1 品种** | 包含所有到期日的期货以及衍生的期权 | 品种代码（一般2字母，少数1字母），如 `IF`/`IM`/`IH`/`CU` | 如"沪深300（IF）" |
| **L2 月份** | 该品种下某到期月份 | `{品种}_{月份}`，如 `IF_2609` | 如"2609月份" |
| **L3 合约** | 该品种该月份下所有期货/期权合约明细 | `{合约}_{direction}`，direction ∈ `long`/`short`，如 `IF2609_long`、`au2612C1200_short` | 合约 symbol（前端把 long/short 映射成 多/空 显示） |

> **v1.5 校正**：机器层（key/name/direction/账本/settlement_dict）统一用 `long`/`short`；中文 `多`/`空` 只出现在**展示层**与 `direction_raw`（CTP 原始值）。§3.7/§3.8 的取数键全部按 `long`/`short` 拼。

> **L1 品种规则说明**：中金所期权理论上不直接对应期货（如 IO 期权标的为 IF 指数），但默认近似使用期货代码归组；商品期权无此映射，直接用标的品种代码（如 CU）。相同合约同向头寸在 L3 可合并数量。

### 4.2 快照 JSON Schema（GET /api/dashboard 契约）

```json
{
  "status": "connected",
  "last_update": "14:25:31",
  "summary": {
    "total_deltacash":   125000,
    "total_gammacash":   -45000,
    "total_vegacash":    -12000,
    "total_thetacash":   8500,
    "total_pnl_today":   1500,
    "total_pnl_history": 86000,
    "position_count":    85,
    "pnl_basis_counts":  { "prev_close_snapshot": 30, "closed": 3 }
  },
  "tree": [
  {
    "key": "IF",
    "name": "沪深300（IF）",
    "type": "L1_PRODUCT",
    "metrics": {
      "volume": 22, "deltacash": -422100, "deltacash_tag": "red",
      "gammacash": -40536, "gammacash_tag": "yellow",
      "vegacash": -460, "thetacash": 160,
      "pnl_today": 120, "pnl_history": 9600, "pnl_tag": "green",
      "pnl_basis_counts": { "prev_close_snapshot": 2 }
    },
    "children": [
      {
        "key": "IF_2609",
        "name": "2609月份",
        "type": "L2_MONTH",
        "metrics": {
          "volume": 22, "deltacash": -422100, "deltacash_tag": "red",
          "gammacash": -40536, "gammacash_tag": "yellow",
          "vegacash": -460, "thetacash": 160,
          "pnl_today": 120, "pnl_history": 9600, "pnl_tag": "green",
          "pnl_basis_counts": { "prev_close_snapshot": 2 }
        },
        "children": [
          {
            "key": "IF2609_long",
            "symbol": "IF2609",
            "direction": "long",
            "direction_raw": "多",
            "volume": 2,
            "last_price": 4230.0,
            "adjust_price": 4230.0,
            "open_price": 4210.0,          // = calc_pnl 的 cost_price（开仓成本，非 CTP 持仓均价）
            "underlying_price": 4230.0,
            "iv": null,
            "delta": 1.0, "gamma": 0.0, "vega": 0.0, "theta": 0.0,
            "deltacash": 2538000, "gammacash": 0, "vegacash": 0, "thetacash": 0,
            "days_to_expiry": null, "itm": false,
            "pnl_today": 200, "pnl_history": 1200,
            "price_basis": "prev_close_snapshot", "cost_basis": "settlement_cost",
            "delta_tag": "green", "gamma_tag": "green", "pnl_tag": "green"
          },
          {
            "key": "IO2609-C-4000_short",
            "symbol": "IO2609-C-4000",
            "direction": "short",
            "direction_raw": "空",
            "volume": -20,               // 机器层带符号：空头为负（vnpy PositionData 原样透传）；L1/L2 的 volume 才取 Σ|vol|
            "last_price": 45.2, "adjust_price": 44.8, "open_price": 38.5,
            "underlying_price": 4230.0, "iv": 16.5,
            "delta": -0.35, "gamma": -0.0012, "vega": 0.023, "theta": -0.008,
            "deltacash": -2960100, "gammacash": -40536, "vegacash": -460, "thetacash": 160,
            "days_to_expiry": 25, "itm": false,
            "pnl_today": -80, "pnl_history": 8400,
            "price_basis": "prev_close_snapshot+today_open_mixed", "cost_basis": "settlement_cost",
            "delta_tag": "yellow", "gamma_tag": "red", "pnl_tag": "green"
          }
        ]
      }
    ]
  }
  ]
}
```

> **Schema 补充（基线修正 C2）**：设计文档 v4 原 schema 仅在 L3 节点标注 `*_tag`；本基线要求 **L1/L2 聚合节点也携带完整 `metrics`（含 `deltacash_tag`/`gammacash_tag`/`pnl_tag`）**，使用与 L3 相同的阈值规则（见 §九），否则聚合行无颜色标识。
>
> **v1.5 Schema 补充**：
> - L1/L2 `metrics` 与 `summary` 均携带 `pnl_basis_counts`（各 `price_basis` 标签的腿数），供降级告警与核对；不参与任何数值计算
> - L3 节点新增 `price_basis`、`cost_basis`；`open_price` 语义 = `calc_pnl` 的 `cost_price`（结算单开仓均价 → 账本今开加权 → CTP 持仓均价，见 §3.7）
> - **`pnl_daily` / `total_pnl_daily` 自 v1.5 起不再出现在任何层级的返回里**，前端列配置与汇总卡同步删除（旧快照 JSON 仍带该字段，reader 忽略即可）
> - `pnl_today` 可为 `null`（无基准/无行情），前端渲染为空单元格，不得当 0 参与求和

---

## 五、前端交互设计

### 5.1 最小 UI 状态

```javascript
const UIState = {
  expandedGroups: new Set(),   // 展开的 L1 key，如 Set(['IF', 'CU'])
  expandedMonths: new Set(),   // 展开的 L2 key，如 Set(['IF_2609'])
  sortCol: 'pnl_history',
  sortAsc: false,
  filter: '',
  colConfig: {}
};
```

### 5.2 渲染机制

1. **树形遍历**：根据 tree 数组递归渲染 L1/L2/L3 行，折叠状态由 `UIState.expanded*` 直接映射为 CSS `.collapsed`
2. **纯前端视图排序**：点击列头直接对 L1 数组及 children 做内存级排序（<1ms，禁止发带排序参数的后端请求）
3. **样式注入**：直接读取数据节点自带的 `*_tag` 字段，追加对应 CSS class

### 5.3 CSS 样式映射

```css
.delta-green, .gamma-green, .pnl-green { color: #4ade80; }
.delta-yellow, .gamma-yellow            { color: #fbbf24; }
.delta-red, .gamma-red, .pnl-red        { color: #f87171; }
.pnl-yellow                                { color: #fbbf24; }
```

---

## 六、API 端点

### 6.1 端点列表

| 路由 | 方法 | 功能 |
|------|------|------|
| `/api/dashboard` | GET | 获取完整聚合快照数据 |
| `/api/ctp/status` | GET | CTP 连接状态心跳 |
| `/api/ctp/connect` | POST | 启动 CTP 引擎 |
| `/api/ctp/disconnect` | POST | 安全关闭连接 |

> **v1.5 响应变更**：`/api/dashboard` 的 `summary` 与 L1/L2 `metrics` 新增 `pnl_basis_counts`；L3 新增 `price_basis`/`cost_basis`；`pnl_daily`/`total_pnl_daily` 全层级不再返回（§四 Schema 补充）。`/api/columns` 的默认列配置已删 `pnl_daily`；**前端 `localStorage['col_config']` 里可能残留含 `pnl_daily` 的旧配置并被优先采用**（见 §十 B8）。

### 6.2 CTP 端点安全要求

> ⚠️ CTP 连接端点（`/api/ctp/connect`）**仅允许 loopback（127.0.0.1）访问**，禁止监听公网端口。

| 安全措施 | 要求 |
|---------|------|
| 网络隔离 | API 服务仅监听 `127.0.0.1:5000`，禁止绑定 `0.0.0.0` |
| 鉴权 | 所有 CTP 操作端点需携带账户凭证（当前通过 `ctp_config.json` 本地文件管理） |
| CSRF | Flask 路由加 `@csrf.exempt` 仅限开发；生产环境需启用 CSRFProtect |
| 审计 | 每次 connect/disconnect 记录审计日志（含时间戳、来源IP、操作类型） |
| 限流 | CTP 连接/断开操作每分钟不超过 5 次，超限返回 429 |
| 凭证保护 | `ctp_config.json` 包含明文密码，**禁止提交到 Git**，已加入 `.gitignore` |

### 6.3 Tick 质量字段

> 每 tick 推送时，Worker 必须填充以下质量字段，供前端和计算层判断数据可用性：

```
market_status 取值：
  - "valid":       正常交易，数据可用
  - "not_opened":  合约当日尚未开盘（未收到 tick）
  - "stale":       合约已开盘但超过 30s 未更新（数据可能过时）
  - "invalid_underlying": 标的期货价格为 0 或 NaN（期权 Greeks 计算无效）

price_source 取值：
  - "mid_price":   盘口中间价
  - "last_price":   最新价（无盘口时降级）
  - "pre_close":    昨收价（无现价时降级）
  - "none":         完全无价格

timestamp:  tick 到达时间（Unix 秒），用于 stale 判断
```

### 6.4 输入校验规则

```
volume == 0 的持仓：
  - 在进入 build_tree 之前过滤，禁止进入树结构
  - 已平仓合约的 realized PnL 通过 _realized_pnl_cache 聚合，不依赖持仓树

calc_status 约定：
  - 指标计算失败（NaN/异常）时，该指标字段返回 null，不返回 0
  - 同时返回 calc_status: {<field>: "error"|"missing"|"ok"} 说明各字段状态
  - calc_status 本身不为 null，始终存在
```

---

> ⚠️ **§七已废弃（DEPRECATED）**：本节内容已被 §十二替代。请勿修改本节，仅作历史参考。
>
> v1.3 升级后，**§十二 文件结构**为唯一权威目录，`Dashboard_重构设计.md` 内容已全部被本文档吸收，不再维护。

## 七、文件结构与实施计划（已废弃，请参考 §十二）

```
C:/Quant_2026/期货执行策略/GreeksDashboard_v0.1/
├── Dashboard_重构设计.md   # 主设计文档 v4
├── Dashboard_设计基线.md   # 本文档（基线 v1.0）
├── Dashboard_设计基线_审查报告.md  # v1.0 审查报告
├── run_server.py           # 服务入口（thin wrapper，引用 dashboard_v2/api_server.py）
├── GreeksDashboard_v0.1/
│   ├── dashboard_v2/
│   │   ├── api_server.py   # Flask + Worker 双线程主服务
│   │   ├── pricing.py      # Black-76 Greeks + IV 反推（纯函数）
│   │   └── settlement.py   # 结算单单路径VWAM
│   │   └── risk_engine.py  # Greeks + PnL + 树形聚合 + 标签
│   ├── static/
│   │   └── dashboard11.js  # 前端渲染器（纯渲染，无业务逻辑）
│   └── templates/
│       └── dashboard.html  # HTML 模板
```

---

## 八、红线要求

1. **红线一（禁止修改库源码）**：严禁修改 `C:\veighna_studio\site-packages`。持仓开仓成本全面改由本地结算单补全。
2. **红线二（纯函数解耦）**：`pricing.py` 与 `risk_engine.py` 严禁引入任何 Flask 或 CTP 模块，必须是无状态纯函数。
3. **红线三（严禁前端碰业务）**：dashboard11.js 严禁正则解析合约代码，严禁计算 Greeks/汇总，所有字段由后端预聚合。
4. **红线四（异步读写分离）**：Worker 生成完整树后通过单个指针赋值（Atomic Swap）更新快照。Flask `/api/dashboard` 禁止在请求上下文中做任何循环迭代计算。

> ⚠️ **基线缺口 G1**：当前 `pricing.py` 的 `price_options_batch` 仍接收 `engine` 参数并调用 `engine.main_engine.get_tick`/`engine.get_contract`，违反红线二。`engine` 参数应删除，行情统一由 `option_ticks` 传入。
> ⚠️ **基线缺口 G4**：当前 `dashboard.js`（HTML 实际引用）自行解析合约（parseSymbol）、前端聚合三层（render_positions 内分组），违反红线三。应改用 `dashboard11.js` 纯渲染器，HTML 引用同步切换。

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

> L1/L2 聚合节点的 `deltacash_tag`/`gammacash_tag`/`pnl_tag` 用聚合后的绝对值，套用同一组阈值。

---

## 十、基线已知缺口清单（Gaps at Baseline）

### v1.5 新增缺口（2026-09-18）

| 编号 | 严重度 | 缺口描述 | 涉及文件 | 修正方向 | 状态 |
|------|--------|---------|---------|---------|------|
| **B7** | P0 | `_register_trade_event` 用 `eng.event_engine`，而 `event_engine` 只是 `VNPYEngine.run()` 的局部变量（挂在 `MainEngine` 上）→ 每次连接抛 `AttributeError` 被 `except` 吞成 WARNING，**EVENT_TRADE 从未注册成功**：成交账本、已实现盈亏、今开加权成本、平仓成本阶梯全链路是死代码，当日已实现恒 0 | api_server.py `_register_trade_event` | 改 `eng.main_engine.event_engine.register(EVENT_TRADE, _on_trade)` + `main_engine` 未就绪守卫 | ✅ **已修**（2026-09-18 13:25 重启后日志出现 `EVENT_TRADE 注册成功`，为历史首次） |
| **B6** | P1 | 昨仓与今开并存的**混合腿**只用一个基准：档 1/2 命中时昨收/昨结基准覆盖全部手数，未按今昨手数拆分，`pnl_today` 存在系统性偏差 | risk_engine.py `calc_pnl` | 按账本 `vol_open` 与昨仓手数拆两段（昨仓段用昨收、今开段用开仓价）后相加；在未拆之前保持 `+today_open_mixed` 标注 | **待修**（已有标签与告警，数值待 09-19 对表后定优先级） |
| **B8** | P2 | 前端列配置双源：`loadColConfig` 优先读 `localStorage['col_config']`，只在缺失时才拉 `/api/columns`。删除 `pnl_daily` 后，**浏览器里存过旧 18 列配置的用户会继续渲染一个恒空的「盯日盈亏」列**，且新增列不会出现（服务端默认列已改，本地配置覆盖它） | static/dashboard11.js `loadColConfig` | 读本地配置后按服务端列白名单过滤（或按版本号失效本地配置） | **待修**（用户端临时解法：清 `localStorage.col_config`） |

> **当日运维记录（非代码缺口）**：`快照/` 下无 `close_snapshot_20260917.json`（9-17 15:00 服务未运行）→ 2026-09-18 全账户 34 腿走 `prev_settlement_fallback`，快照基准 0 腿。09-18 15:00 起若服务在跑即产出首份 `close_snapshot_20260918.json`，09-19 起以昨收基准对表老系统。
> 另有 3 腿 `price_basis=none`（IF2610、MO2610-P-7500、sc2611P700）：今日开仓但成交发生在账本启用（13:25）之前，当日无基准。**用户裁决不补录**，次日由结算单接管。

### v1.4 新增缺口（2026-09-18）

| 编号 | 严重度 | 缺口描述 | 涉及文件 | 修正方向 | 状态 |
|------|--------|---------|---------|---------|------|
| **B4** | P1 | 期货腿无独立 Mark：`price_options_batch` 只喂期权符号，`option_ticks` 里期货 bid/ask 被写 0（L723-724），`positions_out` 的 `adjust_price = adj or last_price` → 期货 Mark 恒等于末成交价。远月非主力无成交时基准失真 | api_server.py | 补期货 bid/ask + `calc_future_mark`（mid → 单边 → last → pre_close）；或更准的**盘中结算模式**（稀疏度阈值 + 时间距离衰减加权均价） | **用户裁决暂不修**（影响有限），将来随期货 Mark 一起做 |
| **B5** | P2 | `load_costs_from_meta` 每轮 poll 重解析 97KB 结算单 JSON + 基准快照，无 mtime 缓存 | settlement.py / api_server.py | 按 mtime 缓存；快照基准按 TradingDay 缓存 | **部分已修**（v1.4：`_load_yesterday_snapshot` 按业务日缓存 `_BASE_CACHE`）；结算单侧 `load_costs_from_meta` 仍待修 |

### v1.1 新增缺口（2026-09-14）

| 编号 | 严重度 | 缺口描述 | 涉及文件 | 修正方向 | 状态 |
|------|--------|---------|---------|---------|------|
| **B1** | P0 | `build_tree` L291 每个 L3 节点先进 l1_metrics，L297-298 L2（含全部L3）又进 l1_metrics → L1 Greeks 是真实值 2× | risk_engine.py | 删 L291 的 `_accumulate_metrics(l1_metrics, node)`，只保留 L309 L2 进 L1 | 修复中/待数值验证 |
| **B2** | P0 | `pricing.py` L343 gammacash 写 `s*s*0.01*0.01*size`（多乘 0.01²），实际 Gamma cash 缩小 100 倍 | pricing.py | 删一个 `*0.01` | 修复中/待数值验证 |
| **B3** | P1 | 结算单缺 09-11/09-14；无结算单时 `open_price`(=cost_price)=0 → `pnl_history` 失真（原描述里的 `pnl_daily` 已随 v1.5 删除该口径） | settlement.py / api_server.py | 结算单路径用绝对路径；无结算单时降级到账本今开加权价 / `position.price`（v1.5 已实现 `cost_basis` 降级链），并保留告警 | **降级链已修**，结算单历史缺口（09-11/09-14）不补 |

### 原 v1.0 缺口状态

| 编号 | 严重度 | 原描述 | 状态 |
|------|--------|--------|------|
| **G1** | P0 | `engine` 方法名错误（is_connected 等不存在） | ✅ FIXED |
| **G2** | P0 | `load_settlement_sync` 扫描 `parsed_*.json` | ✅ FIXED（SettlementManager 增量同步） |
| **G3** | P1 | `calc_adjust_price` 仅占位 | ✅ FIXED（四级规则已移植） |
| **G4** | P1 | `dashboard.js` 正则解析与三层聚合 | ✅ FIXED（切到 dashboard11.js） |
| **G5** | P1 | `/api/dashboard` 未使用 tree | ✅ FIXED |
| **G6** | P2 | ITM PCP 串行计算拓扑序 | ⚠️ 部分实现（需实际数据验证嵌套递归） |

---

## 十一、不掉线不 close 原则（v1.1 新增）

> **背景**：`CtpTdApi.exit()` 在 Win32 环境下持有 GIL 阻塞调用，整个 Python 解释器会冻死（端口仍 LISTENING、CPU 0%、join 超时/watchdog 均无效）。

**原则**：运行期**永远不调用** `engine.close()`、`eng.close()`、`CtpTdApi.exit()`。连接状态丢失时：

```python
# 正确做法：掉线 → 丢弃旧引擎，不 close
_engine = None          # 解除引用，旧引擎的 C++ 对象由 GC 回收
break                    # 跳出内层循环，回到外层重连阶梯建新引擎

# 错误做法：永远不要
eng.close()              # ❌ 会冻死解释器
CtpTdApi.exit()         # ❌ 同上
```

唯一允许调 close 的场景：`atexit` 进程退出时，由主进程执行一次。

---

## 十二、文件结构与实施计划（唯一权威目录）

```
C:/Quant_2026/期货执行策略/GreeksDashboard_v0.1/   (= C:/qproj/ 软链接)
├── Dashboard_设计基线.md          # 基线文档 v1.5（唯一权威）
├── Dashboard_设计基线_审查报告.md  # v1.0 审查报告
├── Dashboard_重构设计.md           # 主设计文档 v4
├── Dashboard_维护清单.md           # 维护清单 v1.1（2026-09-14）
├── run_server.py                   # 服务入口
├── ctp_accounts.json               # 多账户凭证
├── vnpy_engine.py                  # CTP 引擎封装
├── 结算单/                          # 结算单 JSON（full_YYYYMMDD.json）
├── 快照/                            # 自动快照 + 成交账本目录
│   ├── close_snapshot_{TradingDay}.json   # 每业务日 15:00 收盘快照（基准来源，§三-B）
│   └── trade_ledger.json                  # 当日成交账本（每笔成交原子重写，§3.8）
├── dashboard_v2/
│   ├── api_server.py               # Flask + Worker 双线程（不掉线不close）
│   ├── pricing.py                  # Black-76 Greeks + IV反推 + 四级adjust_price
│   └── settlement.py               # 结算单单路径VWAM
│   └── risk_engine.py              # Greeks + PnL + 树形聚合 + 标签
├── static/
│   └── dashboard11.js              # 前端纯渲染器（17列，COL_DEF单一源；v1.5 删「盯日盈亏」列）
├── config/
│   └── thresholds.json             # 阈值单一源（delta/gamma/pnl 打标），代码与前端共享
├── selfcheck_v131.py               # 离线自检（49 项：快照/PnL 基准/账本重放/切日/符号）
├── tools/
│   └── convert_snapshots.py        # 旧快照迁移（v1.5 起不再输出 pnl_daily）
├── docs/
│   ├── pnl_today_设计.md            # DEPRECATED（实现前设计稿，权威口径见 §3.7）
│   └── 验收测试用例.md              # 验收场景清单（16 场景，v1.5 已按新口径改写）
└── templates/
    └── dashboard.html               # HTML 模板
```
