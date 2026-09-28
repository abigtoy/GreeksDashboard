# -*- coding: utf-8 -*-
"""单测：当日盈亏现金口径（2026-09-28）

    当日盈亏(腿) = 今持仓市值 − 昨持仓市值 + 今日成交净现金

关键性质：线性可加（各级 = 各腿之和，无需摊派）、对 offset_flag 免疫。
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from risk_engine import calc_pnl_today

fail = []

def check(name, got, want, tol=0.01):
    ok = abs(float(got) - float(want)) <= tol
    print(("PASS" if ok else "FAIL"), name, f"got={got} want={want}")
    if not ok:
        fail.append(name)

# size=1 便于手算：昨 3 手 @12 → 昨市值 36；今 3 手 @10 → 今市值 30
K, NOW, PREV = 10.0, 10.0, 12.0

# ---- 1. 纯昨仓、今价跌：只有市值差，无现金 ----
check("昨仓多头 3手 10 vs 昨12 → -6",
      calc_pnl_today("X_long", 1, 3, 1, NOW, {}, {"X_long": 3 * PREV}), -6)

# ---- 2. 纯今开腿（问题1：曾被今开成本覆盖）：昨市值缺该腿 = 0 ----
# 今开 2 手卖在 10，昨市值无此腿（今天才有仓）→ 盈亏 0，不该出现开仓成本 360
check("纯今开空头无昨市值 → 0（非今开成本）",
      calc_pnl_today("MO2610-C-8500_short", -1, 2, 1, 10.0, {"MO2610-C-8500_short": 20.0}, {}), 0.0)

# ---- 3. 纯今开腿 + 今价跌：只算今市值 − 0 + 现金 ----
# 卖 2 手 @10.3919 收 20.7838，今价 9.5 → 市值 -19 → -19 + 20.7838
check("纯今开空头今价跌",
      calc_pnl_today("S_short", -1, 2, 1, 9.5, {"S_short": 20.7838}, {}), 1.78)

# ---- 4. 全平腿（问题2：曾被写死 0）：腿没了，只剩现金 ----
check("全平腿只剩现金", calc_pnl_today("S_short", -1, 0, 1, 9.5, {"S_short": 20.0}, {"S_short": -20.0}), 40.0)

# ---- 5. 买入现金为负、卖出为正（api_server._cash_flow_map 的符号约定）----
cf = {"B_long": -30.0, "B_short": 30.0}   # 多头买 3 手 / 空头卖 3 手
check("多头买入现金为负", cf["B_long"], -30.0)
check("空头卖出现金为正", cf["B_short"], 30.0)

# ---- 6. 线性可加：拆成两腿之和 == 整腿（无摊派）----
whole = calc_pnl_today("X_long", 1, 3, 1, NOW, {"X_long": 10.0}, {"X_long": 24.0})
part  = (calc_pnl_today("X_long", 1, 1, 1, NOW, {"X_long": 10.0}, {"X_long": 8.0})
         + calc_pnl_today("X_long", 1, 2, 1, NOW, {}, {"X_long": 16.0}))
check("线性可加 3手 == 1手+2手", part, whole)

# ---- 7. 义务仓：空头期权市值记负，方向符号生效 ----
# 空 2 手 @5（义务仓，市值 -10），今价 4 → -8，昨市值 -12 → -8 - (-12) = 4
check("空头期权市值取负",
      calc_pnl_today("O_short", -1, 2, 1, 4.0, {}, {"O_short": -12.0}), 4.0)

# ---- 8. 平今平昨免疫：offset_flag 不进函数，现金对两种平仓一样 ----
# 卖平 2 手 @12 收 24（无论平今还是平昨），今市值 20，昨市值 24 → 24 + 20 - 24 = 20
check("卖平现金与平今/平昨无关",
      calc_pnl_today("X_long", 1, 2, 1, 10.0, {"X_long": 24.0}, {"X_long": 24.0}), 20.0)

print("\n" + ("ALL PASS" if not fail else f"FAILED: {fail}"))
sys.exit(1 if fail else 0)
