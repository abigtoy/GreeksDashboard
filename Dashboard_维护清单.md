# Dashboard 维护清单

> **建立日期**：2026-09-14
> **版本**：v1.1（整合 2026-09-14 session 成果）
> **设计文档**：`Dashboard_设计基线.md`（v1.0 / 2026-09-11）
> **状态**：🔄 进行中

---

## 一、设计基线 vs 已实现对照

### ✅ 已完成项

| 功能 | 设计基线描述 | 实现文件 | 状态 | 备注 |
|------|------------|---------|------|------|
| 结算单加载 | load_settlement_sync 扫 full_*.json，双策略读 positions_summary/positions_detail | settlement.py | ✅ | |
| settlement_dict 格式 | key="{sym}_多/_空"，value=float | settlement.py | ✅ | |
| CTP 引擎方法 | query_positions / query_tick / query_account | api_server.py | ✅ | G1 已修复 |
| Black-76 Greeks | 纯函数，IV/T/r 参数正确 | pricing.py | ✅ | |
| implied_vol_bisection | T/r 参数顺序正确 | pricing.py | ✅ | 之前错误互换已修复 |
| cp_from_symbol | 从合约名 rfind('C')>rfind('P') 判定 C=1/P=-1 | risk_engine.py | ✅ | CTP option_type 是中文不能用 |
| T 下限 | 0.5/365（pricing + risk_engine 一致） | pricing.py, risk_engine.py | ✅ | |
| r 利率 | 统一 0.02（pricing + risk_engine 一致） | pricing.py, risk_engine.py | ✅ | 之前 pricing=0.03 已修正 |
| adjust_price 四级 | OTM 动态价差(4层) + ITM PCP平价 + 嵌套腿递归 | pricing.py | ✅ | 2026-09-14 移植 |
| Greeks 口径 | 可汇总列=头寸级(原始×sign×手数)，父级 Σ 子级 | risk_engine.py, pricing.py | ✅ | 2026-09-14 统一 |
| deltacash | pos_delta × s × size | pricing.py, risk_engine.py | ✅ | |
| gammacash | pos_gamma × s² × 0.01 × size（1%标的变动） | pricing.py, risk_engine.py | ✅ | 注意：只乘 0.01 不是 0.01² |
| vegacash | pos_vega × size | pricing.py, risk_engine.py | ✅ | 空头自然为负 |
| thetacash | pos_theta × size | pricing.py, risk_engine.py | ✅ | |
| 期货 delta | 方向×手数（不是写死的 1.0） | risk_engine.py | ✅ | |
| L1/L2 汇总 | Greeks/PnL Σ 子级，含 *_tag | risk_engine.py | ✅ | |
| 前端列定义 | 单一 COL_DEF 源，18列（删 direction） | dashboard11.js | ✅ | |
| 前端表头 | JS 由 COL_DEF 生成，不用硬编码 | dashboard11.js | ✅ | |
| 前端树展开 | 初始三层全展开（expandedG/M） | dashboard11.js | ✅ | |
| CTP 连接四态 | disconnected/connecting/connected/error | api_server.py | ✅ | |
|不掉线不close | _engine=None + break，不调 eng.close() | api_server.py | ✅ | CtpTdApi.exit()持GIL阻塞会冻死 |
|掉线重连 | 丢弃旧引擎建新，不 close | api_server.py | ✅ | |
| 掉线守卫 | _td_logged_in(eng) 检测掉线 | api_server.py | ✅ | |

### 🔧 待修复项（已定位）

| # | 问题 | 根因 | 修法 | 优先级 |
|---|------|------|------|--------|
| B1 | L1 Greeks 数值是真实值 2× | build_tree L291 每个 L3先进 L1_metrics，L297-298 L2（含全部L3）又进 L1 → 双计 | 删 L291 的 `_accumulate_metrics(l1_metrics, node)`，只保留 L309 L2 进 L1 | P0 |
| B2 | gammacash 可能多乘了 0.01² | pricing.py L343 写 `s*s*0.01*0.01*size`，应 `s*s*0.01*size` | 删一个 `*0.01` | P0 |
| B3 | open_price 很多是 0 | 结算单缺 09-11/09-14；无结算单时 fallback 到 pos.price（CTP 均价可能为空） | 结算单路径用绝对路径；无结算单时 pnl_history=0 而非错误值 | P1 |
| B4 | L3 name 字段为空 | _build_l3_node 未赋值 name（前端用 l3.symbol fallback，正常） | 可选：补 name=contract.name | P2 |
| B5 | 展开按钮"部分展开"困惑 | 按钮是 toggle，点开后再点等于收；初始加载三层全展开正确 | 可选：改文案"展开/折叠切换" | P2 |

### ❌ 未实现项（原基线缺口）

| 缺口 | 原描述 | 状态 | 说明 |
|------|--------|------|------|
| G1 | engine 方法名错误 | ✅ 已修复 | query_positions 等 |
| G2 | settlement 扫描 parsed_*.json | ✅ 已修复 | 改扫 full_*.json |
| G3 | adjust_price 仅占位 | ✅ 已修复 | 四级规则已移植 |
| G4 | dashboard.js 正则解析 | ✅ 已修复 | 切到 dashboard11.js |
| G5 | /api/dashboard 未用 tree | ✅ 已修复 | 前端用 tree 递归渲染 |
| G6 | ITM PCP 串行计算拓扑序 | ⚠️ 部分 | pricing.py 已实现四级，未验证嵌套递归 |
| — | 结算单自动保存计划任务 | ❌ 未做 | 需 Hermes cron 任务 |
| — | Dashboard/Proxy 进程自愈 | ❌ 未做 | 需 Hermes cron 任务 |

---

## 二、当前数据状态（2026-09-14）

```
连接状态：connected
持仓条数：50
树节点：11（L1 品种 × L2 月份）
L3 腿数：约 38（实际 L3 层）
结算单：40个 full_*.json，最新 09-10，缺 09-11、09-14
settlement_dict 命中：38/50（12 条缺失因合约跨期/新开仓）
ERROR：0
```

### 数据质量

| 字段 | 状态 | 说明 |
|------|------|------|
| Greeks 符号 | ✅ 正确 | short put δ>0（空头正），short call δ<0（空头负） |
| Greeks 口径 | ✅ 正确 | 可汇总列=头寸级，cash 列用正因子 |
| IV | ✅ 正确 | IV=25.7%（之前 65% 已修复） |
| adjust_price | ✅ 正确 | 与 last_price 有差异（OTM/ITM 调整生效） |
| 期货最新价 | ✅ 非零 | 12 个期货 last_price 全有值 |
| flat vs tree Greeks | ⚠️ 微差 | <0.1%，浮点漂移，非 bug |
| L1 delta 双计 | ❌ 2× | 待修 |
| open_price | ⚠️ 部分为0 | 结算单缺失导致 |
| gammacash | ⚠️ 可能虚高100× | 待修 |

---

## 三、待办优先级

### P0（必须修，影响数据正确性）
1. **B1**：删 L291 双计代码 → 重启验证
2. **B2**：删 pricing.py gammacash 多余 `*0.01` → 重启验证

### P1（影响数据完整性）
3. **B3**：结算单路径加 fallback，无结算单时 settle_key=0

### P2（cosmetic）
4. 展开按钮文案优化（选做）
5. L3 name 字段补全（选做）
6. G6：ITM 嵌套 PCP 递归验证（需实际 ITM 合约数据）

### 长期
7. 结算单自动保存 Hermes cron 任务
8. Dashboard/Proxy 进程自愈

---

## 四、关键设计决策记录（2026-09-14 确认）

| 决策 | 内容 |
|------|------|
| Greeks 口径铁律 | 可汇总列(δ/Γ/Vega/Θ)=多头原始值×方向sign×|手数|；现金列=该列×正因子×size；**绝不给任何列单独翻号** |
| cp_from_symbol | 从合约名 `rfind('C')>rfind('P')` 判定；CTP option_type 中文永远不用 |
| adjust_price 四级 | OTM: 4层动态价差(基础0.05/紧0.20/阈值0.001/最大0.50/K=2.25)+新鲜盘口；ITM: PCP平价+对手OTM腿递归 |
| T 下限 | 0.5/365（pricing + risk_engine 一致） |
| r | 0.02（统一） |
| deltacash | pos_delta × s × size |
| gammacash | **F² × 0.01 × size**（1%标的变动） |
| vegacash | pos_vega × size |
| thetacash | pos_theta × size |
|不掉线不close | CtpTdApi.exit()持GIL会冻死解释器，永远不调；掉线→_engine=None+break→外层重建 |
| pnl_daily | 盯日盈亏：last_price 对比昨日 adjust_price（昨收） |
| 前端列 | 18列，删 direction；COL_DEF 单一数据源 |
