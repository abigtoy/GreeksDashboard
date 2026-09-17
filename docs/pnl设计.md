# PnL 计算方案设计

## 1. 概述

GreeksDashboard 采用**双调整价（Mark-to-Mark）**体系计算盈亏：
- `pnl_today`：当日盈亏，当日结算后清零
- `pnl_history`：浮动盈亏（已实现 + 未实现），跨交易日重新计算

本方案解决：
- 今仓（今开、今平）盈亏计算
- 服务不稳定导致数据丢失的持久化方案
- 多交易时段（早A / 午M / 夜N）状态管理

> 注意：结算单中的 `prev_sttl_price`（昨结算列）**永远不使用**。

---

## 2. 核心公式

```
pnl_today  = direction_sign × (Mark_now - Mark_T-1) × vol × size
pnl_history = pnl_realized + pnl_unrealized

pnl_realized   = (P_trade_close - cost_price) × vol_closed × size
pnl_unrealized = (Mark_now - cost_price) × vol_remaining × size
```

---

## 3. 基准价体系

### 3.1 基准价优先顺序

| | 持仓类型 | pnl_today 基准价 | pnl_history cost_price |
|---|---|---|---|
| 老仓 | 昨仓今持、昨仓今平 | 昨快照 adjust_price（T-1 收盘 Mark） | settlement_cost_dict[sym] |
| 老仓降级 | — | T-1 结算单 settlement_price | **无降级** |
| 今仓 | 今开、今平 | 成交回报 P_trade_open | 成交回报 P_trade_open |
| 今仓降级 | — | **无**（T-1 结算单不存在该合约） | **无** |

### 3.2 今仓/老仓判断

```
今仓 = 合约在 full_{T-1}.json 的 positions_detail 中不存在
     且 合约在 trade_cache 中存在（今天有成交记录）

老仓 = 合约在 full_{T-1}.json 中存在
```

---

## 4. 四分法

### 4.1 pnl_today

| | 情形 | 公式 |
|---|---|---|
| ① | 昨仓今持（未平） | `(Mark_now - Mark_T-1) × vol × size` |
| ② | 昨仓今平（平昨） | `(P_trade_close - Mark_T-1) × vol_closed × size` |
| ③ | 今仓未平（新开） | `(Mark_now - P_trade_open) × vol × size` |
| ④ | 今仓今平（平今） | `(P_trade_close - P_trade_open) × vol_closed × size` |

### 4.2 pnl_history

| | 情形 | 公式 |
|---|---|---|
| ① | 昨仓今持 | `pnl_unrealized = (Mark_now - cost_price) × vol × size` |
| ② | 昨仓今平（全部平完） | `pnl_realized = (P_trade_close - cost_price) × vol_closed × size` |
| ③ | 今仓未平 | `pnl_unrealized = (Mark_now - P_trade_open) × vol × size` |
| ④ | 今仓今平（全部平完） | `pnl_realized = (P_trade_close - P_trade_open) × vol_closed × size` |

---

## 5. 混合持仓处理

同一合约同时存在老仓和今仓时，需分两部分计算：

```
已知:
  vol_持仓    = 当前持仓 volume（来自 CTP position）
  vol_今开    = Σ trade_cache[sym] 中 open_close='开' 的 volume
  vol_今平    = Σ trade_cache[sym] 中 open_close='平' 的 volume

若合约在 full_{T-1} 中存在（老仓）:
  vol_老 = max(0, vol_持仓 - vol_今开)          # 老仓剩余持仓
  vol_老_已平 = min(原老仓volume, vol_今平)      # 老仓被平数量
  pnl_老_unrealized = (Mark_now - settlement_cost) × vol_老 × size
  pnl_老_realized   = (P_trade_close - settlement_cost) × vol_老_已平 × size

若合约在 trade_cache 中存在（今仓）:
  vol_新 = vol_今开 - vol_今平                    # 今仓剩余持仓（可为0）
  pnl_新_unrealized = (Mark_now - 今仓成交均价) × vol_新 × size
  pnl_新_realized   = (P_trade_close - 今仓成交均价) × vol_新 × size

pnl_today   = pnl_老_today + pnl_新_today
pnl_history = pnl_老_realized + pnl_老_unrealized + pnl_新_realized + pnl_新_unrealized
```

---

## 6. 边界情况

| 情况 | 处理方式 |
|---|---|
| 同一合约多次开仓 | 取成交量加权均价 |
| 今开今平（当日回转） | vol_新 = 0，pnl_新 = pnl_realized（全为已实现） |
| 老仓今平（部分） | vol_老 > 0，老仓同时有 realized + unrealized |
| 老仓今平（全平） | vol_老 = 0，老仓只有 pnl_realized |
| vnpy TdApi 重连 | trade_cache 保留（进程内），重连后继续追加 |

---

## 7. CTP 成交回报

### 7.1 进程内缓存

```python
_trade_cache: dict[str, list] = {}
# key = instrument_symbol, value = [trade_record, ...]
```

每条成交记录格式：

```json
{
  "trade_id": "278350",
  "instrument": "MO2609-P-7000",
  "direction": "买",
  "open_close": "开",
  "price": 11.600,
  "volume": 1,
  "trade_time": "20260915 09:30:12",
  "account": "101009"
}
```

### 7.2 onRtnTrade 回调

vnpy TdApi 子类覆盖：

```python
def onRtnTrade(self, trade: dict):
    symbol = trade['instrument_id'].split('.')[0]
    record = {
        'trade_id': trade.get('order_sys_id', ''),
        'instrument': symbol,
        'direction': '买' if trade['direction'] == 'long' else '卖',
        'open_close': '开' if is_open_trade(trade) else '平',
        'price': trade['price'],
        'volume': trade['volume'],
        'trade_time': trade.get('trade_time', ''),
    }
    _trade_cache.setdefault(symbol, []).append(record)
```

---

## 8. calc_pnl 修改

`calc_pnl(position, contract, tick, settlement_dict, settlement_prices, yesterday_snapshot, trade_cache=None)`：

```python
sym = position['symbol'].split('.')[0]
is_new_position = (sym not in _yesterday_positions_set) and (sym in trade_cache)

if is_new_position:
    # 今仓
    trades = trade_cache.get(sym, [])
    open_price = weighted_avg(trades, 'price', 'volume')
    base_today = open_price
    cost_price = open_price
else:
    # 老仓
    base_today = (yesterday_snapshot.get(f"{sym}_{direction_str}", {}).get('adjust_price')
                  or settlement_prices.get(sym)
                  or math.nan)
    cost_price = settlement_dict.get(sym, position.get('price', 0.0))
```

pnl_realized 由 trade_cache 中的平仓成交实时计算。

---

## 9. 开盘判断与持久化

### 9.1 持久化范围

| 数据 | 持久化 | 原因 |
|---|---|---|
| 持仓快照（positions_summary + adjust_price） | ✅ | 服务重启后恢复 |
| 结算成本（settlement_cost_dict） | ✅ | 本地文件，已实现 |
| CTP成交回报（trade_cache） | ✅ | 今仓基准，重启后丢失只能归零 |
| 持仓合约开盘状态（opened_contracts） | ✅ | 重启后恢复哪些合约已开盘 |
| 实时行情（tick） | ❌ | 随时变化，重启后重新接收 |

### 9.2 开盘判断规则（事件驱动）

```
任何合约当天收到第一个 tick → 标记为"已开盘"，开始计算 pnl_today
没收到 tick → pnl_today = 0
跨交易日自动清空（CTP TradingDay 变化）
```

不按品种/时间表机械判断，纯事件驱动：收到 tick = 开盘。

### 9.3 持久化数据结构

`session_state.json`：

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

### 9.4 重启恢复逻辑

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

### 9.5 盘中持久化时机

```
触发条件:
  - 持仓发生变化（position.volume 变化）
  - 成交回报到达（trade_cache 新增条目）
  - 合约首次收到 tick（opened_contracts 新增）

写入方式:
  - 异步写，不阻塞主流程
  - 每 30 分钟心跳保存（即使无变化）
```

---

## 10. 文件结构改动

| 文件 | 改动 |
|---|---|
| `api_server.py` | 新增 `_trade_cache`、`_opened_contracts`、`_session_state`；连接 TdApi 时注册 `onRtnTrade`；传入 `trade_cache` 给 `build_tree`；实现 tick 首次接收时开盘标记 |
| `risk_engine.py` | `calc_pnl` 新增 `trade_cache` 参数；今仓用成交回报计算基准价；分离 pnl_realized 和 pnl_unrealized |
| `settlement.py` | 新增 `load_yesterday_positions_set()` 加载 T-1 结算单合约名集合 |
| `session_state.json` | 新增，盘中交易状态持久化文件 |

---

## 11. 依赖

- `full_{T-1}.json` 存在（T-1 结算单）
- vnpy TdApi `onRtnTrade` 回调正常
- `settlement_prices` 来自 `full_{T-1}.json` 的 `positions_detail[].settlement_price`
- CTP 连通后行情正常推送

---

## 12. 附录：关键字段说明

| 字段 | 来源 | 用途 |
|---|---|---|
| `adjust_price` | 行情 tick | 当前 Mark Price |
| `settlement_price`（T-1） | full_{T-1}.json | 老仓 pnl_today 降级基准 |
| `settlement_cost` | settlement_cost_dict | 老仓 pnl_history cost_price |
| `prev_sttl_price` | full_*.json | **不使用** |
| `P_trade_open` | CTP onRtnTrade | 今仓 pnl_today 和 pnl_history 基准 |
| `P_trade_close` | CTP onRtnTrade | 平仓盈亏计算 |
