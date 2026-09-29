# -*- coding: utf-8 -*-
"""没开盘就没有盈亏：夜盘窗口内，无夜盘时段的品种当日盈亏强制 0。

背景：21:16 夜盘，中金所不开盘 → IF/IC/MO 无 tick → 今市值算成 0，
公式 `今市值 − 昨市值 + 现金` 退化成 `−昨市值`，编出 −1884 万假亏。
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dashboard_v2 import api_server
from dashboard_v2.risk_engine import build_tree

FAILS = []


def ck(name, cond, extra=""):
    print(("  OK   " if cond else "  FAIL ") + name + (f"   {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def with_session(sess):
    """把 _current_session 换成固定返回值，返回原函数供还原。"""
    orig = api_server._current_session
    api_server._current_session = lambda dt=None: (sess, "20260929")
    return orig


print("[1] _night_gated_products —— 夜盘窗口判定")
orig = with_session("N")
try:
    gated = api_server._night_gated_products()
finally:
    api_server._current_session = orig
ck("夜盘窗口内: CFFEX IF 在门内", "IF" in gated)
ck("夜盘窗口内: CFFEX MO 在门内", "MO" in gated)
ck("夜盘窗口内: AU 有夜盘 → 放行", "AU" not in gated)
ck("夜盘窗口内: SC 有夜盘 → 放行", "SC" not in gated)

orig = with_session("P")
try:
    day = api_server._night_gated_products()
finally:
    api_server._current_session = orig
ck("非夜盘窗口(午盘) → 空集，腿走正常口径", day == set(), f"got={sorted(day)[:5]}")

print("\n[2] build_tree —— 门内腿归零、门��腿照算")
positions = [
    {"symbol": "IF2612",     "direction": "long", "volume": 8, "size": 300,  "price": 4254.0},
    {"symbol": "au2612",     "direction": "long", "volume": 2, "size": 1000, "price": 780.0},
]
ticks = {
    "IF2612":  {"last_price": 4254.0, "underlying_price": 0, "iv": None},
    "au2612":  {"last_price": 780.0,  "underlying_price": 780.0, "iv": 0.15},
}
contracts = {
    "IF2612": {"size": 300,  "product_type": "FUTURES", "days_to_expiry": None},
    "au2612": {"size": 1000, "product_type": "FUTURES", "days_to_expiry": 14},
}
cash_flow = {"IF2612_long": -1_000_000.0, "au2612_long": -100_000.0}
prev_mv   = {"IF2612_long": 0.0, "au2612_long": 0.0}

out = build_tree(positions, ticks, contracts, {}, None, cash_flow, prev_mv,
                 night_gated={"IF"})
leaves = {}
for l1 in out["tree"]:
    for l2 in l1["children"]:
        for l3 in l2["children"]:
            leaves[l3["key"]] = l3

ck("门内腿 IF2612_long 存在", "IF2612_long" in leaves)
if "IF2612_long" in leaves:
    n = leaves["IF2612_long"]
    ck("门内腿 pnl_today == 0（不编假亏）", n["pnl_today"] == 0.0, f"got={n['pnl_today']}")
    ck("门内腿 price_basis == not_open", n["price_basis"] == "not_open", f"got={n['price_basis']}")

ck("门内腿不污染 total（summary 已含 0）",
   out["summary"]["total_pnl_today"] == leaves["au2612_long"]["pnl_today"],
   f"summary={out['summary']['total_pnl_today']}")

ck("门外观测腿 au2612_long 存在", "au2612_long" in leaves)
if "au2612_long" in leaves:
    n = leaves["au2612_long"]
    ck("门外腿 price_basis == cash_flow", n["price_basis"] == "cash_flow", f"got={n['price_basis']}")
    # 2 手 × 1000 × 780 − 100000 = 1460000
    ck("门外腿照算现金口径", abs(n["pnl_today"] - 1_460_000.0) < 1e-6, f"got={n['pnl_today']}")

print("\n[3] 门计入 pnl_basis_counts（前端可见，不静默）")
cnt = out["summary"].get("pnl_basis_counts", {})
ck("not_open 计数 == 1", cnt.get("not_open") == 1, f"got={cnt}")
ck("cash_flow 计数 == 1", cnt.get("cash_flow") == 1, f"got={cnt}")

print("\n" + ("ALL PASS" if not FAILS else f"FAILED {len(FAILS)}: {FAILS}"))
sys.exit(1 if FAILS else 0)
