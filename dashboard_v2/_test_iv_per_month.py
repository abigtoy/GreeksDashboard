# -*- coding: utf-8 -*-
"""单测：IV 速率按月份合约分窗 + MO→IM 月键映射"""
import sys, os, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import api_server as A

now = time.time()
fail = []

def check(name, cond):
    print(("PASS" if cond else "FAIL"), name)
    if not cond:
        fail.append(name)

class P:
    def __init__(s, vt): s.vt_symbol = vt

def greeks(iv):
    return {"iv": iv, "delta": 0.5, "is_itm": False,
            "underlying_price": 460.0, "strike": 4600.0, "days_to_expiry": 30}

def feed(bu, og, t):
    return A._compute_iv_rate(bu, og, t)

# ---- 两月各自 IV 序列：sc2611 平稳 / sc2612 跳动 ----
bu = {}
og = {}
out = {}
for k in range(6):
    t = now - 250 + k * 40
    bu[("sc2611", "2026-11-25")] = [(P("sc2611C4600.INE"), None)]
    bu[("sc2612", "2026-12-25")] = [(P("sc2612C4600.INE"), None)]
    og["sc2611C4600.INE"] = greeks(21.0)                 # 恒定
    og["sc2612C4600.INE"] = greeks(21.0 + (k % 2) * 2.0)  # 跳动
    out = feed(bu, og, t)

check("iv 月键 SC2611 存在", "SC2611" in out)
check("iv 月键 SC2612 存在", "SC2612" in out)
check("iv 无品种级键(SC)", "SC" not in out)
if "SC2611" in out:
    check("iv SC2611 恒定→速率0", out["SC2611"][0] == 0.0)
if "SC2612" in out:
    r, lo, hi = out["SC2612"]
    check("iv SC2612 两端=自身IV", lo == 21.0 and hi == 23.0)

# ---- MO→IM 映射 ----
bu2 = {("MO2610", "2026-10-16"): [(P("MO2610-P-7000.CFFEX"), None)]}
og["MO2610-P-7000.CFFEX"] = greeks(17.0)
feed(bu2, og, now - 40)   # 窗口需≥2样本
o2 = feed(bu2, og, now)
check("iv MO期权→月键IM2610", "IM2610" in o2)
check("iv 无错误键MO2610", "MO2610" not in o2)

print("RESULT:", "ALL PASS" if not fail else f"{len(fail)} FAIL: {fail}")
sys.exit(1 if fail else 0)
