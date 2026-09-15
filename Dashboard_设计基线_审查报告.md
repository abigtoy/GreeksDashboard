# Dashboard 设计基线 v1.0 — 审查报告

> **审查日期**：2026-09-13
> **审查对象**：[Dashboard_设计基线.md](file:///C:/Quant_2026/期货执行策略/GreeksDashboard_v0.1/Dashboard_设计基线.md)
> **审查方法**：文档自洽性审查 + 代码交叉验证（已核对 6 个源文件 + 目录结构）

---

## 一、总体评价

这是一份**质量很高的基线文档**，具备以下优点：

- ✅ 来源溯源清晰（§〇 资料来源表），每条设计决策可追溯
- ✅ 架构数据流图直观完整
- ✅ 核心算法给出完整可运行的伪代码（Black-76、IV bisection、PnL 三口径）
- ✅ API Schema 契约用 JSON 样例明确定义
- ✅ 已知缺口清单（G1-G6, C1-C2）坦诚标注，有明确修正方向
- ✅ 四条红线要求清晰具体，可直接作为 code review checklist

**但经代码交叉验证，发现若干需要修正或补充的问题，按严重度分类如下。**

---

## 二、缺口验证结果（文档声明 vs 代码实况）

### ✅ 已修复的缺口（文档声明已过时）

| 缺口 | 文档声明 | 代码实况 | 建议 |
|------|---------|---------|------|
| **G1** (部分) | `pricing.py` 的 `price_options_batch` 接收 `engine` 参数，违反红线二 | **已修复** ✅ 当前签名为 `price_options_batch(symbols, option_ticks, settlement_data)`，无 engine 参数 | 更新文档，标记 G1 中 pricing.py 部分为 **已关闭** |
| **G2** | `settlement.py` 扫描 `parsed_*.json` | **已修复** ✅ 当前代码扫描 `full_*.json`（第 181/225 行），且结算单目录确实全部为 `full_*.json`（40 个文件） | 标记 G2 为 **已关闭** |
| **G4** (部分) | HTML 引用 `dashboard.js` 做前端聚合 | **已修复** ✅ `dashboard.html` 第 322 行引用 `dashboard11.js`；`static/` 目录仅含 `dashboard11.js`，无 `dashboard.js` | 标记 G4 中 HTML 引用部分为 **已关闭** |

> [!IMPORTANT]
> **文档§八和§十中对 G1/G2/G4 的缺口描述已过时**，代码已经修复了这些问题。建议在基线文档下一版本中将这些缺口标记为"已关闭"，避免后续开发者被误导去重复修复。

### ⚠️ 仍然存在的缺口（经代码确认）

| 缺口 | 代码验证结果 |
|------|-------------|
| **G1** (api_server.py 部分) | ✅ 确认已修复 — `api_server.py` 实际使用 `engine.query_positions()`（L269）、`engine.query_tick()`（L310/321/334/365）、`engine.query_account()`（L387），与 `vnpy_engine.py` 实际方法一致 |
| **G3** | ✅ 确认仍存在 — `calc_adjust_price` 仍为占位实现（仅 `last_price` → `pre_close` 兜底，无 ITM/OTM/PCP 逻辑） |
| **G5** | 需进一步验证前端是否已消费 `tree` 结构 |
| **G6** | Worker 的 IV 反推两阶段拆分状态需进一步验证 |

---

## 三、文档内部问题

### 3.1 🔴 严重问题

#### P1: `pos_theta` 符号处理不一致

**§3.4 代码第 245 行**：
```python
pos_theta = -g['theta'] * vol if direction_sign == -1 else g['theta'] * vol
```

这段逻辑**与 `pos_delta`/`pos_gamma`/`pos_vega` 的统一模式 `= g[x] * direction_sign * vol` 不一致**。

- 对于 `direction_sign = -1`（空头）：`pos_theta = -g['theta'] * vol`（取反了 theta 但没乘 direction_sign）
- 对于 `direction_sign = 1`（多头）：`pos_theta = g['theta'] * vol`

**问题**：如果 theta 值本身为负（期权 time decay 通常为负），卖方（空头）的 pos_theta 应为正（收取时间价值），但这段代码对空头做了 `-g['theta'] * vol` = 正值 × vol = 正值，看起来结果正确。但写法打破了 `direction_sign *` 的统一范式，容易引发维护混淆。

**建议**：明确注释说明为何 theta 需要特殊处理，或统一为 `pos_theta = g['theta'] * direction_sign * vol`（需验证与 Black-76 theta 符号约定是否吻合）。

#### P2: `calc_pnl` 中 `settle_price` 取值来源可疑

**§3.5 代码第 281 行**：
```python
settle_price = float(position.get('price') or 0.0)
```

文档注释称这是"昨日结算价"，但 `position['price']` 在 VNPY 中通常是**开仓均价**或**持仓成本**，并非昨日结算价。VNPY `PositionData` 的结算价字段应为 `yd_volume` 相关或需从 tick 数据获取。

**建议**：明确 `position['price']` 在 VNPY 语境下的确切含义，如果确实是结算价则添加注释；如果不是，需修正为正确的结算价来源。

#### P3: Black-76 与 `implied_vol_bisection` 的 IV 单位约定不统一

- `black76()` 函数接受 IV 后内部判断：`v = IV / 100.0 if IV > 1.0 else max(IV, 0.001)` — 即**同时兼容百分比和小数**
- `implied_vol_bisection()` 返回的是**小数形式**（搜索范围 0.001~5.0）

**问题**：如果 `implied_vol_bisection` 输出 0.165（16.5%），传入 `black76(0.165, ...)` 时因为 `0.165 < 1.0` 不会除以 100，正确。但如果某处传入 `16.5`（百分比形式），则 `16.5 > 1.0` 会被除以 100 变成 `0.165`，也正确。

这个**隐式转换是一个定时炸弹**——代码依赖一个假设：IV 值要么是 <1 的小数，要么是 >1 的百分比。但 IV = 1.0（即 100%）是一个边界值，此时条件 `IV > 1.0` 为 False，会被当作小数处理（实际应为 100%）。

**建议**：在基线文档中明确规定 IV 的唯一标准单位（推荐**小数形式**），并在 `black76()` 入口强制检查而非自动猜测。

---

### 3.2 🟡 中等问题

#### M1: 商品期权乘数缺失

§3.1 给出了商品**期货**乘数（cu=5, au=1000 等），但**没有给出商品期权乘数**。表头有"期权乘数"列但内容为"各品种固定乘数"——这不是一个可实施的规格。

**建议**：补全商品期权乘数表，或注明与期货乘数相同（如果确实如此）。

#### M2: `pnl_today` 的昨日基准取值链不完整

§3.5 中 `pnl_today` 定义：
```python
base_today = cost_price
if yesterday_snapshot:
    prev = yesterday_snapshot.get(f"{sym}_{direction_str}", {})
    if prev:
        base_today = prev.get('adjust_price', cost_price)
```

但文档**未定义 `yesterday_snapshot` 的加载机制和数据结构**。它是从哪里来的？是保存的 JSON 快照还是内存缓存？首日无快照时的行为是否符合预期（退化为 cost_price）？

**建议**：在 §2 或新增章节中定义 `yesterday_snapshot` 的生成、存储和加载流程。

#### M3: 树形聚合中 `volume` 使用绝对值，但文档与代码可能有歧义

§3.6 定义 `volume = Σ|pos.volume|`（绝对值），但 L3 节点的 `volume` 是原始持仓量（有方向），而 L1/L2 的 `volume` 是绝对值加总。这种层级间语义不同的同名字段容易造成前端显示混淆。

**建议**：考虑 L1/L2 使用 `total_volume` 或在 Schema §4.2 中明确标注。

#### M4: 文件路径表（§七）与实际目录不一致

§七 文件结构写的路径是：
```
C:/Quant_2026/期货执行策略/vnpy接口封装/
```
但实际项目目录是：
```
C:/Quant_2026/期货执行策略/GreeksDashboard_v0.1/
```
且代码文件实际在 `dashboard_v2/` 子目录下（如 `dashboard_v2/pricing.py`），而非直接在根目录。

**建议**：更新 §七 的路径为实际路径。

---

### 3.3 🟢 轻微问题 / 改进建议

| 编号 | 问题 | 建议 |
|------|------|------|
| L1 | §3.2 adjust_price 规则列了"1/2/3/4"四级但标题写"三级规则" | 改标题为"四级规则"或合并 3+4 为一级 |
| L2 | §3.3 `implied_vol_bisection` 参数顺序与 Black-76 标准文献不同（price, F, K, T vs 通常 F, K, T, price） | 不影响正确性，但建议加注释说明参数含义 |
| L3 | §4.2 Schema 示例中 `itm: false` 对期货合约没有意义 | 建议期货节点省略 `itm` 字段或设为 `null` |
| L4 | §3.3 bisection 兜底返回 `0.20` 但 §3.2 写"兜底用同到期日 ATM IV 中位数" | 两处兜底策略不一致，需统一 |
| L5 | §九 阈值打标的 `tag_pnl` 逻辑：`pnl < -50000` → red，`pnl < 0` → yellow，但 L1/L2 聚合后 PnL 可能很大，阈值是否需要区分层级？ | 建议讨论是否需要 L1 级别更高的阈值 |
| L6 | §2.2 策略2覆写策略1 的行为（`settlement_dict[key] = ...` 在循环末尾直接覆盖）——如果 detail 数据不完整，可能反而丢失 summary 的准确值 | 建议改为：detail 结果仅在 key 不存在于 settlement_dict 时才写入（"兜底"语义） |

---

## 四、架构设计层面的审查意见

### 4.1 ✅ 优秀设计决策

1. **单进程双线程 + 原子快照交换**：避免了多进程 IPC 复杂性，适合这个量级的实时系统
2. **后端预聚合、前端纯渲染**：职责分离彻底，前端代码可以保持极简
3. **结算单双策略加载**：先嗅探汇总、再逐笔兜底，实用且健壮
4. **红线制度**：四条红线定义明确、可检查，是保持架构整洁的有效护栏

### 4.2 ⚠️ 潜在风险

| 风险 | 说明 | 缓解建议 |
|------|------|---------|
| **Worker 单点故障** | Worker 线程异常死亡后快照会"冻结"在最后一帧，前端无感知 | 添加 heartbeat 时间戳，前端检测超时（如 >5s 未更新）显示告警 |
| **IV 收敛边界** | bisection 30 次迭代、搜索上界 5.0（500% vol），对极端行情可能不够 | 建议添加 Newton-Raphson 加速或至少在文档中标注适用范围 |
| **内存泄漏风险** | 每秒生成新快照对象做 atomic swap，旧对象依赖 GC 回收 | Python GC 通常够用，但建议监控内存；或复用对象池 |
| **无持久化层** | 全部数据在内存，进程重启丢失所有状态 | 当前设计可接受（结算单从文件加载），但 yesterday_snapshot 需持久化 |

---

## 五、修订优先级建议

```
┌─ P0（阻塞性，建议立即修正）──────────────────┐
│  1. 更新 G1/G2/G4 缺口状态为"已关闭"        │
│  2. 修正 §七 文件路径为实际路径               │
│  3. 明确 position['price'] 是否为结算价 (P2)  │
└──────────────────────────────────────────────┘

┌─ P1（重要，建议下一版本修正）─────────────────┐
│  4. 统一 IV 单位约定，消除 black76 隐式转换   │
│  5. pos_theta 符号处理加注释或统一范式         │
│  6. 补全商品期权乘数表                        │
│  7. 定义 yesterday_snapshot 加载流程           │
│  8. 统一 bisection 兜底 vs ATM 中位数兜底     │
└──────────────────────────────────────────────┘

┌─ P2（改进型，可规划到后续迭代）───────────────┐
│  9. 策略2覆写策略1 的语义改为"兜底"           │
│  10. L1/L2 volume 语义标注                    │
│  11. 三级/四级规则标题修正                     │
│  12. Worker heartbeat 机制                    │
└──────────────────────────────────────────────┘
```

---

## 七、修正状态追踪

| 问题 | 状态 | 修正内容 |
|------|------|---------|
| **M4 §七路径** | ✅ 已修正 | 文档路径从 `vnpy接口封装/` 更新为 `GreeksDashboard_v0.1/`，目录结构同步更新 |
| **P2 pnl_daily settle_price** | ✅ 已修正 | `risk_engine.py` + 文档 §3.5：改为从昨日快照取 adjust_price，无快照则用 contract.pre_close 兜底 |
| **P1 pos_theta** | ❌ 误报 | 报告引用的是旧代码，当前代码 L237 已统一为 `direction_sign` 范式，无问题 |
| **L4 bisection兜底** | ❌ 误报 | 两处均用 0.20，报告所述"不一致"不成立 |
| **P3 IV边界** | ⏸️ 低风险暂留 | black76 L27 边界值问题，低概率场景，文档已有注释标注 |

文档整体质量 **优秀（8/10）**，是一份可以实际指导重构的基线文档。主要问题集中在：

1. **缺口清单过时** — 多个 G 缺口在代码中已修复但文档未更新
2. **少数算法细节有歧义** — IV 单位、theta 符号、settle_price 来源
3. **部分规格不完整** — 商品期权乘数、yesterday_snapshot 机制

建议在下一版（v1.1-baseline）中集中修正上述 P0/P1 问题，并将缺口清单更新为最新状态。

> **修正完成（2026-09-13）**：M4、P2 已修正，P1 pos_theta 和 L4 bisection 核实为误报，P3 IV 边界低风险暂留。
