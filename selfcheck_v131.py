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
check("meta.latest 为最近有效结算单", latest == "20260917", latest)
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

print("== 5. 无行情 → pnl_today=None，且不影响账户汇总 ==")
import datetime as _dt                              # noqa: E402
from dashboard_v2 import risk_engine as R           # noqa: E402

pos_if = {"symbol": "IF2609", "direction": "long",  "volume": 2, "price": 4500.0}
pos_ic = {"symbol": "IC2612", "direction": "short", "volume": 1, "price": 7000.0}
contracts = {"IF2609": {"size": 300}, "IC2612": {"size": 200}}
ticks = {"IF2609": {"last_price": 4550.0, "datetime": _dt.datetime.now()}}   # IC2612 无 tick
settle_cost = {"IF2609_long": 4400.0}
settle_px = {"IF2609": 4460.8}
recv = {"IF2609": True, "IC2612": True}

# 5a tick 缺失 → None
r = R.calc_pnl(pos_ic, contracts["IC2612"], {}, settle_cost, settle_px, None, recv)
check("无 tick → pnl_today=None", r["pnl_today"] is None, str(r))
check("无 tick → price_basis=no_tick / pnl_history=0（不留陈旧值）",
      r["price_basis"] == "no_tick" and r["pnl_history"] == 0.0, str(r))

# 5b 有 tick 但是往日报价（重连后 vnpy 缓存）→ None
stale = {"last_price": 4550.0, "datetime": _dt.datetime.now() - _dt.timedelta(days=1)}
r = R.calc_pnl(pos_if, contracts["IF2609"], stale, settle_cost, settle_px, None, recv)
check("陈旧缓存价（非今日 datetime）→ pnl_today=None", r["pnl_today"] is None, str(r))

# 5c 有今日 tick 但无基准（今仓）→ None
r = R.calc_pnl(pos_ic, contracts["IC2612"], {"last_price": 7000.0, "datetime": _dt.datetime.now()},
               {}, {}, None, recv)
check("有 tick 无基准（今仓）→ pnl_today=None", r["pnl_today"] is None, str(r))

# 5d 正常路径仍出数
r = R.calc_pnl(pos_if, contracts["IF2609"], ticks["IF2609"], settle_cost, settle_px, None, recv)
check("正常路径 pnl_today=53520", r["pnl_today"] == 53520.0, str(r))

# 5e 汇总不因 None 行受污染
tree = R.build_tree([pos_if, pos_ic], ticks, contracts, settle_cost, settle_px, None, recv)
rows = {n["symbol"]: n for l1 in tree["tree"] for l2 in l1["children"] for n in l2["children"]}
check("L3：IF2609 有值 / IC2612 为 None",
      rows["IF2609"]["pnl_today"] == 53520.0 and rows["IC2612"]["pnl_today"] is None,
      f"IF={rows['IF2609']['pnl_today']} IC={rows['IC2612']['pnl_today']}")
check("total_pnl_today = 仅有效行之和（None 不累加）",
      tree["summary"]["total_pnl_today"] == 53520.0, str(tree["summary"]["total_pnl_today"]))

# 5f 快照/响应里不再出现 NaN 字面量（严格 JSON 可解析）
blob = json.dumps(A._clean_nan(tree), ensure_ascii=False, allow_nan=False)
check("严格 JSON 序列化通过且无 NaN", "NaN" not in blob, "")

# ===========================================================================
# 6. 今开仓腿基准 + 成交账本持久化（2026-09-18 定案：今开按开仓价）
# ===========================================================================
print("== 6. 今开仓腿基准 / 成交账本落盘重放 ==")
import datetime as _dt2  # noqa: E402

_t = {"last_price": 4478.2, "adjust_price": 4478.2, "datetime": _dt2.datetime.now()}
_p = {"symbol": "IF2610", "direction": "long", "volume": 8, "price": 4464.0}
_c = {"IF2610": {"size": 300}}

# 6a 今开腿（无昨收/昨结）→ 基准=账本开仓价
_r = R.calc_pnl(_p, _c["IF2610"], _t, {}, {}, None, {"IF2610": True}, {"IF2610_long": 4464.0})
check("今开腿基准=开仓价（IF2609 平昨 73,968 + IF2610 今开 34,080 ≈ 老系统 108,500）",
      abs(_r["pnl_today"] - 34080.0) < 1 and _r["price_basis"] == "today_open_cost", str(_r))

# 6b 昨结存在 + 今开 → 昨结为准并标 mixed（单一基准无法拆分今昨手数）
_r = R.calc_pnl(_p, _c["IF2610"], _t, {}, {"IF2610": 4460.8}, None, {"IF2610": True},
                {"IF2610_long": 4464.0})
check("昨结+今开 → 标 today_open_mixed 待核",
      _r["price_basis"] == "prev_settlement_fallback+today_open_mixed", str(_r))

# 6c 期权今开（CTP position.price=0）→ 成本取账本开仓价，不留 -30,775 假数
_p2 = {"symbol": "MO2610-P-7500", "direction": "short", "volume": -2, "price": 0.0}
_t2 = {"last_price": 129.8, "adjust_price": 129.0, "datetime": _dt2.datetime.now()}
_r = R.calc_pnl(_p2, {"size": 100}, _t2, {}, {}, None, {"MO2610-P-7500": True},
                {"MO2610-P-7500_short": 129.0})
check("期权今开成本取账本开仓价（无 position.price 假数）",
      _r["cost_basis"] == "ledger_open_cost" and _r["pnl_history"] == 0.0, str(_r))

# 6d 账本落盘 → 重放 → 切日清零
_tmpdir = tempfile.mkdtemp(prefix="ledger_")
_old_dir, _old_day = A._SNAPSHOT_DIR, A._LEDGER_TRADING_DAY
A._SNAPSHOT_DIR = _tmpdir
A._reset_trade_state()
A._LEDGER_TRADING_DAY = "20260918"
A._replay_trade_record({"dedup_key": "20260918_CTP_CFFEX_1",
                        "ledger_key": ["20260918", "CTP", "CFFEX", "IF2609", "long"],
                        "trade_id": "1", "symbol": "IF2609", "position_direction": "long",
                        "offset_flag": "close_yesterday", "price": 4491.62, "volume": 8,
                        "realized_pnl": 73968.0})
A._save_trade_ledger()
A._reset_trade_state()
A._LEDGER_TRADING_DAY = ""
A._load_trade_ledger("20260918")
check("重启重放：已实现 73,968 不归零",
      abs(A._REALIZED_PNL_CACHE.get("IF2609", 0) - 73968.0) < 0.01, str(A._REALIZED_PNL_CACHE))
A._rollover_trading_day("20260919")
check("切日（CTP TradingDay）→ 账本清零",
      not A._REALIZED_PNL_CACHE and A._LEDGER_TRADING_DAY == "20260919", "")
A._SNAPSHOT_DIR, A._LEDGER_TRADING_DAY = _old_dir, _old_day
A._reset_trade_state()
shutil.rmtree(_tmpdir, ignore_errors=True)

print(f"\nPASS {ok} 项")
