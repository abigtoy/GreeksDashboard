# -*- coding: utf-8 -*-
"""单测：_on_trade 必须认得 vnpy Event 载荷（2026-09-28 断链回归）

背景：_on_trade 被原样注册到 event_engine.register(EVENT_TRADE, _on_trade)，
收到的是 Event 而非 TradeData → getattr 全落空 → 兜底 trade_id 恒为 "_0_0"
→ 全天成交 dedup 成同一条被丢弃 → 当日盈亏缺现金项。
本测试直接构造 Event 喂进去，证明修复后能正确入账，且坏条不入账。
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from vnpy.trader.event import EVENT_TRADE
from vnpy.event.engine import Event   # vnpy/event/engine.py:75 → handler(event)，传的是 Event
from vnpy.trader.object import TradeData
from vnpy.trader.constant import Direction, Offset, Exchange
import api_server as A

fail = []

def check(name, cond):
    print(("PASS" if cond else "FAIL"), name)
    if not cond:
        fail.append(name)

def mk_event(**kw):
    t = TradeData(symbol=kw["symbol"], exchange=Exchange.CFFEX, orderid="O1",
                  tradeid=kw["tid"], direction=kw["dir"], offset=kw["off"],
                  price=kw["price"], volume=kw["vol"], gateway_name="CTP",
                  datetime="2026-09-28 13:20:00")
    return Event(type=EVENT_TRADE, data=t)

A._reset_trade_state()

# ⚠️ 隔离：_on_trade 会经 _rollover_trading_day 回放磁盘账本、并原子写回 trade_ledger.json。
# 测试必须把账本重定向到临时文件，否则会污染生产账本（2026-09-28 踩过）。
import tempfile
_TMP_LEDGER = os.path.join(tempfile.gettempdir(), "_test_trade_ledger.json")
if os.path.exists(_TMP_LEDGER):
    os.remove(_TMP_LEDGER)
A._ledger_path = lambda: _TMP_LEDGER

# ---- 1. 卖平 2 手 @3800：Event 载荷能解出全部字段 ----
A._on_trade(mk_event(symbol="IF2610.CFFEX", tid="T1", dir=Direction.SHORT,
                     off=Offset.CLOSE, price=3800.0, vol=2))
recs = [r for lst in A._TRADE_CACHE.values() for r in lst]
check("Event 载荷解析出 1 条", len(recs) == 1)
if recs:
    r = recs[0]
    check("symbol 去交易所后缀", r["symbol"] == "IF2610", )
    check("price 正确", abs(r["price"] - 3800.0) < 1e-6)
    check("volume 正确", r["volume"] == 2)
    check("trade_side=short（卖）", r["trade_side"] == "short")
    check("position_direction=long（卖平→平掉多头）", r["position_direction"] == "long")
    check("dedup_key 不再退化成 _0_0", r["dedup_key"] == "20260928_CTP_CFFEX_T1", )
    check("account=gateway_name", r["account"] == "CTP")
    check("trade_time 来自 event.data", r["trade_time"].startswith("2026-09-28 13:20"))

# ---- 2. 坏条（volume=0）不入账 ----
n0 = len([r for lst in A._TRADE_CACHE.values() for r in lst])
A._on_trade(mk_event(symbol="IF2610.CFFEX", tid="TBAD", dir=Direction.SHORT,
                     off=Offset.CLOSE, price=0.0, vol=0))
check("零手数坏条被丢弃", len([r for lst in A._TRADE_CACHE.values() for r in lst]) == n0)

# ---- 3. 同一 tradeid 重复推送只入一次（幂等）----
A._on_trade(mk_event(symbol="IF2610.CFFEX", tid="T1", dir=Direction.SHORT,
                     off=Offset.CLOSE, price=3800.0, vol=2))
check("重复 tradeid 幂等", len([r for lst in A._TRADE_CACHE.values() for r in lst]) == n0)

# ---- 4. 现金口径：卖平产生正现金，键 = symbol_position_direction ----
cf = A._cash_flow_map()
check("IF2610_long 现金 = +7600（2手@3800，size 300）", abs(cf.get("IF2610_long", 0) - 7600.0) < 1e-6,
      )
print("   _cash_flow_map =", cf)

# ---- 5. 直接喂 TradeData（不包 Event）也兼容 ----
n1 = len([r for lst in A._TRADE_CACHE.values() for r in lst])
A._on_trade(mk_event(symbol="IF2612.CFFEX", tid="T2", dir=Direction.LONG,
                     off=Offset.OPEN, price=3900.0, vol=1).data)
check("裸 TradeData 也能入账", len([r for lst in A._TRADE_CACHE.values() for r in lst]) == n1 + 1)

print("\n" + ("ALL PASS" if not fail else f"FAILED: {fail}"))
sys.exit(1 if fail else 0)
