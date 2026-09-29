"""
v1.3.1/v1.4 自检：结算单读取链 + 收盘快照（采样窗口/算术平均基准/落盘守护/T-1 读取）。
运行：C:/veighna_studio/python.exe selfcheck_v131.py   （项目根目录）
只读真实文件 + 临时目录，不写业务数据、不碰运行中的服务。
"""
import json
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
SETTLE_DIR = os.path.join(ROOT, "结算单")

from dashboard_v2 import settlement as S          # noqa: E402
from dashboard_v2 import api_server as A          # noqa: E402

ok = 0


def check(label, cond, extra=""):
    global ok
    assert cond, f"FAIL: {label} {extra}"
    ok += 1
    print(f"  ok  {label} {extra}")


print("== 1. 结算单目录与字段双兼容 ==")
check("SETTLEMENT_DIR 指向项目内 结算单/",
      os.path.normcase(os.path.abspath(S.SETTLEMENT_DIR)) == os.path.normcase(os.path.abspath(SETTLE_DIR)),
      S.SETTLEMENT_DIR)

latest = json.load(open(os.path.join(SETTLE_DIR, "settlement_meta.json"), encoding="utf-8"))["latest"]
full_path = os.path.join(SETTLE_DIR, f"full_{latest}.json")
full = json.load(open(full_path, encoding="utf-8"))
prices = S.load_settlement_prices(full)   # {sym: 今结算价}
costs = S.load_settlement_cost(full)      # {sym}_{long|short}: 开仓均价

check("load_settlement_prices 非空", len(prices) > 0, f"{len(prices)} 条")
check("load_settlement_cost 非空", len(costs) > 0, f"{len(costs)} 条")
check("price 键=纯 symbol（无方向后缀）", all("_" not in k or "-" in k for k in prices), str(list(prices)[:2]))
check("cost 键={sym}_{long|short}", all(k.endswith(("_long", "_short")) for k in costs), str(list(costs)[:2]))
check("方向已归一为英文", not any(k.endswith(("_多", "_空")) for k in costs))
check("结算价有正数", any(v > 0 for v in prices.values()))
_full_dates = sorted(f[5:-5] for f in os.listdir(SETTLE_DIR) if f.startswith("full_") and f.endswith(".json"))
check("meta.latest 为最近有效结算单", latest == _full_dates[-1], f"{latest} vs {_full_dates[-1]}")
check("_is_valid_settlement(真结算单)=True", S._is_valid_settlement(full_path))
check("_valid_dates 含 latest", latest in S._valid_dates(), str(sorted(S._valid_dates())))

print("== 2. meta 进程内刷新（Bug: 常驻实例停在旧日期）==")
mgr = S.SettlementManager()
mgr._meta = {"latest": "19990101", "loaded": True}
mgr.load_costs_from_meta()
check("load_costs_from_meta 重读磁盘 meta", mgr._meta.get("latest") == latest, str(mgr._meta))
check("刷新后缓存已加载", len(mgr._price_cache) > 0 and len(mgr._cost_cache) > 0,
      f"price={len(mgr._price_cache)} cost={len(mgr._cost_cache)}")

print("== 3. 收盘 Mark 采样与落盘（v1.4）==")
import datetime as _real_dt                              # noqa: E402


class _DT:
    """替身 datetime.datetime：now() 可固定，用于驱动收盘窗口"""
    _now = None

    @classmethod
    def now(cls):
        return cls._now

    @staticmethod
    def timedelta(*a, **k):
        return _real_dt.timedelta(*a, **k)


class _DTMod:
    datetime = _DT
    timedelta = _real_dt.timedelta
    time = _real_dt.time


def at(hhmmss):
    _DT._now = _real_dt.datetime(2026, 9, 18, hhmmss // 10000, hhmmss // 100 % 100, hhmmss % 100)


saved_dt, saved_snap = A.datetime, A._snapshot
A.datetime = _DTMod
tmp = tempfile.mkdtemp(prefix="snapcheck_")
saved_dir = A._SNAPSHOT_DIR
A._SNAPSHOT_DIR = tmp


def fake_snapshot(positions, marks, status="connected"):
    tree = [{"symbol": s, "direction": d, "adjust_price": p, "children": []}
            for (s, d, p) in marks]
    return {"ctp_status": status, "positions": positions, "underlying_prices": {},
            "summary": {}, "tree": tree, "account": {}}


try:
    saved_vd = A._valid_dates
    A._valid_dates = lambda: {"20260917", "20260918"}   # 交易日历桩：今日 0918，T-1=0917
    pos = [{"symbol": "IF2609", "direction": "long", "volume": 2}]

    # 3a 采样窗内：每轮追加一个样本，重复值不去重（= 按时间加权）
    A._MARK_SAMPLES.clear(); A._CLOSE_SAVED.clear(); A._SAMPLE_BD = ""
    A._SEEN_NONEMPTY_POS = True
    A._snapshot = lambda: fake_snapshot(pos, [("IF2609", "long", 4000.0)], "connected")
    at(145600); A._close_snapshot_step()
    at(145601); A._close_snapshot_step()
    at(145602); A._close_snapshot_step()
    check("14:55–15:00 每轮记一个样本", A._MARK_SAMPLES["IF2609_long"] == [4000.0] * 3,
          str(A._MARK_SAMPLES))
    check("采样窗内不落盘", not os.path.exists(os.path.join(tmp, "close_snapshot_20260918.json")))

    # 3b 非 connected 不采样
    n_before = len(A._MARK_SAMPLES["IF2609_long"])
    A._snapshot = lambda: fake_snapshot(pos, [("IF2609", "long", 4000.0)], "connecting")
    at(145610); A._close_snapshot_step()
    check("未连接 → 不采样", len(A._MARK_SAMPLES["IF2609_long"]) == n_before)
    A._snapshot = lambda: fake_snapshot(pos, [("IF2609", "long", 4006.0)])

    # 3c 15:00 落盘：算术平均 + price_basis=close_avg + samples 计数
    at(145959); A._close_snapshot_step()          # 4006 入缓冲
    at(150001); A._close_snapshot_step()
    f = os.path.join(tmp, "close_snapshot_20260918.json")
    check("15:00 后第一轮落盘", os.path.isfile(f))
    d = json.load(open(f, encoding="utf-8"))
    leaf = d["leaves"]["IF2609_long"]
    check("adjust_price = 窗口 Mark 算术平均",
          leaf["adjust_price"] == round((4000.0 * 3 + 4006.0) / 4, 4), str(leaf))
    check("price_basis = close_avg", leaf["price_basis"] == "close_avg")
    check("samples = 窗口内样本数", leaf["samples"] == 4, str(leaf))
    check("快照记 window 与 trading_date",
          d["window"] == {"start": "14:55:00", "end": "15:00:00"} and d["trading_date"] == "20260918",
          str(d.get("window")))
    check("无 session 字段（时段体系已废）", "session" not in d)
    check("落盘后缓冲清空", A._MARK_SAMPLES == {})

    # 3d 同业务日不重复写（15:05 再调也不覆盖）
    first = open(f, encoding="utf-8").read()
    at(150500); A._close_snapshot_step()
    check("每业务日一份，不覆盖不重写", open(f, encoding="utf-8").read() == first)

    # 3e 假空仓：本连接内没见过非空持仓 → 不落盘
    os.remove(f)
    A._MARK_SAMPLES.clear(); A._CLOSE_SAVED.clear(); A._SAMPLE_BD = ""
    A._SEEN_NONEMPTY_POS = False
    A._snapshot = lambda: fake_snapshot([], [("IF2609", "long", 4000.0)])
    at(145600); A._close_snapshot_step()
    at(150001); A._close_snapshot_step()
    check("空持仓且无佐证 → 不落盘", not os.path.isfile(f))

    # 3f 真平仓：见过非空持仓后变空 → 照存 snapshot_kind=empty
    A._SEEN_NONEMPTY_POS = True
    at(150002); A._close_snapshot_step()
    check("空持仓但有佐证 → 落盘 snapshot_kind=empty",
          os.path.isfile(f) and json.load(open(f, encoding="utf-8"))["snapshot_kind"] == "empty")

    # 3g 错过采样窗（缓冲空）→ 放弃当日，不出空文件
    os.remove(f)
    A._MARK_SAMPLES.clear(); A._CLOSE_SAVED.clear(); A._SAMPLE_BD = ""
    at(150300); A._close_snapshot_step()
    check("采样缓冲为空 → 不落盘，T+1 降级结算价", not os.path.isfile(f))

    print("== 4. 昨收基准读取（只认 T-1 close_snapshot）==")

    def reset_cache():
        A._BASE_CACHE["bd"], A._BASE_CACHE["result"] = "", {}

    def write_close(date, leaves):
        json.dump({"version": 4, "trading_date": date, "leaves": leaves},
                  open(os.path.join(tmp, f"close_snapshot_{date}.json"), "w", encoding="utf-8"))

    # 4a 历史 data_snapshot_*（盘中/末价）一律不读
    if os.path.isfile(f):
        os.remove(f)
    json.dump({"version": 3, "raw": {"positions": pos},
               "computed": {"tree": [{"symbol": "IF2609", "direction": "long",
                                      "adjust_price": 9999.0, "children": []}]}},
              open(os.path.join(tmp, "data_snapshot_20260917_P.json"), "w", encoding="utf-8"))
    reset_cache(); at(100000)
    check("只有历史时段快照 → 无基准（不冒充昨收）", A._load_yesterday_snapshot() == {})

    # 4b T-1 收盘快照正常提取（leaves 键由写入端生成，读取端不猜测不改写）
    write_close("20260917", {
        "IF2609_long": {"adjust_price": 4520.0, "price_basis": "close_avg", "samples": 300},
        "IC2612_short": {"adjust_price": 7100.0, "price_basis": "close_avg", "samples": 300},
    })
    reset_cache()
    r = A._load_yesterday_snapshot()
    check("T-1 close_snapshot → 提取基准", r.get("IF2609_long", {}).get("adjust_price") == 4520.0, str(r))
    check("两条基准都入账", len(r) == 2, str(r))
    check("写入端把中文 direction 归一为英文键",
          A._leaf_marks([{"symbol": "IC2612", "direction": "空", "adjust_price": 1.0, "children": []}])
          == {"IC2612_short": 1.0})

    # 4c price_basis 非 close_avg → 拒作基准
    write_close("20260917", {
        "IF2609_long": {"adjust_price": 4520.0, "price_basis": "last_price", "samples": 1},
        "IC2612_short": {"adjust_price": 7100.0, "price_basis": "close_avg", "samples": 300},
    })
    reset_cache()
    r = A._load_yesterday_snapshot()
    check("price_basis 非 close_avg → 拒收", "IF2609_long" not in r and "IC2612_short" in r, str(r))

    # 4d 非法价格（None / NaN / <=0）跳过；样本数为 0 不降级（仍按已有值入账）
    write_close("20260917", {
        "IF2609_long": {"adjust_price": None, "price_basis": "close_avg", "samples": 0},
        "IC2612_short": {"adjust_price": float("nan"), "price_basis": "close_avg", "samples": 2},
        "MO2609_long": {"adjust_price": 5000.0, "price_basis": "close_avg", "samples": 1},
    })
    reset_cache()
    r = A._load_yesterday_snapshot()
    check("None/NaN 基准价不入账，samples=1 仍可用",
          set(r) == {"MO2609_long"}, str(r))

    # 4e 禁止跨业务日回退：T-1（结算单最近有效日=20260917）无快照 → 不取更老的 20260915
    os.remove(os.path.join(tmp, "close_snapshot_20260917.json"))
    write_close("20260915", {"IF2609_long": {"adjust_price": 3000.0,
                                             "price_basis": "close_avg", "samples": 300}})
    reset_cache()
    check("T-1 缺失 → 不跨业务日回退", A._load_yesterday_snapshot() == {})

    # 4f 今日快照不作基准
    write_close("20260918", {"IF2609_long": {"adjust_price": 3500.0,
                                             "price_basis": "close_avg", "samples": 300}})
    reset_cache()
    check("当日 close_snapshot 不作基准", A._load_yesterday_snapshot() == {})

    # 4f-2 回归（2026-09-18 夜盘事故）：结算单日历滞后一天（只到 0916），
    #      但 0918 收盘快照已落盘 → 快照即实据，必须作基准，不得降级结算价
    A._valid_dates = lambda: {"20260916"}
    reset_cache(); at(210000)                     # 夜盘：bd = 20260919
    r = A._load_yesterday_snapshot()
    check("结算单日历滞后 → 快照仍作基准（快照为主、结算单为备）",
          r.get("IF2609_long", {}).get("adjust_price") == 3500.0, str(r))
    A._valid_dates = lambda: {"20260917", "20260918"}
    at(100000)                                    # 还原时钟到日盘，不污染 4g

    # 4g 交易日历不可用时退化（带 WARNING）+ 同业务日缓存命中
    def _boom():
        raise RuntimeError("结算单目录不可读")
    A._valid_dates = _boom
    reset_cache()
    r = A._load_yesterday_snapshot()
    check("交易日历不可用 → 退最近快照日期（带 WARNING）",
          r.get("IF2609_long", {}).get("adjust_price") == 3000.0, str(r))
    A._BASE_CACHE["result"] = {"sentinel": True}
    check("同业务日命中缓存不重读盘", A._load_yesterday_snapshot() == {"sentinel": True})
finally:
    A.datetime, A._snapshot = saved_dt, saved_snap
    A._valid_dates = saved_vd
    A._SNAPSHOT_DIR = saved_dir
    A._MARK_SAMPLES.clear()
    A._CLOSE_SAVED.clear()
    A._SAMPLE_BD = ""
    A._SEEN_NONEMPTY_POS = False
    reset_cache()
    shutil.rmtree(tmp, ignore_errors=True)

print("== 5. 当日盈亏现金口径（2026-09-28 定案：cash + 今市值 − 昨市值）==")
import datetime as _dt                              # noqa: E402
from dashboard_v2 import risk_engine as R           # noqa: E402

# 5a 夜盘门禁：夜盘窗口内无夜盘时段的品种（MO/IF/IC/IO…）当日盈亏强制 0
# 注意门禁比对的是 normalize_underlying(sym)：MO→IM 走 CFFEX_MAP，故传 "IM"
_mo_pos = {"symbol": "MO2610-P-7500", "direction": "short", "volume": 2, "price": 129.0}
_mo_con = {"MO2610-P-7500": {"size": 100, "option_type": "看跌期权", "strike": 7500,
                             "days_to_expiry": 20}}
_mo_tick = {"last_price": 129.0, "underlying_price": 7400.0, "iv": 0.25,
            "datetime": _dt.datetime.now()}
_mo_cash = {"MO2610-P-7500_short": 25800.0}          # 平 2 手 @129 卖出
_mo_prev = {"MO2610-P-7500_short": -24000.0}        # 昨 2 手 @120 义务仓市值

_r_gated = R.calc_pnl_today("MO2610-P-7500_short", -1, 2, 100, 129.0, _mo_cash, _mo_prev)
check("calc_pnl_today 手算 = 25,800 + (−25,800) − (−24,000) = 24,000", _r_gated == 24000.0, str(_r_gated))

_t_gated = R.build_tree([_mo_pos], {"MO2610-P-7500": _mo_tick}, _mo_con, {},
                        None, _mo_cash, _mo_prev, night_gated={"IM"})
_t_open = R.build_tree([_mo_pos], {"MO2610-P-7500": _mo_tick}, _mo_con, {},
                       None, _mo_cash, _mo_prev, night_gated=set())
_n_gated = _t_gated["tree"][0]["children"][0]["children"][0]
_n_open = _t_open["tree"][0]["children"][0]["children"][0]
check("夜盘门禁生效 → pnl_today=0 且 price_basis=not_open",
      _n_gated["pnl_today"] == 0.0 and _n_gated["price_basis"] == "not_open", str(_n_gated))
check("门禁非摆设：同一腿不门禁时出 24,000（去掉门禁即 2.4 万假盈亏）",
      _n_open["pnl_today"] == 24000.0 and _n_open["price_basis"] == "cash_flow", str(_n_open))

# 5b 多头正常路径：0 + 2×300×4550 − 2,640,000 = 90,000
_r = R.calc_pnl_today("IF2609_long", 1, 2, 300, 4550.0, {"IF2609_long": 0.0},
                      {"IF2609_long": 2640000.0})
check("多头含昨市值 = 90,000", _r == 90000.0, str(_r))

# 5c 昨市值缺失 → 只剩现金项（告警降级，不静默编造）
_r_no = R.calc_pnl_today("MO2610-P-7500_short", -1, 2, 100, 129.0, {}, {})
_r_yes = R.calc_pnl_today("MO2610-P-7500_short", -1, 2, 100, 129.0, {}, {"MO2610-P-7500_short": -24000.0})
check("无昨市值 → 全额 −25,800；有昨市值 → −1,800",
      _r_no == -25800.0 and _r_yes == -1800.0, f"{_r_no} / {_r_yes}")

# 5d vol=0 当日往返：开 2 手 @120 卖、平 2 手 @129 买回 → 净现金 +1,800，仍须显示
_r = R.calc_pnl_today("MO2610-P-7500_short", -1, 0, 100, 0.0,
                      {"MO2610-P-7500_short": 1800.0}, {})
check("vol=0 当日往返 → pnl_today=1,800（cash 净额，不被 vol 抹掉）", _r == 1800.0, str(_r))

# 5e 逐级可加：各级 = 各腿之和，无摊派
_pos = [{"symbol": "IF2609", "direction": "long", "volume": 2, "price": 4500.0},
        {"symbol": "IC2612", "direction": "short", "volume": 1, "price": 7000.0},
        {"symbol": "MO2610-P-7500", "direction": "short", "volume": 2, "price": 129.0}]
_con = {"IF2609": {"size": 300}, "IC2612": {"size": 200}, **_mo_con}
_tick = {"IF2609": {"last_price": 4550.0}, "IC2612": {"last_price": 7000.0},
         "MO2610-P-7500": _mo_tick}
_cash = {"IF2609_long": 0.0, "IC2612_short": 0.0, "MO2610-P-7500_short": 25800.0}
_prev = {"IF2609_long": 2640000.0, "IC2612_short": -1400000.0, "MO2610-P-7500_short": -24000.0}
_tree = R.build_tree(_pos, _tick, _con, {}, None, _cash, _prev)
_l3 = [n["pnl_today"] for l1 in _tree["tree"] for l2 in l1["children"] for n in l2["children"]]
_l2 = [l2["metrics"]["pnl_today"] for l1 in _tree["tree"] for l2 in l1["children"]]
_l1 = [l1["metrics"]["pnl_today"] for l1 in _tree["tree"]]
check("L3 逐腿（按品种序 IC/IF/MO）：0 / 90,000 / 24,000",
      _l3 == [0.0, 90000.0, 24000.0], str(_l3))
check("L2 = 各 L3 之和", all(abs(a - b) < 0.01 for a, b in zip(_l2, _l3)), str(_l2))
check("L1 = 各 L2 之和", all(abs(a - b) < 0.01 for a, b in zip(_l1, _l2)), str(_l1))
check("summary.total_pnl_today = Σ 各腿（无摊派）",
      _tree["summary"]["total_pnl_today"] == 114000.0 and _tree["summary"]["position_count"] == 3,
      f"{_tree['summary']['total_pnl_today']} / {_tree['summary']['position_count']}")

# 5f 快照/响应里不再出现 NaN 字面量（严格 JSON 可解析）
blob = json.dumps(A._clean_nan(_tree), ensure_ascii=False, allow_nan=False)
check("严格 JSON 序列化通过且无 NaN", "NaN" not in blob, "")

# ===========================================================================
# 6. 开仓成本三档链 + 成交账本持久化（当日盈亏已移出 calc_pnl）
# ===========================================================================
print("== 6. 开仓成本链 / 账本落盘重放 / 业务日过滤 ==")

_c6 = {"IF2609": {"size": 300}}
_t6 = {"last_price": 4550.0, "adjust_price": 4550.0, "datetime": _dt.datetime.now()}
_p6 = {"symbol": "IF2609", "direction": "long", "volume": 2, "price": 4500.0}

# 6a 结算单开仓均价优先
_r = R.calc_pnl(_p6, _c6["IF2609"], _t6, {"IF2609_long": 4400.0}, None)
check("成本档一：结算单 4400 → pnl_history=90,000 / settlement_cost",
      _r["cost_basis"] == "settlement_cost" and _r["pnl_history"] == 90000.0, str(_r))

# 6b 结算单缺 → 今日账本开仓价
_r = R.calc_pnl(_p6, _c6["IF2609"], _t6, {}, {"IF2609_long": 4464.0})
check("成本档二：账本 4464 → 51,600 / ledger_open_cost",
      _r["cost_basis"] == "ledger_open_cost" and _r["pnl_history"] == 51600.0, str(_r))

# 6c 两档都缺 → CTP 持仓均价
_r = R.calc_pnl(_p6, _c6["IF2609"], _t6, {}, {})
check("成本档三：CTP 持仓均价 4500 → 30,000 / position_price",
      _r["cost_basis"] == "position_price" and _r["pnl_history"] == 30000.0, str(_r))

# 6d vol=0 → closed，不留陈旧浮盈
_r = R.calc_pnl({"symbol": "IF2609", "direction": "long", "volume": 0, "price": 4500.0},
                _c6["IF2609"], _t6, {"IF2609_long": 4400.0}, None)
check("vol=0 → closed / pnl_history=0",
      _r == {"pnl_history": 0.0, "cost_price": 0.0, "cost_basis": "closed"}, str(_r))

# 6e 期权今开：CTP position.price=0 时成本落到账本开仓价，不留 0 假数
_p6o = {"symbol": "MO2610-P-7500", "direction": "short", "volume": 2, "price": 0.0}
_r = R.calc_pnl(_p6o, _mo_con["MO2610-P-7500"], _mo_tick, {}, {"MO2610-P-7500_short": 120.0})
check("期权今开 position.price=0 → 账本成本 120 / pnl_history=−1,800（空头涨 9 点亏）",
      _r["cost_basis"] == "ledger_open_cost" and _r["pnl_history"] == -1800.0, str(_r))

# ── 6f/6g/6h：账本业务日过滤（2026-09-29 修复）───────────────────────────
# 根因：_load_trade_ledger / _cash_flow_map / _accum_open_cost 漏 trading_day 过滤，
#       21:00 后 CTP 已切下一交易日，历史账本里的旧成交混进当日现金项（污染 1,227,740）。
_tmpdir = tempfile.mkdtemp(prefix="ledger_")
_saved = (A._SNAPSHOT_DIR, A._LEDGER_TRADING_DAY, dict(A._shared_state.get("contracts") or {}),
          A._TRADE_CACHE, A._SEEN_TRADE_IDS, A._REALIZED_PNL_CACHE, A._TODAY_OPEN_ACC)
A._SNAPSHOT_DIR = _tmpdir
A._shared_state["contracts"] = {"IF2609": {"size": 300}, "IC2612": {"size": 200}}
A._reset_trade_state()


def _rec(day, sym, direction, side, offset, px, vol, pnl, tid):
    return {"dedup_key": f"{day}_CTP_CFFEX_{tid}", "ledger_key": [day, "CTP", "CFFEX", sym, direction],
            "trading_day": day, "trade_id": str(tid), "symbol": sym,
            "position_direction": direction, "trade_side": side, "offset_flag": offset,
            "price": px, "volume": vol, "realized_pnl": pnl}


_today, _stale = "20260929", "20260928"
A._LEDGER_TRADING_DAY = _today
A._replay_trade_record(_rec(_today, "IF2609", "long", "long", "open", 4464.0, 8, 1000.0, 1))
A._replay_trade_record(_rec(_stale, "IC2612", "short", "short", "open", 7000.0, 1, 999999.0, 2))
check("_accum_open_cost 业务日过滤：只收当日开仓，昨日期不进",
      list(A._TODAY_OPEN_ACC.keys()) == ["IF2609_long"], str(A._TODAY_OPEN_ACC))

A._reset_trade_state()
A._LEDGER_TRADING_DAY = _today
for _r0 in (_rec(_today, "IF2609", "long", "short", "close_today", 4550.0, 8, 0.0, 1),
            _rec(_stale, "IC2612", "short", "short", "close_today", 7100.0, 1, 0.0, 2)):
    A._TRADE_CACHE.setdefault(tuple(_r0["ledger_key"]), []).append(_r0)
_cf = A._cash_flow_map()
check("_cash_flow_map 业务日过滤：IC2612 昨日期 71 万不进当日现金",
      _cf == {"IF2609_long": 8 * 300 * 4550.0}, str(_cf))

# 落盘 → 重置 → 重放：_LEDGER_TRADING_DAY 必须先于重放赋值，否则当日记录被滤光
A._reset_trade_state()
A._LEDGER_TRADING_DAY = _today
A._replay_trade_record(_rec(_today, "IF2609", "long", "long", "open", 4464.0, 8, 1000.0, 1))
A._replay_trade_record(_rec(_stale, "IC2612", "short", "short", "open", 7000.0, 1, 999999.0, 2))
A._save_trade_ledger()
A._reset_trade_state()
A._LEDGER_TRADING_DAY = ""
A._load_trade_ledger(_today)
check("_load_trade_ledger 逐条过滤：只重放 1 笔，已实现 1,000（99.9 万假账被挡住）",
      A._REALIZED_PNL_CACHE == {"IF2609": 1000.0}, str(A._REALIZED_PNL_CACHE))
check("重放时 _LEDGER_TRADING_DAY 已定 → 当日开仓成本进账（4464）",
      A._open_cost_map() == {"IF2609_long": 4464.0}, str(A._open_cost_map()))

# 切日（CTP TradingDay）→ 清零
A._rollover_trading_day("20260930")
check("切日 → 账本清零且 _LEDGER_TRADING_DAY=20260930",
      not A._REALIZED_PNL_CACHE and A._LEDGER_TRADING_DAY == "20260930",
      f"{A._REALIZED_PNL_CACHE} {A._LEDGER_TRADING_DAY}")

# 账本顶层日 ≠ CTP TradingDay → 整本丢弃（昨日账由结算单接管）
A._reset_trade_state()
A._LEDGER_TRADING_DAY = _stale
A._replay_trade_record(_rec(_stale, "IC2612", "short", "short", "open", 7000.0, 1, 999999.0, 2))
A._save_trade_ledger()
A._reset_trade_state()
A._LEDGER_TRADING_DAY = ""
A._load_trade_ledger(_today)
check("账本日(20260928) ≠ 交易日(20260929) → 整本丢弃，99.9 万不进当日",
      not A._REALIZED_PNL_CACHE and A._LEDGER_TRADING_DAY == _today, str(A._REALIZED_PNL_CACHE))

# 6i T-1 收盘兜底：一条口径，marks + underlying 读同一份 snapshot
_shots = tempfile.mkdtemp(prefix="prevclose_")
_saved2 = (A._SNAPSHOT_DIR, A._prev_trading_day_file, A._PREV_CLOSE_CACHE)
A._SNAPSHOT_DIR = _shots
with open(os.path.join(_shots, "close_snapshot_T1.json"), "w", encoding="utf-8") as _f:
    json.dump({"leaves": {
        "MO2610-P-7500_short": {"adjust_price": 120.0, "price_basis": "close_avg"},
        "IF2609_long":         {"adjust_price": 4444.0, "price_basis": "last"},
        "IC2609_long":         {"adjust_price": -1.0,  "price_basis": "close_avg"},
    }, "raw": {"underlying_prices": {
        "IM2610.CFFEX": 7316.0, "IF2609.CFFEX": 0, "BAD.CFFEX": "x",
    }}}, _f)
A._prev_trading_day_file = lambda bd: "close_snapshot_T1.json"
A._PREV_CLOSE_CACHE = {"bd": "", "marks": {}, "underlying": {}}
_marks, _unds = A._t1_close()
check("_t1_close marks 只收 close_avg，拒 price_basis=last 与非正价",
      _marks == {"MO2610-P-7500_short": 120.0}, str(_marks))
check("_t1_close 读 raw.underlying_prices 作期权 F，拒非数值/非正价",
      _unds == {"IM2610.CFFEX": 7316.0}, str(_unds))
check("未开盘腿兜底：命中拿 T-1 收盘 7316.0，miss 归零不抛异常（一致性口径）",
      _marks.get("IF2609_long", 0.0) == 0.0
      and _unds.get("IF2609.CFFEX", 0.0) == 0.0
      and _unds.get("IM2610.CFFEX", 0.0) == 7316.0)
# 按业务日缓存：同 bd 二次调用不再读文件；换 bd 才重载
A._PREV_CLOSE_CACHE = {"bd": "", "marks": {}, "underlying": {}}
A._prev_trading_day_file = lambda bd: "close_snapshot_MISSING.json"
check("_t1_close 换业务日重载，snapshot 缺失时两半都空（不抛）",
      A._t1_close("20260101") == ({}, {}) and A._t1_close("20260101") == ({}, {}))
A._SNAPSHOT_DIR, A._prev_trading_day_file, A._PREV_CLOSE_CACHE = _saved2

# 还原
A._SNAPSHOT_DIR, A._LEDGER_TRADING_DAY = _saved[0], _saved[1]
A._shared_state["contracts"] = _saved[2]
A._TRADE_CACHE, A._SEEN_TRADE_IDS = _saved[3], _saved[4]
A._REALIZED_PNL_CACHE, A._TODAY_OPEN_ACC = _saved[5], _saved[6]
shutil.rmtree(_tmpdir, ignore_errors=True)
shutil.rmtree(_shots, ignore_errors=True)

print(f"\nPASS {ok} 项")
