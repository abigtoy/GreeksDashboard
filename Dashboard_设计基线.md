# VolTrading Dashboard — 设计基线（Baseline）

> **基线版本**：v1.3-baseline
> **建立日期**：2026-09-11
> **最后更新**：2026-09-17（PnL 增强版合并 + 审查意见逐条修正）
> **状态**：审查后修订（本次修正 P0-1~10 / P1-1~12 / P2-1~4 共 26 条问题）
> **用途**：作为后续重构的唯一权威参考。本文档已吸收《Dashboard_重构设计.md》v4 的全部内容，并合并了旧版 `DESIGN.md` 中缺失的 IV 链路与 adjust_price 逻辑，同时标注了命名统一修正与已知缺口。
>
> **v1.3 更新说明（2026-09-17）**：
> - P0-1~10 / P1-1~12 / P2-1~4 共 26 条审查问题逐条修正，详见审查意见 `审查意见260917.md`
> - 3.7 PnL 公式重写（补全 direction_sign、守恒逻辑、已实现通道）
> - 3.5 标记 DEPRECATED（仅保留签名，删除正文）
> - trade_cache 账本模型重定义（分组键/去重键/OffsetFlag 归因）
> - 结算成本双策略优先级修正（汇总为主，明细兜底）
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
| `vnpy接口封装_backup_20260910/DESIGN.md` | 2026-09-08 | IV 计算链路（adjust_price → bisection → tick['iv']）、adjust_price 三级规则、PnL 三口径定义、CFFEX 映射、合约乘数表 | 高（历史实现记录，含已验证修复） |
| `dashboard_v2/pricing.py` | 当前代码 | Black-76、`implied_vol_bisection`、`price_options_batch`、字段命名（`days_to_expiry`） | 中（实现与文档有偏差，以本文档修正后为准） |
| `dashboard_v2/risk_engine.py` | 当前代码 | `calc_greeks` / `calc_pnl` / `build_tree` / `calc_adjust_price` 签名与字段 | 中（含占位实现，待补全） |
| `dashboard_v2/settlement.py` | 当前代码 | `SettlementManager`（增量同步 + 日期标记 + CTP 下载）、`load_settlement_cost`（双策略VWAP） | 高（已实现） |
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

### 2.2 双策略加载（汇总优先，明细兜底）

```python
from collections import defaultdict

def load_settlement_cost(settlement_json: dict) -> dict:
    settlement_dict = {}

    # ========== 策略1: 嗅探券商预计算的汇总均价（优先）==========
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

    # ========== 策略2: 逐笔明细加权自算兜底（仅填充策略1未覆盖的 key）==========
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
                # 仅当策略1未写入时才自算兜底（严禁直接覆写）
                if key not in settlement_dict:
                    detail_calc[key]["total_cost"] += p * v
                    detail_calc[key]["total_vol"] += v
        except (ValueError, TypeError):
            continue

    for key, data in detail_calc.items():
        if data["total_vol"] > 0:
            settlement_dict[key] = round(data["total_cost"] / data["total_vol"], 4)

    return settlement_dict
```

> ⚠️ **严禁单条直接覆写**：策略2仅填充策略1未覆盖的 key；策略1已存在的 key **不被策略2覆写**。两个策略的结果差异应产生告警。

**方向映射**：`'买'/'多'/'B'/'1'/'Buy'` → `'多'`；其余 → `'空'`

> ⚠️ **未知方向拒绝入账**：方向字段无法映射到 `'多'` 或 `'空'` 时（如空字符串、未知值），该条记录**拒绝入账**，不降级不兜底，并记录告警日志。严禁将其归入任意方向。
>
> **汇总/明细优先级**：
> 1. 汇总（`positions_summary`/`positions`）**优先**：若有汇总数据，直接使用汇总均价
> 2. 明细（`positions_detail`）**兜底**：仅在汇总缺失该合约时，才用明细 VWAP 自算
> 3. 若汇总与明细的同一合约价格差异超过 ±5%，记录差异告警（`warning: settlement_summary_detail_mismatch`）

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

#### adjust_price 四级规则（来源：旧版 DESIGN.md §四.5，当前代码仅为占位，见缺口 G3）

1. **正常流动性合约**：mid_price 或 last_price
2. **深度实值（ITM）**：PCP 平价公式 + OTM 腿时间价值反推
3. **深度虚值（OTM）/ 盘口宽价差**：微观盘口挂单量 + 动态价差过滤
4. **兜底**：last_price 为空 → pre_close → 上一快照

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

### 3.5 PnL 三口径计算

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

### 3.7 PnL 三口径计算（增强版：今仓/老仓双基准）

> **来源**：本节内容由 `docs/pnl设计.md` 合并入基线，并经审查意见修正 P0-1~4 后作为唯一正式规范。替代原 3.5 基础版。

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

#### 基准价优先顺序

| | 持仓类型 | pnl_today 基准价 | pnl_history cost_price |
|---|---|---|---|
| 老仓 | 昨仓今持、昨仓今平 | 昨快照 adjust_price（T-1 收盘 Mark） | settlement_cost_dict[`{sym}_{direction_str}`] |
| 老仓降级 | — | T-1 结算单 settlement_price | **无降级** |
| 今仓 | 今开、今平 | 成交回报 P_trade_open（成交量加权均价 VWAP） | 成交回报 P_trade_open |
| 今仓降级 | — | **无**（T-1 结算单不存在该合约） | **无** |

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

#### calc_pnl 增强版函数签名

```python
def calc_pnl(position: dict, contract: dict, tick: dict,
             settlement_dict: dict, settlement_prices: dict,
             yesterday_snapshot: dict, trade_cache: dict = None) -> dict:
    """请通过 build_tree 统一调用，不要单独调用本函数。
    
    持仓 tree 由 Worker 的 build_tree 统一构建，已平合约的 realized PnL
    由 _realized_pnl_cache 单独聚合，不通过本函数返回。
    """
    sym = position['symbol'].split('.')[0]
    direction_str = '多' if position['direction'] in ('long', '多') else '空'
    direction_sign = 1 if direction_str == '多' else -1
    vol = abs(position['volume'])
    size = contract.get('size', 1)
    signed_qty = direction_sign * vol

    # 真实开仓成本：结算单 VWAP，key 必须带方向维度
    cost_price = settlement_dict.get(f"{sym}_{direction_str}", position.get('price', 0.0))
    if cost_price == 0.0:
        cost_price = position.get('price', 0.0)

    adj_price  = tick.get('adjust_price', tick.get('last_price', 0))
    last_price = tick.get('last_price', 0)

    # pnl_history
    pnl_unrealized = direction_sign * (adj_price - cost_price) * vol * size
    pnl_history = pnl_unrealized  # realized 由 trade_cache 单独累加到 summary

    # pnl_today
    base_today = cost_price
    if yesterday_snapshot:
        prev = yesterday_snapshot.get(f"{sym}_{direction_str}", {})
        if prev:
            base_today = prev.get('adjust_price', cost_price)
    pnl_today = direction_sign * (adj_price - base_today) * vol * size

    return {
        "pnl_daily":   round(direction_sign * (last_price - base_today) * vol * size, 2),
        "pnl_today":   round(pnl_today, 2),
        "pnl_history": round(pnl_history, 2),
    }
```

> **注意**：结算单中的 `prev_sttl_price`（昨结算列）**永远不使用**。

### 3.8 CTP 成交回报（进程内账本）

#### 账本分组 Key（不含 offset）

> ⚠️ `offset`（开/平）不能放入分组 Key，否则同一持仓方向的开仓和平仓记录会被割裂到不同组，无法共同参与 PnL 计算。

```python
_ledger_key = (trading_day, account, exchange, symbol, position_direction)
# position_direction = '多'（原持仓为多，平卖也是"空头平仓"，但 group key 用原持仓方向）
# 注意：分组键中不含 offset；offset 保留在每条成交记录中用于归因
```

#### 去重 Key

```python
_dedup_key = (trading_day, account, exchange, trade_id)
# 每条成交记录到达时，先查 _seen_trade_ids
# 若已存在：幂等跳过，不重复处理
# 若不存在：写入 _seen_trade_ids，继续处理
```

#### 每条成交记录格式

```json
{
  "dedup_key": "20260915_101009_CFFEX_278350",
  "ledger_key": ["20260915", "101009", "CFFEX", "MO2609-P-7000", "多"],
  "trade_id": "278350",
  "symbol": "MO2609-P-7000",
  "direction": "买",
  "position_direction": "多",
  "open_close": "开",
  "offset_flag": "open",
  "price": 11.600,
  "volume": 1,
  "trade_time": "20260915 09:30:12",
  "account": "101009",
  "exchange": "CFFEX",
  "trading_day": "20260915"
}
```

> **字段说明**：
> - `position_direction`：原持仓方向（多/空），由第一条成交记录决定，后续同组记录沿用
> - `offset_flag`：CTP 原生字段，`open` / `close_today` / `close_yesterday`；缺失时由 `open_close` 推断并标注 `allocation_source: "fifo_fallback"`
> - `dedup_key`：用于幂等去重，确保重连重放不重复计入

#### onRtnTrade 回调（幂等版）

```python
_seen_trade_ids: set = set()

def onRtnTrade(self, trade: dict):
    trading_day = trade.get('trading_day', '')
    account     = trade.get('account_id', '')
    exchange    = trade.get('exchange', '')
    trade_id    = trade.get('order_sys_id', '')

    dedup_key = (trading_day, account, exchange, trade_id)
    if dedup_key in _seen_trade_ids:
        return  # 幂等去重

    symbol = trade['instrument_id'].split('.')[0]
    raw_dir = trade.get('direction', '')
    position_direction = '多' if raw_dir in ('long', '买') else '空'

    # offset_flag：优先取 CTP 原生字段，缺失时推断
    offset_flag = trade.get('offset_flag', '')
    if not offset_flag:
        is_open = is_open_trade(trade)
        offset_flag = 'open' if is_open else 'close_yesterday'
        allocation_source = 'fifo_fallback'
    else:
        allocation_source = 'ctp_offset'

    record = {
        'dedup_key': f"{trading_day}_{account}_{exchange}_{trade_id}",
        'ledger_key': [trading_day, account, exchange, symbol, position_direction],
        'trade_id': trade_id,
        'symbol': symbol,
        'direction': raw_dir,
        'position_direction': position_direction,
        'open_close': '开' if offset_flag == 'open' else '平',
        'offset_flag': offset_flag,
        'allocation_source': allocation_source,
        'price': trade['price'],
        'volume': trade['volume'],
        'trade_time': trade.get('trade_time', ''),
        'account': account,
        'exchange': exchange,
        'trading_day': trading_day,
    }
    _seen_trade_ids.add(dedup_key)
    _trade_cache.setdefault(tuple(record['ledger_key']), []).append(record)
```

#### 持久化行为

> **trade_cache 持久化策略（唯一正式行为）**：
> - `session_state.json` 保存 `_trade_cache` 完整内容（含所有历史成交记录）
> - 服务重启后：从 `session_state.json` 恢复，视为"昨仓降级"场景
> - 今仓基准（成交均价 VWAP）：**重启后丢失，归零处理**；这是受控降级，不是"持久化后完整恢复"

#### 3.8.2 已实现 PnL 实时累加

```python
_realized_pnl_cache: dict[str, float] = {}  # key = symbol，value = 累计 realized PnL

def onRtnTrade(self, trade: dict):
    # ... 上述处理 ...
    if offset_flag != 'open':
        # 计算已实现 PnL
        sym = symbol
        direction_sign = 1 if position_direction == '多' else -1
        pnl_realized = direction_sign * (trade['price'] - cost_price) * trade['volume'] * size
        _realized_pnl_cache[sym] = _realized_pnl_cache.get(sym, 0) + pnl_realized
```

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
| 持仓快照（positions_summary + adjust_price） | ✅ | 服务重启后恢复 |
| 结算成本（settlement_cost_dict） | ✅ | 本地文件，已实现 |
| CTP成交回报（trade_cache） | ✅ | 今仓基准，重启后丢失只能归零 |
| 持仓合约开盘状态（opened_contracts） | ✅ | 重启后恢复哪些合约已开盘 |
| 实时行情（tick） | ❌ | 随时变化，重启后重新接收 |

### 3.A session_state.json 持久化结构与重启恢复

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
  "positions_summary": { ... }
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
| `settlement_price`（T-1） | full_{T-1}.json | 老仓 pnl_today 降级基准 |
| `settlement_cost` | settlement_cost_dict | 老仓 pnl_history cost_price |
| `prev_sttl_price` | full_*.json | **不使用** |
| `P_trade_open` | CTP onRtnTrade | 今仓 pnl_today 和 pnl_history 基准 |
| `P_trade_close` | CTP onRtnTrade | 平仓盈亏计算 |

---

## 三-B、快照引擎业务规则

> **来源**：快照引擎业务规则合并入基线（2026-09-17）。

### 三-B.1 快照时间表

| Session | 文件名 | 含义 |
|---------|--------|------|
| N（夜盘） | `data_snapshot_{date}_N.json` | 上一交易日 20:20 → 当日 15:00 截面 |
| A（早盘） | `data_snapshot_{date}_A.json` | 当日早盘 09:00 → 11:30 截面 |
| P（午盘） | `data_snapshot_{date}_P.json` | 当日午盘 13:00 → 15:00 截面 |

同一交易日（上一日 20:20 → 次日 16:00）数据**连续继承**，交易日变更时数据重置。

### 三-B.2 保存逻辑（核心规则）

**触发方式：持续检测 + 定时保存**

1. **Dirty Flag 检测**（每个合约级别）：
   - `_poll_once` 每轮检测：若任一合约的持仓量、adjust_price 或 Greeks 与上一份快照有差异，标记 dirty
   - Dirty 合约积累到一定程度 → 全局 dirty flag = true

2. **每 30 分钟定时保存**：
   ```
   若 dirty == true:
       保存当前状态到 data_snapshot_{date}_{session}.json（覆盖式）
       dirty = false
   若 dirty == false:
       不写入（节省磁盘，无变化不写）
   ```

3. **覆盖式而非追加式**：同一 session 只有一份文件，每次保存是整体覆盖，不是追加

4. **空仓快照规范写入**：
   ```
   即使当前持仓为空（volume == 0），也必须写入快照文件：
   - 文件内容包含 snapshot_kind = "empty"
   - 不另建 _empty 后缀文件，统一写入同名 snapshot 文件
   - 目的：清除前一个 session 的非空数据，确保 T+1 开盘基准干净
   ```

5. **原子写（防文件损坏）**：
   ```
   标准 POSIX 原子替换流程：
   1. 写入 tempfile（os.path.dirname + ".tmp_" + os.path.basename）
   2. flush + fsync（确保内核缓冲区落盘）
   3. os.replace(tempfile, target)（原子替换）
   4. 若写失败：记录日志，不抛异常，不阻塞主流程
   ```

6. **TradingDay 绑定**：
   ```
   快照日期强制使用 CTP TradingDay，禁止使用本机 clock。
   Worker 在交易日切换时（T-1 15:00 后或 20:20 后）清空 opened_contracts 和 trade_cache，
   生成新的 session 文件。
   ```

7. **发布后禁止写入（Immutable Snapshot）**：
   ```
   _snapshot = copy.deepcopy(tree)  # Worker 在发布前深拷贝
   快照一旦写入 _shared_state["snapshot"]，禁止任何代码再修改其内容。
   所有后续计算必须基于新 tick 数据增量更新，不得原地修改已发布的快照对象。
   ```

### 三-B.3 快照文件命名

```
data_snapshot_{YYYYMMDD}_{session}.json
# 示例：data_snapshot_20260916_N.json
```

- 同一交易日共用同一 base date（如 0916 的 N/A/P 三份快照都叫 20260916）
- session 变更时文件名 session 段变化，内容整体继承上一份

### 三-B.4 跨 Session 继承

```
N → A → P（同一交易日内连续）
T-1_P → T_N（跨交易日，清空 opened_contracts 和 trade_cache）
```

- 持仓数量、adjust_price、pnl_today 等值在 N→A→A→P 过程中逐步更新
- 切换交易日时（TradingDay 变化），opened_contracts 和 trade_cache 必须清空

### 三-B.5 例外处理与容错

| 情况 | 处理 |
|---|---|
| 空仓（volume == 0） | **必须写入快照**（含 `snapshot_kind: "empty"`），不清空则 T+1 开盘基准不干净 |
| 服务异常退出 | 上一次完整写入的快照即为恢复基准 |
| 磁盘写失败 | 记录日志，不抛异常，不阻塞主流程 |
| 快照中 adjust_price = None | 使用 position.price 兜底 |
| TradingDay 切换 | 清空 opened_contracts + trade_cache，session 重置 |

---

## 四、数据结构

### 4.1 树形三层规则（基线统一定义）

| 层级 | 含义 | key 规则 | name 显示 |
|------|------|---------|----------|
| **L1 品种** | 包含所有到期日的期货以及衍生的期权 | 品种代码（一般2字母，少数1字母），如 `IF`/`IM`/`IH`/`CU` | 如"沪深300（IF）" |
| **L2 月份** | 该品种下某到期月份 | `{品种}_{月份}`，如 `IF_2609` | 如"2609月份" |
| **L3 合约** | 该品种该月份下所有期货/期权合约明细 | `{合约}_{方向}`，如 `IF2609_多` | 合约symbol |

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
      "volume": 22, "deltacash": -422100, "deltacash_tag": "red",
      "gammacash": -40536, "gammacash_tag": "yellow",
      "vegacash": -460, "thetacash": 160,
      "pnl_daily": 600, "pnl_today": 120, "pnl_history": 9600, "pnl_tag": "green"
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
          "pnl_daily": 600, "pnl_today": 120, "pnl_history": 9600, "pnl_tag": "green"
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
            "delta": 1.0, "gamma": 0.0, "vega": 0.0, "theta": 0.0,
            "deltacash": 2538000, "gammacash": 0, "vegacash": 0, "thetacash": 0,
            "days_to_expiry": null, "itm": false,
            "pnl_daily": 800, "pnl_today": 200, "pnl_history": 1200,
            "delta_tag": "green", "gamma_tag": "green", "pnl_tag": "green"
          },
          {
            "key": "IO2609-C-4000_空",
            "symbol": "IO2609-C-4000",
            "direction": "空",
            "direction_raw": "short",
            "volume": 20,
            "last_price": 45.2, "adjust_price": 44.8, "open_price": 38.5,
            "underlying_price": 4230.0, "iv": 16.5,
            "delta": -0.35, "gamma": -0.0012, "vega": 0.023, "theta": -0.008,
            "deltacash": -2960100, "gammacash": -40536, "vegacash": -460, "thetacash": 160,
            "days_to_expiry": 25, "itm": false,
            "pnl_daily": -200, "pnl_today": -80, "pnl_history": 8400,
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
│   │   ├── settlement.py   # 结算单双策略加载
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

### v1.1 新增缺口（2026-09-14）

| 编号 | 严重度 | 缺口描述 | 涉及文件 | 修正方向 | 状态 |
|------|--------|---------|---------|---------|------|
| **B1** | P0 | `build_tree` L291 每个 L3 节点先进 l1_metrics，L297-298 L2（含全部L3）又进 l1_metrics → L1 Greeks 是真实值 2× | risk_engine.py | 删 L291 的 `_accumulate_metrics(l1_metrics, node)`，只保留 L309 L2 进 L1 | 修复中/待数值验证 |
| **B2** | P0 | `pricing.py` L343 gammacash 写 `s*s*0.01*0.01*size`（多乘 0.01²），实际 Gamma cash 缩小 100 倍 | pricing.py | 删一个 `*0.01` | 修复中/待数值验证 |
| **B3** | P1 | 结算单缺 09-11/09-14；无结算单时 open_price=0，pnl_daily 错误 | settlement.py / api_server.py | 结算单路径用绝对路径；无结算单时 settle_key=0 | 待修 |

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
├── Dashboard_设计基线.md          # 基线文档 v1.1
├── Dashboard_设计基线_审查报告.md  # v1.0 审查报告
├── Dashboard_重构设计.md           # 主设计文档 v4
├── Dashboard_维护清单.md           # 维护清单 v1.1（2026-09-14）
├── run_server.py                   # 服务入口
├── ctp_accounts.json               # 多账户凭证
├── vnpy_engine.py                  # CTP 引擎封装
├── 结算单/                          # 结算单 JSON（full_YYYYMMDD.json）
├── 快照/                            # 自动快照输出目录
├── dashboard_v2/
│   ├── api_server.py               # Flask + Worker 双线程（不掉线不close）
│   ├── pricing.py                  # Black-76 Greeks + IV反推 + 四级adjust_price
│   ├── settlement.py               # 结算单加载（full_*.json，双策略）
│   └── risk_engine.py              # Greeks + PnL + 树形聚合 + 标签
├── static/
│   └── dashboard11.js              # 前端纯渲染器（18列，COL_DEF单一源）
└── templates/
    └── dashboard.html               # HTML 模板
```
