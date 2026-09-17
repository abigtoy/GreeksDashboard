# pnl_today 计算方案设计

## 1. 背景与目标

GreeksDashboard 采用**双调整价（Mark-to-Mark）**体系计算当日盈亏（pnl_today）。
本方案解决：
- 今仓（今开、今平）无法计算 pnl_today
- 服务不稳定导致数据丢失
- 多交易时段（早A / 午M / 夜N）状态管理

## 2. 基准价优先顺序

| 持仓类型 | 基准价（首选） | 降级备用 |
|---|---|---|
| 老仓（昨仓今持、昨仓今平） | 昨快照 `adjust_price`（昨收盘 Mark） | T-1 结算单 `settlement_price` |
| 今仓（今开、今平） | CTP 成交回报（秒级） | **无**（T-1 结算单不存在该合约） |

> 注意：结算单中的 `prev_sttl_price`（昨结算列）**永远不使用**。

## 3. 四分法

| | 情形 | pnl_today 公式 |
|---|---|---|
| ① | 昨仓今持（未平） | `(Mark_now - Mark_T-1) × vol × size` |
| ② | 昨仓今平（平昨） | `(P_trade_close - Mark_T-1) × vol × size` |
| ③ | 今仓未平（新开） | `(Mark_now - P_trade_open) × vol × size` |
| ④ | 今仓今平（平今） | `(P_trade_close - P_trade_open) × vol × size` |

- `Mark_now` = tick 的 `adjust_price`
- `Mark_T-1` = 昨快照 `adjust_price`，无则用 T-1 结算单 `settlement_price`
- `P_trade_open` / `P_trade_close` = CTP 成交回报真实成交价

## 4. 今仓/老仓判断

```
今仓 = 合约在 full_{T-1}.json 的 positions_detail 中不存在
     且 合约在 _trade_cache 中存在（今天有成交记录）

老仓 = 合约在 full_{T-1}.json 中存在
```

## 5. CTP 成交回报

### 5.1 进程内缓存

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

### 5.2 onRtnTrade 回调

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

## 6. calc_pnl 修改

`calc_pnl(position, contract, tick, settlement_dict, settlement_prices, yesterday_snapshot, trade_cache=None)`：

```python
sym = position['symbol'].split('.')[0]
is_new_position = (sym not in _yesterday_positions_set) and (sym in trade_cache)

if is_new_position:
    # 今仓：用成交回报的 open_price（成交量加权均价）
    trades = trade_cache.get(sym, [])
    open_price = weighted_avg(trades, 'price', 'volume')
    base_today = open_price
else:
    # 老仓：原有逻辑
    base_today = (yesterday_snapshot.get(f"{sym}_{direction_str}", {}).get('adjust_price')
                  or settlement_prices.get(sym)
                  or math.nan)
```

## 7. 平仓盈亏（已实现 pnl_realized）

```
pnl_realized = (P_trade_close - P_trade_open) × vol × size
```

由成交回报的 close_price 和 open_price 实时计算。

## 8. 边界情况

| 情况 | 处理方式 |
|---|---|
| 同一合约多次开仓 | 取成交量加权均价 |
| 今开今平（当日回转） | open_price + close_price 均从 `_trade_cache` 取 |
| 混合老仓+今仓（同合约） | 今仓部分用成交回报，老仓部分用 settlement_price |
| 老仓也有新成交（加仓） | 同上：新加部分用成交回报，原有老仓用 settlement_price |
| vnpy TdApi 重连 | `_trade_cache` 保留（进程内），重连后继续追加 |

## 9. 持久化方案

### 9.1 持久化范围

| 数据 | 持久化 | 原因 |
|---|---|---|
| 持仓快照（positions_summary + adjust_price） | ✅ 保存 | 服务重启后恢复 |
| 结算成本（settlement_cost_dict） | ✅ 保存 | 本地文件，已实现 |
| CTP成交回报（trade_cache） | ✅ 保存 | 今仓基准，重启后丢失只能归零 |
| 持仓合约开盘状态（opened_contracts） | ✅ 保存 | 重启后恢复哪些合约已开盘 |
| 实时行情（tick） | ❌ 不保存 | 随时变化，重启后重新接收 |

### 9.2 开盘判断规则（规则化）

```
任何合约当天收到第一个 tick 价格信息 → 标记为"已开盘"，开始计算 pnl_today
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
       恢复 opened_contracts（已开盘合约继续算pnl_today）
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

## 10. 文件结构改动

| 文件 | 改动 |
|---|---|
| `api_server.py` | 新增 `_trade_cache`、`_opened_contracts`、`_session_state`；连接 TdApi 时注册 `onRtnTrade`；传入 `trade_cache` 给 `build_tree`；实现 `on_tick` 开盘标记 |
| `risk_engine.py` | `calc_pnl` 新增 `trade_cache` 参数；今仓用成交回报计算基准价 |
| `settlement.py` | 新增 `load_yesterday_positions_set()` 加载 T-1 结算单合约名集合 |
| `session_state.json` | 新增，盘中交易状态持久化文件 |

## 11. 依赖

- `full_{T-1}.json` 存在（T-1 结算单）
- vnpy TdApi `onRtnTrade` 回调正常
- `settlement_prices` 来自 `full_{T-1}.json` 的 `positions_detail[].settlement_price`

## 12. 附录：关键字段说明

| 字段 | 来源 | 用途 |
|---|---|---|
| `adjust_price` | 行情 tick | 当前 Mark Price |
| `settlement_price`（T-1） | full_{T-1}.json | 老仓降级基准 |
| `prev_sttl_price` | full_*.json | **不使用** |
| `open_price`（成交回报） | CTP onRtnTrade | 今仓基准 |
| `close_price`（成交回报） | CTP onRtnTrade | 已实现盈亏 |
