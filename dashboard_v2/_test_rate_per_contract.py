# -*- coding: utf-8 -*-
"""单测：F/IV 速率按月份合约分窗（2026-09-24 改造）"""
import sys, os, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import api_server as A

now = time.time()
fail = []

def check(name, cond):
    print(("PASS" if cond else "FAIL"), name)
    if not cond:
        fail.append(name)

# ---- 1. f_rate 按合约分窗：RU2611 恒价 / RU2612 振荡交替喂价 ----
up = {}
out = {}
for k in range(10):
    t = now - 250 + k * 25
    up["RU2611.SHFE"] = 19470.0                 # 恒定
    up["RU2612.SHFE"] = 19560.0 - (k % 2) * 90  # 振荡
    out = A._compute_f_rate(up, t)
check("f_rate 恒价合约无键(RU2611)", "RU2611" not in out)
check("f_rate 振荡合约有键(RU2612)", "RU2612" in out)
if "RU2612" in out:
    rate, lo, hi = out["RU2612"]
    check("f_rate RU2612 窗口两端=自身价格", lo == 19470.0 and hi == 19560.0)
# 旧聚合若仍在，RU 键会混出假速率；确认无品种级键
check("f_rate 无品种级键(RU)", "RU" not in out)

# ---- 2. 键格式：大写合约代码 ----
up2 = {"sc2611.INE": 460.0, "sc2612.INE": 470.0}
for k in range(3):
    o2 = A._compute_f_rate(dict(up2, sc2612=up2["sc2612.INE"] + k), now - 100 + k)
# full_und 键带交易所后缀的才是真实输入
up3 = {"sc2611.INE": 460.0 + k, "sc2612.INE": 470.0 - k}
o3 = A._compute_f_rate(up3, now)
check("f_rate 键为大写合约 SC2611", "SC2611" in o3)
check("f_rate 键为大写合约 SC2612", "SC2612" in o3)

# ---- 3. iv_rate 按月分窗：看 _atm_iv_of_group 输入结构 ----
import inspect
print("---- _atm_iv_of_group source ----")
print(inspect.getsource(A._atm_iv_of_group))

print("RESULT:", "ALL PASS" if not fail else f"{len(fail)} FAIL: {fail}")
sys.exit(1 if fail else 0)
