"""
dashboard_v2/api_server.py
────────────────────────────
前后端分离架构：纯数据接口，零 CTP 业务逻辑
CTP Worker 线程负责持仓轮询 + 行情订阅 + Greeks 计算，写入共享内存
Flask API 层只读共享内存，响应 HTTP
"""

import copy
import datetime
import hashlib
import json
import threading
import time
import uuid
from collections import defaultdict, deque
from queue import Queue
from typing import Optional

from flask import Blueprint, Flask, jsonify, request

def _clean_nan(obj):
    """递归将 float('nan') / float('inf') 替换为 None，避免 JSON 序列化失败"""
    if isinstance(obj, dict):
        return {k: _clean_nan(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_clean_nan(v) for v in obj]
    elif isinstance(obj, float):
        import math
        if math.isnan(obj) or math.isinf(obj):
            return None
        return obj
    return obj

# ── 项目内部 ─────────────────────────────────────────────────────────────────
import sys as _sys
import os as _os
# 确保 dashboard_v2 在路径中
_dashboard_dir = _os.path.dirname(_os.path.abspath(__file__))
_parent_dir = _os.path.dirname(_dashboard_dir)
if _parent_dir not in _sys.path:
    _sys.path.insert(0, _parent_dir)

from vnpy.event import Event
from vnpy.trader.constant import Direction, Offset, Product, Exchange
from vnpy_engine import VNPYEngine
from loguru import logger
from dashboard_v2.risk_engine import build_tree, normalize_underlying, cp_from_symbol
from dashboard_v2.pricing import price_options_batch, days_to_expiry, black76
from dashboard_v2.settlement import SettlementManager, _valid_dates, _cutoff_date as _sm_cutoff, _scanned_dates as _sm_scanned
from dashboard_v2.alert_config import load_alert_settings as _load_alerts, save_alert_settings as _save_alerts, get_threshold as _get_thresh
from dashboard_v2.alert_config import load_sigma_ref as _load_sigma, sigma_ref_missing as _sigma_missing, append_sigma_symbols as _append_sigma

# ── 共享状态（Worker 写，API 读，无锁 Python 对象）────────────────────────────
# 均为 Python 对象，无锁，Worker 线程写，Flask API 读
# API 读取时是原子快照引用，不会有撕裂问题

_shared_lock = threading.RLock()          # 保护 _ctp_status / _worker_thread
_SERVER_START = time.time()               # 服务进程启动时间（uptime 基准，非客户端计时）
_COLUMNS_LOADED = False   # 列配置是否已从盘载入（进程级，首次 GET/POST 触发）
_shared_state = {
    "positions": [],          # list[dict]  最新持仓快照
    "underlying_prices": {},  # {symbol: price}
    "tree": [],               # list  树形结构
    "contracts": {},          # {symbol: contract_dict}  合约元数据（含 size/strike/days_to_expiry）
    "settlement_dict": {},    # {symbol: net_cost}       净仓开仓均价
    "account": {},            # dict  账户信息
    "ctp_status": "disconnected",   # "disconnected" | "connecting" | "connected" | "error"
    "ctp_error": "",          # str   错误信息
    "instance_status": "running",   # "running" | "stopping"
    "last_update": None,      # datetime  最后更新时间
    "worker_alive": False,    # bool  Worker 线程是否存活
    "column_config": [        # 列配置（顺序、显隐、fmt掩码）
        {"col": "symbol",           "label": "合约",      "visible": True,  "fmt": None},
        {"col": "volume",           "label": "数量",      "visible": True,  "fmt": "0"},
        {"col": "last_price",       "label": "最新价",    "visible": True,  "fmt": "0.00"},
        {"col": "adjust_price",     "label": "调整价",    "visible": True,  "fmt": "0.0000"},
        {"col": "open_price",       "label": "开仓价",    "visible": True,  "fmt": "0.00"},
        {"col": "underlying_price", "label": "标的价",    "visible": False, "fmt": "0.00"},
        {"col": "iv",               "label": "IV%",       "visible": True,  "fmt": "0.00"},
        {"col": "delta",            "label": "Δ",         "visible": True, "fmt": "0.0000"},
        {"col": "gamma",            "label": "Γ",         "visible": True, "fmt": "0.000000"},
        {"col": "vega",             "label": "Vega",      "visible": True, "fmt": "0.0000"},
        {"col": "deltacash",        "label": "ΔCash",     "visible": True, "fmt": "0"},
        {"col": "gammacash",        "label": "ΓCash",     "visible": True, "fmt": "0"},
        {"col": "vegacash",         "label": "VegaCash",  "visible": True, "fmt": "0"},
        {"col": "thetacash",        "label": "ΘCash",     "visible": True, "fmt": "0"},
        {"col": "days_to_expiry",   "label": "剩余天",    "visible": True, "fmt": "0"},
        {"col": "pnl_today",        "label": "当日盈亏",  "visible": True, "fmt": "0"},
        {"col": "pnl_history",      "label": "浮动盈亏",  "visible": True, "fmt": "0"},
    ],
    # 监控预警状态（Ring buffer + 冷却计数 + 速率采样）
    "alerts": [],                 # list[dict] 最多 100 条
    "active_flags": {},           # {品种: "warn"|"danger"} 当前仍触发的聚合行
    "active_details": {},         # {源: {键: "warn"|"danger"}} 单元格级标记（conv_delta 键=合约码）
    "contract_und": {},           # {合约代码: 品种} 前端归一键（MO→IM 等别名）
    "popups": [],                 # 待弹窗队列（累加不覆盖，前端按 alert_id|ts 去重）
    "alert_state": {},            # {alert_id: {date, count, last_popup}}
    "_f_samples": {},             # {und: deque([(ts, price), ...])} 标的价 5min 滚动窗口
    "_iv_samples": {},            # {und: deque([(ts, iv), ...])} 5min 滚动窗口
    "hours_missing": set(),       # 交易时段表查不到、已降级 4h 的品种
}

# ── CTP 连接参数（由 /api/ctp/connect 设置，Worker 启动时读取）────────────────
# 全链路中文键：{用户名, 密码, 经纪商代码, 交易服务器, 行情服务器, 产品名称, 授权编码}
_ctp_credential = {}       # 中文键 dict，直接作为 VNPYEngine.cctp_setting 传入
_ctp_stop_event = threading.Event()
_worker_thread: Optional[threading.Thread] = None

# ── P0-2/3/5: CTP 成交回报账本（进程内，重启丢失）─────────────────────────────
# 分组键：不含 offset（同一持仓方向的开仓和平仓记录共同参与 PnL 计算）
# _trade_cache: dict[ledger_key_tuple, list[trade_record]]
# ledger_key = (trading_day, account, exchange, symbol, position_direction)
_TRADE_CACHE: dict = {}
_SEEN_TRADE_IDS: set = set()   # 幂等去重：(trading_day, account, exchange, trade_id)

# ── P0-6: 已实现 PnL 实时累加通道 ─────────────────────────────────────────────
# 全平合约从持仓 tree 消失，但其 realized PnL 必须进入当日和历史汇总
_REALIZED_PNL_CACHE: dict[str, float] = {}   # key = symbol, value = 累计 realized PnL

# ── F2: 成交账本持久化 + 今日开仓加权价（重启不丢当日已实现盈亏）──────────────
# 文件：快照/trade_ledger.json = {"trading_day": "YYYYMMDD", "trades": [record…]}
# record 内自带 realized_pnl / cost_price / cost_basis → 重启按原值重放，不重算
# 交易日以 CTP TradingDay 为权威：切日即清零（昨日的账由结算单接管）
_LEDGER_FILE_NAME = "trade_ledger.json"
_LEDGER_TRADING_DAY: str = ""           # 当前内存账本所属交易日
_TODAY_OPEN_ACC: dict[str, list] = {}   # f"{sym}_{pos_dir}" → [Σ(价×量), Σ量]
_BASIS_WARN_SIGNATURE: str = ""         # 上次基准告警签名，避免每轮 poll 刷日志


def _ledger_path() -> str:
    return _os.path.join(_SNAPSHOT_DIR, _LEDGER_FILE_NAME)


def _reset_trade_state() -> None:
    _TRADE_CACHE.clear()
    _SEEN_TRADE_IDS.clear()
    _REALIZED_PNL_CACHE.clear()
    _TODAY_OPEN_ACC.clear()


def _accum_open_cost(rec: dict) -> None:
    """开仓成交累加今日开仓加权成本。"""
    p = float(rec.get('price', 0) or 0)
    v = int(rec.get('volume', 0) or 0)
    if p <= 0 or v <= 0:
        return
    k = f"{rec.get('symbol')}_{rec.get('position_direction')}"
    acc = _TODAY_OPEN_ACC.setdefault(k, [0.0, 0])
    acc[0] += p * v
    acc[1] += v


def _open_cost_map() -> dict:
    """今日开仓加权价 → calc_pnl 的 today_open_cost（key 与 {sym}_{direction} 对齐）。"""
    return {k: amt / vol for k, (amt, vol) in _TODAY_OPEN_ACC.items() if vol > 0}


def _replay_trade_record(rec: dict) -> None:
    """从落盘记录重建内存账本（按 dedup_key 幂等）。"""
    dk = rec.get("dedup_key") or ""
    key = tuple(rec.get("ledger_key") or [])
    if not dk or len(key) != 5 or dk in _SEEN_TRADE_IDS:
        return
    _SEEN_TRADE_IDS.add(dk)
    _TRADE_CACHE.setdefault(key, []).append(rec)
    if rec.get("offset_flag") == "open":
        _accum_open_cost(rec)
    sym = rec.get("symbol", "")
    _REALIZED_PNL_CACHE[sym] = _REALIZED_PNL_CACHE.get(sym, 0.0) + float(rec.get("realized_pnl", 0.0) or 0.0)


def _save_trade_ledger() -> None:
    """每笔成交后原子落盘。ponytail: 全量重写，日内成交条数有限；上千条时改追加写。"""
    try:
        trades = [r for lst in _TRADE_CACHE.values() for r in lst]
        path = _ledger_path()
        _os.makedirs(_os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"trading_day": _LEDGER_TRADING_DAY, "trades": trades},
                      f, ensure_ascii=False, indent=1)
            f.flush()
            _os.fsync(f.fileno())
        _os.replace(tmp, path)
    except Exception:
        logger.exception("[ledger] 落盘失败（内存账本仍有效，重启会丢当日已实现）")


def _load_trade_ledger(expected_day: str = "") -> None:
    """启动时重放账本。expected_day 为空 → 先装载，待 CTP TradingDay 到手再校验。"""
    global _LEDGER_TRADING_DAY
    try:
        with open(_ledger_path(), encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return
    except Exception:
        logger.exception("[ledger] 读取失败，今日已实现盈亏从 0 起算")
        return
    td = str(data.get("trading_day") or "")
    if expected_day and td and td != expected_day:
        logger.info(f"[ledger] 账本日 {td} ≠ 交易日 {expected_day} → 丢弃，等结算单接管")
        _LEDGER_TRADING_DAY = expected_day
        return
    trades = data.get("trades") or []
    _reset_trade_state()
    for rec in trades:
        _replay_trade_record(rec)
    _LEDGER_TRADING_DAY = td or expected_day
    if trades:
        logger.info(f"[ledger] 重放 {len(trades)} 笔（交易日 {td}），已实现合计 {sum(_REALIZED_PNL_CACHE.values()):.2f}")


def _rollover_trading_day(td: str) -> None:
    """以 CTP TradingDay 为账本分区键；切日清零。"""
    global _LEDGER_TRADING_DAY
    if not td:
        return
    if not _LEDGER_TRADING_DAY:
        _load_trade_ledger(td)
        if not _LEDGER_TRADING_DAY:
            _LEDGER_TRADING_DAY = td
    if _LEDGER_TRADING_DAY != td:
        logger.info(f"[ledger] 交易日切换 {_LEDGER_TRADING_DAY} → {td}，账本清零")
        _reset_trade_state()
        _LEDGER_TRADING_DAY = td


def _ctp_trading_day() -> str:
    """CTP 权威交易日（YYYYMMDD）。CtpTdApi 登录时保存，未登录返回 ''。"""
    try:
        eng = _engine
        gw = eng.main_engine.gateways.get("CTP") if (eng and eng.main_engine) else None
        if gw and getattr(gw, "td_api", None):
            return str(getattr(gw.td_api, "trading_day", "") or "")
    except Exception:
        pass
    return ""

# ── P0-8: 开盘合约标记（事件驱动：收到 tick 即标记）────────────────────────────
_OPENED_CONTRACTS: dict[str, dict] = {}  # key = symbol, value = {first_tick: "HH:MM:SS"}

# ── P0-8 补充：行情推送日标记（received_today）─────────────────────────────────
# 进程内记录，Tick 断线重连/新合约上线/隔夜后首笔行情均自动重置
_RECEIVED_TODAY_DATE: str = ""          # 当前已激活的交易日（YYYYMMDD）
_RECEIVED_TODAY: dict[str, bool] = {}   # {sym: True}  收到今日行情推送的合约集合

# ── 收盘快照（v1.4）：每业务日仅 15:00 一份，基准 = 收盘前窗口 Mark 算术平均 ──
# 采样：14:55:00–15:00:00 每轮 _poll_once 之后读当时的 adjust_price（Mark，盘口驱动，不依赖成交）
# 写盘：15:00 之后第一轮 poll；15:00–15:10 为失败重试窗；写完即锁定，不覆盖不重写
# 口径：不用末成交价（期权尾盘稀疏 + 做市商撤单），不做成交量加权，重复样本不去重（等价按时间加权）
_CLOSE_PREFIX = "close_snapshot_"
_CLOSE_SAMPLE_START = datetime.time(14, 55, 0)
_CLOSE_SAMPLE_END   = datetime.time(15, 0, 0)
_CLOSE_RETRY_END    = datetime.time(15, 10, 0)
_MARK_SAMPLES: dict[str, list] = {}     # {f"{sym}_{direction}": [mark, ...]} 窗口内时间等差样本
_SAMPLE_BD: str = ""                    # 当前采样缓冲所属业务日（跨日清空）
_CLOSE_SAVED: set = set()               # 已落盘（或已放弃）的业务日
_SEEN_NONEMPTY_POS: bool = False        # 本连接内是否见过非空持仓 —— 真空仓的唯一佐证

# ── 工具函数 ──────────────────────────────────────────────────────────────────

def _now_str():
    return datetime.datetime.now().strftime("%H:%M:%S")


def _snapshot():
    """返回共享状态的原子快照（字典浅拷贝）。
    tree 在 _poll_once 中由 build_tree 生成，结构为 {"summary": {...}, "tree": [...]}。
    """
    with _shared_lock:
        tree_obj = _shared_state["tree"] or {"summary": {}, "tree": []}
        return {
            "positions": list(_shared_state["positions"]),
            "underlying_prices": dict(_shared_state["underlying_prices"]),
            "contracts": dict(_shared_state["contracts"]),
            "settlement_dict": dict(_shared_state["settlement_dict"]),
            "settlement_prices": dict(_shared_state.get("settlement_prices", {})),
            "summary": tree_obj.get("summary", {}),
            "tree": tree_obj.get("tree", []),
            "account": dict(_shared_state["account"]),
            "ctp_status": _shared_state["ctp_status"],
            "ctp_error": _shared_state["ctp_error"],
            "last_update": _shared_state["last_update"].strftime("%H:%M:%S") if _shared_state["last_update"] else "--:--:--",
            "worker_alive": _shared_state["worker_alive"],
            "uptime_seconds": int(time.time() - _SERVER_START),
            "margin_status": _shared_state.get("margin_status", {}),
            "hours_missing": sorted(_shared_state.get("hours_missing") or []),
            "alerts": list(_shared_state.get("alerts", []))[-_ALERT_RING_MAX:],
            "active_flags": dict(_shared_state.get("active_flags", {})),
            "active_details": {k: dict(v) for k, v in (_shared_state.get("active_details") or {}).items()},
            "contract_und": dict(_shared_state.get("contract_und", {})),
            "popups": list(_shared_state.get("popups", []))[-_ALERT_RING_MAX:],
        }


_settlement_manager: Optional[SettlementManager] = None


def _fetch_settlement(settlement_dir: str):
    """加载结算单，返回 {symbol: {avg_buy_price, avg_sell_price, ...}}"""
    global _settlement_manager
    if _settlement_manager is None:
        _settlement_manager = SettlementManager()
        _settlement_manager.load_costs_from_meta()
    return _settlement_manager.get_all_costs()


# ── CTP Worker 线程 ─────────────────────────────────────────────────────────

# 全局 engine 实例（Worker 内部创建/重连时替换）
_engine: Optional[VNPYEngine] = None

# 重连阶梯参数
_RETRY_FAST_INTERVAL = 3        # 秒
_RETRY_FAST_ATTEMPTS = 10       # fast 阶段次数
_RETRY_IDLE_INTERVAL = 1800     # 30min 兜底重试


def _set_status(status: str, error: str = None, alive: bool = None):
    """写共享状态；进入 connected 时重置"真空仓佐证"（新连接内必须重新见过非空持仓）。"""
    global _SEEN_NONEMPTY_POS
    with _shared_lock:
        prev = _shared_state["ctp_status"]
        _shared_state["ctp_status"] = status
        if error is not None:
            _shared_state["ctp_error"] = error
        if alive is not None:
            _shared_state["worker_alive"] = alive
    if status == "connected" and prev != "connected":
        _SEEN_NONEMPTY_POS = False
    elif status != "connected":
        _SEEN_NONEMPTY_POS = False


def _td_logged_in(engine: VNPYEngine) -> bool:
    """CTP TD 登录探测原语：gateway.onFrontDisconnected 会把 login_status 置 False。
    比不可靠的 get_start()（仅表示 run() 跑过 connect()）更真实反映链路存活。
    """
    try:
        gw = engine.main_engine.gateways.get("CTP")
        if not gw or not getattr(gw, "td_api", None):
            return False
        return bool(gw.td_api.login_status)
    except Exception:
        return False


def _td_login_error(engine: VNPYEngine):
    """读取 CTP TD 登录错误码/信息（best-effort）。返回 (id, msg)。"""
    try:
        if engine and engine.main_engine:
            gw = engine.main_engine.gateways.get("CTP")
            if gw and getattr(gw, "td_api", None):
                lid = getattr(gw.td_api, "login_error_id", 0) or 0
                lmsg = getattr(gw.td_api, "login_error_msg", "") or ""
                return lid, lmsg
    except Exception:
        pass
    return 0, ""


def _connect_engine(cred: dict):
    """创建 VNPYEngine 并轮询登录（max 30s）。

    返回 (engine_or_None, error_msg, retryable):
    - 登录成功        → (engine, "", True)
    - 登录被拒(错误码) → (None, "验证失败(码): msg", False)   重试无意义
    - 构造/run 抛异常  → (None, "网络通信异常: e", True)       停服/抖动，重试
    - 30s 超时未登录   → (None, "无法连接服务器(地址/端口/网络)", True)
    """
    try:
        cred["柜台环境"] = "实盘"
        eng = VNPYEngine(ctp_setting=dict(cred), type="CTP")
        t = threading.Thread(target=eng.run, daemon=True)
        t.start()
    except Exception as e:
        return None, f"网络通信异常: {e}", True

    # 三条失败出口一律不 close：CtpTdApi.exit() 持 GIL 阻塞会冻死整个解释器。
    # 旧引擎丢着等 GC（对齐旧生产版 _try_connect_once，泄漏一轮是可接受代价）。
    for _ in range(60):                       # 30s
        if _ctp_stop_event.is_set():
            return None, "已取消", False
        try:
            lid, lmsg = _td_login_error(eng)
            if lid:
                return None, f"验证失败({lid}): {lmsg or '用户名/密码/授权码/经纪商代码错误'}", False
            if eng.main_engine is not None and _td_logged_in(eng):
                return eng, "", True
        except Exception:
            pass
        time.sleep(0.5)
    return None, "无法连接服务器（检查交易/行情服务器地址、端口与网络）", True


# ── P0-2/3/5: CTP 成交回报回调 ─────────────────────────────────────────────
def _on_trade(trade) -> None:
    """
    CTP 成交通知回调（P0-2/3/5 账本模型 + P0-6 realized_pnl）。

    幂等去重：同一 (trading_day, account, exchange, trade_id) 只处理一次。
    账本分组：ledger_key = (trading_day, account, exchange, symbol, position_direction)
    方向守恒：同一分组内的开仓/平仓记录共同参与 PnL 计算。

    归因优先级：
      1. CTP 原生 offset_flag（open / close_today / close_yesterday）
      2. 降级推断（无 offset_flag 时）：由 open_close 推断，并标注 allocation_source="fifo_fallback"
    """
    try:
        dt_str = str(getattr(trade, 'datetime', '') or '')
        # 交易日以 CTP TradingDay 为权威（夜盘 21:00 的成交属下一业务日）
        # 取不到（未登录等）才退回成交自然日，仅作兜底
        trading_day = _ctp_trading_day() or dt_str[:10].replace('-', '') \
            or datetime.datetime.now().strftime('%Y%m%d')
        account  = getattr(trade, 'gateway_name', '') or ''
        exchange = getattr(trade, 'exchange', '') or ''
        if hasattr(trade, 'exchange') and hasattr(trade.exchange, 'value'):
            exchange = trade.exchange.value
        # 无 tradeid 时用 时间+价+量 合成，保证幂等去重与重放可用
        trade_id = getattr(trade, 'tradeid', '') or f"{dt_str}_{getattr(trade, 'price', 0)}_{getattr(trade, 'volume', 0)}"
    except Exception:
        return

    dedup_key = f"{trading_day}_{account}_{exchange}_{trade_id}"
    if dedup_key in _SEEN_TRADE_IDS:
        return
    _SEEN_TRADE_IDS.add(dedup_key)
    _rollover_trading_day(trading_day)

    try:
        symbol = getattr(trade, 'symbol', '') or ''
        if '.' in symbol:
            symbol = symbol.split('.')[0]
        raw_dir = str(getattr(trade, 'direction', '') or '')
        # vnpy 枚举：str(Direction.LONG)=='Direction.LONG'、.value=='多'
        # 旧代码拿枚举串去匹配 ('long','Long','B'…) → 永不命中，所有成交恒判 short，已实现盈亏符号全反
        dir_val = getattr(getattr(trade, 'direction', None), 'value', '') or ''
        trade_side = 'short' if (dir_val == '空' or 'SHORT' in raw_dir.upper()) else 'long'

        # offset_flag：优先取 CTP 原生字段
        offset_flag = ''
        allocation_source = 'ctp_offset'
        raw_offset = getattr(trade, 'offset', '') or ''
        if hasattr(raw_offset, 'value'):
            raw_offset = raw_offset.value
        if raw_offset in ('开', 'OPEN', 'open', 'Open'):
            offset_flag = 'open'
        elif raw_offset in ('平今', 'CLOSETODAY', 'close_today', 'CloseToday'):
            offset_flag = 'close_today'
        elif raw_offset in ('平昨', 'CLOSEYESTERDAY', 'close_yesterday', 'CloseYesterday'):
            offset_flag = 'close_yesterday'
        elif raw_offset in ('平', 'CLOSE', 'close', 'Close'):
            # 中金所等只报"平"，不区分今昨 → 先按昨结，无昨结时降级落到今开成本
            offset_flag = 'close_yesterday'
        else:
            # 降级推断
            allocation_source = 'fifo_fallback'
            is_open = getattr(trade, 'is_open', False)
            offset_flag = 'open' if is_open else 'close_yesterday'

        # 头寸方向：开仓与买卖同向；平仓反向（卖平 = 平掉多头）
        position_direction = trade_side if offset_flag == 'open' else (
            'short' if trade_side == 'long' else 'long')

        price = float(getattr(trade, 'price', 0) or 0)
        volume = int(getattr(trade, 'volume', 0) or 0)

        record = {
            'dedup_key': dedup_key,
            'ledger_key': [trading_day, account, exchange, symbol, position_direction],
            'trade_id': trade_id,
            'symbol': symbol,
            'direction': raw_dir,
            'trade_side': trade_side,
            'position_direction': position_direction,
            'open_close': '开' if offset_flag == 'open' else '平',
            'offset_flag': offset_flag,
            'allocation_source': allocation_source,
            'price': price,
            'volume': volume,
            'trade_time': dt_str,
            'account': account,
            'exchange': exchange,
            'trading_day': trading_day,
            'cost_price': 0.0,
            'cost_basis': 'n/a',
            'realized_pnl': 0.0,
        }
        ledger_key = tuple(record['ledger_key'])
        _TRADE_CACHE.setdefault(ledger_key, []).append(record)

        # 开仓 → 累加今日开仓加权成本（今开腿的 pnl 基准 / 平今成本）
        if offset_flag == 'open':
            _accum_open_cost(record)

        # P0-6/F2: 平仓时计算 realized PnL 并累加
        # 成本基准（盯市口径，与老系统一致）：
        #   平昨：昨结算价 → 今开加权 → 结算单开仓均价 → 成交价（告警，盈亏记 0）
        #   平今：今开加权 → 昨结算价 → 结算单开仓均价 → 成交价（告警）
        if offset_flag != 'open' and price > 0 and volume > 0:
            direction_sign = 1 if position_direction == 'long' else -1
            sym = symbol
            with _shared_lock:
                cost_dict = dict(_shared_state.get('settlement_dict', {}))
                settle_dict = dict(_shared_state.get('settlement_prices', {}))
                c = _shared_state.get('contracts', {}).get(sym, {})
            size = c.get('size', 1) or 1
            if not c:
                logger.warning(f"[_on_trade] {sym} 合约信息未就绪，size 暂按 1（已实现盈亏可能偏小）")
            prev_settle = float(settle_dict.get(sym) or 0.0)
            open_cost = float(_open_cost_map().get(f"{sym}_{position_direction}") or 0.0)
            avg_cost = float(cost_dict.get(f"{sym}_{position_direction}") or 0.0)
            if offset_flag == 'close_today':
                ladder = ((open_cost, 'today_open_cost'), (prev_settle, 'prev_settlement'),
                          (avg_cost, 'settlement_open_cost'))
            else:
                ladder = ((prev_settle, 'prev_settlement'), (open_cost, 'today_open_cost'),
                          (avg_cost, 'settlement_open_cost'))
            cost_price, cost_basis = 0.0, ''
            for _v, _b in ladder:
                if _v > 0:
                    cost_price, cost_basis = _v, _b
                    break
            if cost_price <= 0:
                cost_price, cost_basis = price, 'unknown_use_trade_price'
                logger.warning(f"[_on_trade] {sym} 无任何成本基准（归因 {allocation_source}），已实现按 0 计")
            pnl_realized = direction_sign * (price - cost_price) * volume * size
            record['cost_price'] = round(cost_price, 6)
            record['cost_basis'] = cost_basis
            record['realized_pnl'] = round(pnl_realized, 2)
            _REALIZED_PNL_CACHE[sym] = _REALIZED_PNL_CACHE.get(sym, 0.0) + pnl_realized
            logger.info(f"[_on_trade] {sym} {offset_flag} {volume}手@{price} "
                        f"成本{cost_price}({cost_basis}) → realized {pnl_realized:.2f}，"
                        f"累计 {_REALIZED_PNL_CACHE[sym]:.2f}")

        _save_trade_ledger()

    except Exception:
        logger.exception("[_on_trade] 处理成交回报异常")


# ── P0-2: EVENT_TRADE 注册（在 engine 连接成功后调用）──────────────────────────
def _register_trade_event(eng: VNPYEngine):
    """将 _on_trade 注册到 vnpy 事件引擎。"""
    try:
        from vnpy.trader.event import EVENT_TRADE
        # event_engine 不是 VNPYEngine 的属性，挂在 MainEngine 上
        me = getattr(eng, "main_engine", None)
        if me is None or not hasattr(me, "event_engine"):
            logger.warning("[_register_trade_event] main_engine/event_engine 未就绪，成交回报未注册")
            return
        me.event_engine.register(EVENT_TRADE, _on_trade)
        logger.info("[_register_trade_event] EVENT_TRADE 注册成功")
    except Exception as e:
        logger.warning(f"[_register_trade_event] EVENT_TRADE 注册失败: {e}")


def _worker_loop(settlement_dir: str):
    """
    CTP Worker 主循环（连接 + 重连阶梯 + 轮询 + 自动快照）：
    - 外层：连接/重连循环。连接失败按 fast(3s×10)→idle(30min) 阶梯重试。
    - 内层：connected 后每秒轮询持仓；探测到掉线（login_status=False）→ 丢弃旧引擎，break 回外层重连。
    - 收盘快照只在内层 _poll_once 成功后调 _close_snapshot_step()（内部还有 ctp_status==connected 守卫）。
      重连与快照解耦：本模块只负责让 engine 活着，快照只读 ctp_status 作数据边界。
    """

    # === 时间守卫：非交易时段休眠，不建任何会话 ===
    now = datetime.datetime.now()
    weekday = now.weekday()   # Mon=0, Sun=6
    cur_min = now.hour * 60 + now.minute
    is_trading = weekday < 5   # 周一到周五

    if is_trading and 4 * 60 <= cur_min < 8 * 60 + 20:   # 04:00–08:20
        wake = now.replace(hour=8, minute=20, second=0, microsecond=0)
        if wake <= now:   # 已是20:00以后，08:20是"今天"已过
            wake += datetime.timedelta(days=1)
        nap = (wake - now).total_seconds()
        logger.info(f"[时间守卫] 非交易时段，休眠 {nap/60:.0f} 分钟，至 {wake.strftime('%H:%M')}")
        time.sleep(nap)

    global _engine

    global _settlement_manager
    cred = _ctp_credential.copy()
    if not cred.get("用户名") or not cred.get("密码"):
        _set_status("error", "缺少用户名或密码", alive=False)
        return

    # 结算单管理器全程只创建一次（初始化时已加载本地数据，后续 sync 只做增量补充）
    if _settlement_manager is None:
        _settlement_manager = SettlementManager()
        _settlement_manager.load_costs_from_meta()
    # F2: 重启后重放成交账本（当日已实现盈亏不因重启归零）
    _load_trade_ledger(_ctp_trading_day())
    
    # ── 结算单同步逻辑（修复两个 bug）──────────────────────────────────────
    # Bug 1: `_local_complete`一票否决 → 只看历史缓存，不看当天是否已入库 → 20:20 后再也不下载当天结算单
    # Bug 2: sync 只在连接成功时触发一次 → 20:00 后无路径再触发
    # Fix: 把静态标记改成函数 + 每日重检（每 10 分钟），同时保留连接时的立即触发
    # ---------------------------------------------------------------------
    
    _SYNC_GATE_MIN    = 20 * 60 + 20      # 20:20 起当日结算单可查（单位：分钟）
    _SYNC_RETRY_SEC   = 600               # 缺当天结算单则每 10 分钟重试一次
    
    _sync_state = {"running": False, "last_try": 0.0, "done_day": None}
    
    
    def _today_settlement_present() -> bool:
        """当天结算单是否已入库（口径：有 full_{date}.json 即已下载）。"""
        try:
            return _sm_cutoff() in _sm_scanned()
        except Exception:
            return False
    
    
    def _local_settlement_complete() -> bool:
        """
        本地是否齐全 —— 必须同时检查"当天"。
        否则 13:25 连上时历史缓存在 → 判定齐全 → 当晚 20:20 后再也不下载当天结算单。
        """
        if not _settlement_manager._cost_cache:
            return False
        if not _settlement_manager._meta.get("loaded", False):
            return False
        
        now = datetime.datetime.now()
        if now.hour * 60 + now.minute >= _SYNC_GATE_MIN:
            if not _today_settlement_present():
                return False
        
        return True
    
    
    def _do_settlement_sync():
        """后台线程目标：执行增量同步并更新 meta。已在调用处启动 daemon 线程。"""
        nonlocal _sync_state
        
        if _sync_state["running"]:
            return
        _sync_state["running"] = True
        try:
            if _local_settlement_complete():
                logger.info("[结算单] 本地数据已齐全（含当天），跳过 sync")
                return
            
            sync_result = _settlement_manager.sync()
            logger.info(f"[结算单] sync_result={sync_result}")
            
            # 若成功或当天已入库，则标记本日 done（避免重复尝试）
            if sync_result.get("today_updated") or _today_settlement_present():
                _sync_state["done_day"] = _sm_cutoff()
        except Exception as e:
            logger.error(f"[结算单] sync 异常：{e}")
            import traceback
            logger.debug(traceback.format_exc())
        finally:
            _sync_state["running"] = False
    
    
    def _maybe_settlement_sync():
        """
        每天 20:20 后自动触发一次当天结算单下载（独立于连接时刻）。
        - 只在 connected 且已过闸门时间时尝试
        - 每 _SYNC_RETRY_SEC 秒重试一次，直到当天文件入库
        - 已有 sync 在跑则不重复起线程
        """
        now = datetime.datetime.now()
        if now.hour * 60 + now.minute < _SYNC_GATE_MIN:
            return
        
        today_str = _sm_cutoff()
        
        # 今日已完成？返回
        if _sync_state["done_day"] == today_str and _today_settlement_present():
            return
        
        # 已在运行？
        if _sync_state["running"]:
            return
        
        # 距离上次尝试不足 _SYNC_RETRY_SEC？
        if time.time() - _sync_state["last_try"] < _SYNC_RETRY_SEC:
            return
        
        _sync_state["last_try"] = time.time()
        threading.Thread(target=_do_settlement_sync, daemon=True).start()
        logger.info("[结算单] 启动每日重试线程（20:20 后）")
    


    attempts = 0
    disconnect_retry_count = 0   # 记录连续掉线次数（用于自动重连上限）
    while not _ctp_stop_event.is_set():
        # 时间守卫：交易日前夜 04:00–08:20 不连接
        now = datetime.datetime.now()
        weekday = now.weekday()
        cur_min = now.hour * 60 + now.minute
        if weekday < 5 and 4 * 60 <= cur_min < 8 * 60 + 20:
            wake = now.replace(hour=8, minute=20, second=0, microsecond=0)
            if wake <= now:
                wake += datetime.timedelta(days=1)
            nap = (wake - now).total_seconds()
            logger.info(f"[时间守卫] 非交易时段，休眠 {nap/60:.0f} 分钟，至 {wake.strftime('%H:%M')}")
            time.sleep(nap)
            continue   # 重新循环检查连接状态

        _set_status("connecting", None, alive=True)
        eng, err, retryable = _connect_engine(cred)
        if eng is not None:
            # 连接成功
            _engine = eng
            attempts = 0
            disconnect_retry_count = 0
            _set_status("connected", "")

            # P0-2: 注册 CTP 成交通知回调
            _register_trade_event(eng)

            # 后台线程：增量同步（补缺漏 + 当天下载）
            t = threading.Thread(target=_do_settlement_sync, daemon=True)
            t.start()

            # 连接成功后立即主动查询持仓（不等 2 秒定时器）
            try:
                eng.query_positions()
                logger.info("[_worker_loop] 连接成功，已触发持仓查询")
            except Exception as e:
                logger.warning(f"触发持仓查询异常: {e}")

            while not _ctp_stop_event.is_set():
                if not _td_logged_in(eng):
                    # 掉线 → 丢弃旧引擎（不 close！CtpTdApi.exit() 持 GIL 阻塞会冻死
                    # 整个解释器），回外层走重连阶梯建新引擎。对齐旧版 _do_retry_loop。
                    disconnect_retry_count += 1
                    if disconnect_retry_count <= _RETRY_FAST_ATTEMPTS:
                        _set_status("connecting",
                                    f"CTP 掉线，第{disconnect_retry_count}/{_RETRY_FAST_ATTEMPTS}次重连中")
                        _ctp_stop_event.wait(_RETRY_FAST_INTERVAL)
                        # 重建引擎继续尝试
                        eng, err, retryable = _connect_engine(cred)
                        if eng is not None:
                            _engine = eng
                            attempts = 0
                            disconnect_retry_count = 0
                            _set_status("connected", "")

                            # P0-2: 重连后重新注册 CTP 成交通知回调
                            _register_trade_event(eng)
                            continue
                    else:
                        # 10次重连均失败 → 通知前端弹窗，等用户手动处理
                        _set_status("error",
                                    f"CTP 连续掉线{disconnect_retry_count}次，请检查网络或重连",
                                    alive=False)
                        return
                # 每次轮询都取最新结算数据（sync 线程可能已更新 _settlement_manager）
                _settlement_manager.load_costs_from_meta()   # 确保缓存是最新的
                
                # ★ 每天 20:20 后自动触发一次当天结算单下载（独立于连接时刻）
                _maybe_settlement_sync()
                
                settlement_data    = _settlement_manager.get_all_costs()
                settlement_prices = _settlement_manager.get_all_prices()
                if len(settlement_data) == 0:
                    logger.warning(f"[结算单] settlement_data 为空！latest={_settlement_manager._meta.get('latest')}, cache={len(_settlement_manager._cost_cache)}")
                try:
                    _poll_once(eng, settlement_data, settlement_prices)
                except Exception:
                    logger.exception("[_poll_once] 异常")
                    import traceback
                    logger.info(f"[_poll_once] traceback: {traceback.format_exc()}")
                _close_snapshot_step()
                _ctp_stop_event.wait(1.0)
            continue  # 回到外层重连阶梯

        # ── 连接失败 ──
        if not retryable:
            # 验证失败（密码/授权码错等）：重试无意义，停 worker 等用户改参数
            _set_status("error", err, alive=False)
            return
        attempts += 1
        if attempts <= _RETRY_FAST_ATTEMPTS:
            _set_status("connecting",
                        f"{err}｜第{attempts}/{_RETRY_FAST_ATTEMPTS}次重试，{_RETRY_FAST_INTERVAL}s后")
            _ctp_stop_event.wait(_RETRY_FAST_INTERVAL)
        else:
            # fast 阶段耗尽：转 idle 长间隔后台重试，但前端显示 error 终态
            _set_status("error",
                        f"{err}｜已重试{_RETRY_FAST_ATTEMPTS}次仍无法连接，后台每{_RETRY_IDLE_INTERVAL // 60}分钟继续",
                        alive=True)
            _ctp_stop_event.wait(_RETRY_IDLE_INTERVAL)

    with _shared_lock:
        _shared_state["ctp_status"] = "disconnected"
        _shared_state["worker_alive"] = False
    # 从 worker 线程内关引擎（避免 api 线程 close 触发 CTP .pyd 崩溃）
    # 只在「还连着」时优雅关；已掉线的引擎 close() 会持 GIL 永久阻塞（同 L224 的雷）
    try:
        if _engine and _td_logged_in(_engine):
            _engine.close()
    except Exception:
        pass
    _engine = None


def _poll_once(engine: VNPYEngine, settlement_data: dict, settlement_prices: dict):
    """单次轮询：读取持仓 → 订阅行情 → 计算 Greeks → 写共享状态"""
    global _engine, _RECEIVED_TODAY_DATE, _RECEIVED_TODAY, _SEEN_NONEMPTY_POS
    _engine = engine

    # ── P0-8 补充：交易日切换 → 重置 received_today ────────────────────────────
    current_session, current_day = _current_session()
    # F2: 账本分区以 CTP TradingDay 为权威（本机 clock 只作兜底，不用于账务）
    _ctp_day = _ctp_trading_day()
    if _ctp_day:
        _rollover_trading_day(_ctp_day)
    if current_day and current_day != _RECEIVED_TODAY_DATE:
        if _RECEIVED_TODAY:
            logger.info(f"[received_today] 交易日切换 {_RECEIVED_TODAY_DATE} → {current_day}，重置行情推送标记")
        _RECEIVED_TODAY_DATE = current_day
        _RECEIVED_TODAY.clear()

    # ── 读取持仓 ──────────────────────────────────────────────────────────────
    raw_positions = engine.query_positions()
    if raw_positions:
        _SEEN_NONEMPTY_POS = True   # 真空仓佐证：本连接内曾见非空持仓，之后变空才算真平仓
    logger.info(f"[_poll_once] query_positions() 返回 {len(raw_positions)} 条持仓")

    # ── 订阅行情 + 收集标的 ───────────────────────────────────────────────────
    all_option_ticks = []
    futures_positions = []
    underlying_set = set()
    by_underlying = defaultdict(list)
    by_symbol = {}

    for pos in raw_positions:
        vt = pos.vt_symbol
        avail = pos.volume - pos.frozen
        if vt not in by_symbol:
            pos = copy.copy(pos)
            pos.available = avail
            by_symbol[vt] = pos
        else:
            existing = by_symbol[vt]
            if pos.direction == Direction.LONG:
                existing.volume += pos.volume
                existing.available += avail
            else:
                existing.volume -= pos.volume
                existing.available -= avail

    positions = list(by_symbol.values())
    logger.info(f"[_poll_once] 分组后 positions={len(positions)}")

    for pos in positions:
        contract = engine.get_contract(pos.vt_symbol)
        if not contract:
            # 合约缓存未加载：先订阅该合约触发行情回填，本轮跳过 Greeks 计算
            engine.query_tick(pos.vt_symbol, timeout=None)
            continue
        if contract.product != Product.OPTION:
            futures_positions.append((pos, contract))
            continue

        und = contract.option_underlying or ""
        expiry = str(contract.option_expiry)[:10] if contract.option_expiry else ""
        key = (und, expiry)
        by_underlying[key].append((pos, contract))

        engine.query_tick(pos.vt_symbol, timeout=None)

        und_exchange = contract.exchange.value
        if und_exchange == "CFFEX" and und.startswith("MO"):
            mapped_und = "IM" + und[2:]
        else:
            mapped_und = und
        full_und = f"{mapped_und}.{und_exchange}"
        underlying_set.add(full_und)

    for full_und in underlying_set:
        engine.query_tick(full_und, timeout=None)

    # ── 收集标的行情 ─────────────────────────────────────────────────────────
    underlying_prices = {}
    for (und, expiry), pos_list in by_underlying.items():
        if not und:
            continue
        und_exchange = pos_list[0][1].exchange.value
        if und_exchange == "CFFEX" and und.startswith("MO"):
            mapped_und = "IM" + und[2:]
        else:
            mapped_und = und
        full_und = f"{mapped_und}.{und_exchange}"
        tick = engine.query_tick(full_und, timeout=None)
        if tick and tick.last_price != 0:
            price = tick.last_price
        elif tick and tick.pre_close:
            price = tick.pre_close
        else:
            und_contract = engine.get_contract(full_und)
            price = getattr(und_contract, "pre_close", 0) or 0.0
        underlying_prices[full_und] = price

    # ── 计算 Greeks ───────────────────────────────────────────────────────────
    # 构造 symbols 列表：[(vt_symbol, contract, pos, direction_str)]
    option_symbols = []
    for (und, expiry), pos_list in by_underlying.items():
        for pos, contract in pos_list:
            direction_str = "long" if pos.direction == Direction.LONG else "short"
            # pricing.py 期望 contract/pos 为 dict，这里由 ContractData/PositionData 转构
            cdict = {
                "option_underlying": contract.option_underlying,
                "option_expiry":     contract.option_expiry,
                "option_strike":     contract.option_strike,
                "name":              getattr(contract, "name", pos.vt_symbol),
                "size":              contract.size,
                "pre_close":         getattr(contract, "pre_close", 0) or 0,
            }
            pdict = {"volume": pos.volume}
            option_symbols.append((pos.vt_symbol, cdict, pdict, direction_str))

    # 构造 option_ticks: {vt_symbol: {last_price, bid_price_1, ask_price_1, underlying_price, iv}}
    option_ticks = {}
    for (und, expiry), pos_list in by_underlying.items():
        # 标的价（含 MO→IM 映射）
        und_exchange = pos_list[0][1].exchange.value
        if und_exchange == "CFFEX" and und.startswith("MO"):
            mapped_und = "IM" + und[2:]
        else:
            mapped_und = und
        full_und = f"{mapped_und}.{und_exchange}"
        und_price = underlying_prices.get(full_und, 0)

        for pos, contract in pos_list:
            tick = engine.query_tick(pos.vt_symbol, timeout=None)
            if tick and tick.last_price != 0:
                # P0-8: 事件驱动开盘标记（收到 tick = 开盘）
                sym = pos.vt_symbol.split('.')[0]
                ts = getattr(tick, 'datetime', None)
                if sym not in _OPENED_CONTRACTS:
                    ts_str = str(ts)[11:19] if ts else datetime.datetime.now().strftime('%H:%M:%S')
                    _OPENED_CONTRACTS[sym] = {'first_tick': ts_str}
                # P0-8 补充：收到 tick → 转换为交易日 → 记录今日行情推送
                if ts is not None:
                    tick_session, tick_day = _current_session(ts)
                    if tick_day == _RECEIVED_TODAY_DATE:
                        _RECEIVED_TODAY[sym] = True
                        # 品种级标记：该期权所属品种（und）所有合约开始计算
                        if und:
                            _RECEIVED_TODAY[und] = True
                            # CFFEX MO/IM 互映射：MO 合约进来也标记 IM
                            if und.startswith("MO"):
                                _RECEIVED_TODAY["IM" + und[2:]] = True
                option_ticks[pos.vt_symbol] = {
                    "last_price":   tick.last_price,
                    "bid_price_1":  getattr(tick, "bid_price_1", 0) or 0,
                    "ask_price_1":  getattr(tick, "ask_price_1", 0) or 0,
                    "bid_volume_1": getattr(tick, "bid_volume_1", 0) or 0,
                    "ask_volume_1": getattr(tick, "ask_volume_1", 0) or 0,
                    "datetime":     getattr(tick, "datetime", None),
                }
            else:
                option_ticks[pos.vt_symbol] = {
                    "last_price":   0.0,
                    "bid_price_1":  0,
                    "ask_price_1":  0,
                    "bid_volume_1": 0,
                    "ask_volume_1": 0,
                    "datetime":     None,
                }
            option_ticks[pos.vt_symbol]["underlying_price"] = und_price
            option_ticks[pos.vt_symbol]["iv"] = None

    # FIX 2.2: 期货自带行情，query_tick 触发订阅并写入 option_ticks（last_price 与标的价同源）
    for pos, contract in futures_positions:
        ft = engine.query_tick(pos.vt_symbol, timeout=None)
        lp = 0.0
        ft_dt = None
        if ft:
            lp = ft.last_price or (getattr(ft, "pre_close", 0) or 0)
            ft_dt = getattr(ft, "datetime", None)
        sym = pos.vt_symbol.split('.')[0]
        # 品种级标记：futures 的品种 = 合约主符号（如 CU/AU/NI/IM 期货品种）
        # 提取品种前缀：2字母（CU/AU/NI/IM 等）或 1字母（J/M/RU 等），数字前部分
        import re
        m = re.match(r'^([A-Z]{1,2})', sym)
        species = m.group(1) if m else sym
        if ft_dt is not None:
            tick_session, tick_day = _current_session(ft_dt)
            if tick_day == _RECEIVED_TODAY_DATE:
                _RECEIVED_TODAY[sym] = True
                _RECEIVED_TODAY[species] = True
        option_ticks[pos.vt_symbol] = {
            "last_price":       lp,
            "bid_price_1":      0,
            "ask_price_1":      0,
            "underlying_price": lp,
            "iv":               None,
            "datetime":         ft_dt,
        }

    # 调用 price_options_batch（纯函数，无 engine 依赖）
    option_greeks = price_options_batch(option_symbols, option_ticks, settlement_data)

    # 回填 IV / adjust_price 到 option_ticks：positions_out 与 build_tree 都从这里取；
    # 否则 iv=None 传入 black76 会 TypeError，并导致整个 tree 构造抛异常 → 看板全空
    for vt_sym, g in option_greeks.items():
        if vt_sym in option_ticks:
            option_ticks[vt_sym]["iv"] = g.get("iv")
            option_ticks[vt_sym]["adjust_price"] = g.get("adjust_price")

    # 读取账户
    account_data = {}
    try:
        acct = engine.query_account(engine.accountid)
        if acct:
            account_data = {
                "balance": getattr(acct, "balance", 0) or 0,
                "available": getattr(acct, "available", 0) or 0,
                "commission": getattr(acct, "commission", 0) or 0,
                "margin": getattr(acct, "margin", 0) or 0,
                "position_pnl": getattr(acct, "position_pnl", 0) or 0,
                "close_pnl": getattr(acct, "close_pnl", 0) or 0,
            }
    except Exception:
        pass

    # ── 构造 positions 列表（Greeks 平铺，非嵌套）──────────────────────────────
    positions_out = []
    for pos in positions:
        contract = engine.get_contract(pos.vt_symbol)
        if not contract:
            continue
        is_option = contract.product == Product.OPTION
        symbol = pos.vt_symbol

        # 方向/数量
        if pos.direction == Direction.LONG:
            direction = "long"
            pos_volume = pos.volume
            available = pos.available
        else:
            direction = "short"
            pos_volume = -pos.volume
            available = -pos.available if pos.available else 0

        # 开仓价：优先结算单
        settle_key = f"{symbol.split('.')[0]}_{('多' if direction == 'long' else '空')}"
        open_price = pos.price or 0
        if symbol in option_greeks:
            open_price = option_greeks[symbol].get("open_price", open_price)
        elif settle_key in settlement_data:
            open_price = settlement_data[settle_key]

        # 从 option_ticks 取期权 tick（避免二次查询）
        tick_data = option_ticks.get(symbol, {})
        last_price = tick_data.get("last_price") or 0
        underlying_price = tick_data.get("underlying_price", 0)
        iv = tick_data.get("iv")
        adj_price = tick_data.get("adjust_price")

        # Greeks（期权有，期货为 0）
        if is_option and symbol in option_greeks:
            g = option_greeks[symbol]
            greeks_flat = {k: g.get(k, 0) for k in (
                "delta", "gamma", "vega", "theta", "pos_delta", "pos_gamma",
                "pos_vega", "pos_theta", "deltacash", "gammacash", "vegacash",
                "thetacash", "is_itm", "days_to_expiry"
            )}
        else:
            greeks_flat = {
                # 可汇总列=头寸级；期货 delta=方向×手数
                "delta": pos_volume, "gamma": 0.0, "vega": 0.0, "theta": 0.0,
                "pos_delta": pos_volume, "pos_gamma": 0.0, "pos_vega": 0.0, "pos_theta": 0.0,
                "deltacash": round(pos_volume * last_price * contract.size),
                "gammacash": 0, "vegacash": 0, "thetacash": 0,
                "is_itm": False, "days_to_expiry": None,
            }

        positions_out.append({
            "symbol":        symbol,
            "direction":     direction,
            "volume":        pos_volume,
            "yd_volume":    getattr(pos, 'yd_volume', 0) or 0,
            "available":     available,
            "price":         open_price,
            "last_price":    last_price,
            "underlying_price": underlying_price,
            "iv":            iv,
            "adjust_price":  adj_price or last_price,
            "underlying":    contract.option_underlying if is_option else symbol,
            "expiry":        str(contract.option_expiry)[:10] if is_option else "",
            "strike":        contract.option_strike if is_option else 0,
            "option_type":   contract.option_type.value if is_option else "",
            "size":          contract.size,
            **greeks_flat,
        })
    
    # ── 记录持仓品种集合（用于 σ_ref 弹窗，规范化到品种码）──────────────────
    held_products = sorted({normalize_underlying(p["symbol"].split('.')[0]) for p in positions_out})

    # ── 构造 ticks（格式对齐 build_tree 期望）────────────────────────────────
    # ticks 格式: {symbol: {last_price, underlying_price, iv}}
    ticks = {}
    for full_und, price in underlying_prices.items():
        sym = full_und.split('.')[0]
        ticks[sym] = {"last_price": price, "underlying_price": price, "iv": None}
    for vt_sym, td in option_ticks.items():
        sym = vt_sym.split('.')[0]
        ticks[sym] = {
            "last_price":       td.get("last_price") or 0,
            "underlying_price": td.get("underlying_price") or 0,
            "iv":               td.get("iv"),
            "adjust_price":     td.get("adjust_price"),
        }

    logger.info(f"[_poll_once] futures_positions={len(futures_positions)}, by_underlying={len(by_underlying)}, option_contracts={sum(len(v) for v in by_underlying.values())}")

    # ── 构造 contracts（格式对齐 build_tree 期望，统一 days_to_expiry）──────────
    contracts = {}
    for pos, contract in futures_positions:
        sym = pos.vt_symbol.split('.')[0]
        contracts[sym] = {
            "size":         contract.size,
            "product_type": "FUTURES",
            "option_type":  "",
            "strike":       0,
            "days_to_expiry": None,
        }
    for (und, expiry), pos_list in by_underlying.items():
        for pos, contract in pos_list:
            sym = pos.vt_symbol.split('.')[0]
            expiry_str = str(contract.option_expiry)[:10] if contract.option_expiry else ""
            # FIX: option_type 可能是字符串/枚举/None，统一转成大写'C'/'P'
            ot_raw = contract.option_type or ""
            if hasattr(ot_raw, 'value'):
                ot_str = ot_raw.value.upper()
            elif isinstance(ot_raw, str):
                ot_str = ot_raw.upper().strip()
            else:
                ot_str = ""
            contracts[sym] = {
                "size":           contract.size or 1,
                "product_type":   "OPTION",
                "option_type":    ot_str,  # 'C' 或 'P'
                "strike":         contract.option_strike or 0,
                "days_to_expiry": days_to_expiry(expiry_str),
            }

    settlement_cost_dict  = settlement_data
    settlement_prices_dict = settlement_prices

    # ── 加载昨快照（adjust_price）──────────────────────────────────────────────
    # calc_pnl 期望格式: {f"{sym}_{direction}": {"adjust_price": float}}
    # yesterday_snapshot key 已在 _load_yesterday_snapshot 中构建为英文
    yesterday_snapshot = _load_yesterday_snapshot()

    # ── 写共享状态 ────────────────────────────────────────────────────────────
    # build_tree 是纯函数，需要 ticks + contracts + settlement_dict
    try:
        tree = build_tree(positions_out, ticks, contracts,
                          settlement_cost_dict, settlement_prices_dict,
                          yesterday_snapshot, _RECEIVED_TODAY, _open_cost_map())
    except Exception:
        import traceback
        tree = {"summary": {}, "tree": []}
        with open(_os.path.join(_parent_dir, "_poll_error.log"), "a") as f:
            f.write(f"=== {datetime.datetime.now()} ===\n")
            f.write(f"positions_out={len(positions_out)}\n")
            f.write(f"contracts={len(contracts)}\n")
            f.write(f"ticks={list(ticks.keys())[:10]}\n")
            f.write(traceback.format_exc())
            f.write("\n")
    logger.info(f"[_poll_once] positions_out={len(positions_out)}, contracts={len(contracts)}, tree_nodes={len(tree.get('tree',[]))}, yesterday_snapshot_keys={len(yesterday_snapshot)}, settlement_prices_keys={len(settlement_prices_dict)}")

    # F1: 基准来源可见性——昨收盘快照以外的降级腿计数发生变化时告警一次（不每轮刷屏）
    global _BASIS_WARN_SIGNATURE
    _counts = (tree.get("summary") or {}).get("pnl_basis_counts") or {}
    _sig = json.dumps(_counts, sort_keys=True)
    if _sig != _BASIS_WARN_SIGNATURE:
        _BASIS_WARN_SIGNATURE = _sig
        _bad = {k: v for k, v in _counts.items() if k != "prev_close_snapshot"}
        if _bad:
            logger.warning(f"[pnl基准] 非昨收盘快照基准腿 计数={_bad}（快照基准 "
                           f"{_counts.get('prev_close_snapshot', 0)} 腿）—— 见基线 §3.7 降级链")

    # P0-6: 汇总已实现 PnL（_realized_pnl_cache）追加到 summary
    # 全平合约从 tree 消失，但其 realized PnL 必须进入当日和历史汇总
    total_realized = sum(_REALIZED_PNL_CACHE.values())
    if total_realized != 0:
        if tree.get("summary"):
            # _make_summary 的键名是 total_*，注入必须对齐，否则写进无人消费的野键
            tree["summary"]["total_pnl_today"] = round(tree["summary"].get("total_pnl_today", 0) + total_realized, 2)
            tree["summary"]["total_pnl_history"] = round(tree["summary"].get("total_pnl_history", 0) + total_realized, 2)
        logger.debug(f"[_poll_once] realized_pnl accumulated: {total_realized:.2f}")

    with _shared_lock:
        _shared_state["positions"] = positions_out
        _shared_state["underlying_prices"] = underlying_prices
        _shared_state["tree"] = tree
        _shared_state["contracts"] = contracts
        _shared_state["settlement_dict"] = settlement_cost_dict
        _shared_state["settlement_prices"] = settlement_prices_dict
        _shared_state["account"] = account_data
        _shared_state["last_update"] = datetime.datetime.now()
        # 监控预警：持仓品种集合 + σ_ref 缺失 + Margin 状态
        if held_products:
            _shared_state["_held_products"] = held_products
            from dashboard_v2.alert_config import sigma_ref_missing
            pending = sigma_ref_missing(held_products)
            if pending:
                logger.info(f"[alert] σ_ref 待填品种（本月免打扰后）:{pending}")
            else:
                logger.debug("[alert] σ_ref 全量已填")
        # Margin 风险度分档（95%/110% 两级）
        margin_ratio = None
        margin_headroom = None
        strict_risk_ratio = None
        level = "unknown"
        if account_data and tree.get('summary'):
            m = _get_thresh("margin_ratio_warn")
            d = _get_thresh("margin_ratio_danger")
            bal = account_data.get("balance", 0) or 0
            mg = account_data.get("margin", 0) or 0
            obl_prem = tree.get('summary', {}).get('obligation_premium', 0) or 0
            if bal > 0:
                ratio_pct = mg / bal * 100.0
                strict_risk_ratio = round((mg + obl_prem) / bal * 100.0, 2)
                # 滞回防抖：进入用原阈值，退出需低于阈值-缓冲（防 85% 边缘横跳刷记录）
                _MARG_HYST = 0.5   # 百分点，可调
                prev_lv = _shared_state.get("_margin_prev_level")
                if ratio_pct >= d:
                    level = "danger"
                elif ratio_pct >= m:
                    level = "warn"
                elif prev_lv == "danger" and ratio_pct >= d - _MARG_HYST:
                    level = "danger"   # danger 缓冲带内保持，不降级
                elif prev_lv == "warn" and ratio_pct >= m - _MARG_HYST:
                    level = "warn"     # warn 缓冲带内保持，不消除
                else:
                    level = "ok"
                _shared_state["_margin_prev_level"] = level
                margin_ratio = round(ratio_pct, 2)
                margin_headroom = round(bal * d / 100.0 - mg, 2)
        _shared_state["margin_status"] = {
            "ratio_pct": margin_ratio,
            "headroom": margin_headroom,
            "strict_risk_ratio": strict_risk_ratio,
            "level": level,
        }

    # ── 监控预警：四源触发检测（F 速率 / IV 速率 / Burn / Margin）──────────────────
    try:
        now_ts = time.time()
        f_rate = _compute_f_rate(underlying_prices, now_ts)
        iv_rate = _compute_iv_rate(by_underlying, option_greeks, now_ts)
        burn = _compute_burn(positions_out, yesterday_snapshot)
        structure = _compute_structure(positions_out)
        # 合约代码 → 品种（前端所有变色/标记的统一键；MO→IM 等别名由 normalize_underlying 归一）
        cund = {}
        for p in positions_out:
            code = (p.get("symbol") or "").split(".")[0]
            if code:
                cund[code] = normalize_underlying(code)
        _shared_state["contract_und"] = cund
        popups = _evaluate_alerts(f_rate, iv_rate, burn, structure, cund,
                                  _shared_state.get("margin_status"), now_ts)
        if popups:
            # 累加不覆盖：popups 是瞬时事件，整体覆盖会让前端 3s 轮询扑空
            buf = _shared_state.setdefault("popups", [])
            buf.extend(popups)
            del buf[: -_ALERT_RING_MAX]
    except Exception:
        import traceback
        logger.warning(f"[alert] 触发检测异常: {traceback.format_exc()}")


# ── Flask Blueprint ──────────────────────────────────────────────────────────

api_bp = Blueprint("api", __name__, url_prefix="/api")


# ── 快照目录（项目内）─────────────────────────────────────────────────────────
_SNAPSHOT_DIR = _os.path.join(_parent_dir, "快照")
_SNAPSHOT_PREFIX = "data_snapshot_"
_SNAPSHOT_EXT = ".json"

# ── 多账户配置（明文，本地专用）────────────────────────────────────────────────
_ACCOUNTS_FILE = _os.path.join(_parent_dir, "ctp_accounts.json")
_ACCOUNT_KEYS = ["用户名", "密码", "经纪商代码", "交易服务器", "行情服务器", "产品名称", "授权编码"]


def _load_accounts() -> dict:
    """返回 {active: str, accounts: {name: {7键}}}，文件缺失返回空壳。"""
    try:
        with open(_ACCOUNTS_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
        if not isinstance(d, dict) or "accounts" not in d:
            return {"active": "", "accounts": {}}
        return d
    except Exception:
        return {"active": "", "accounts": {}}


def _save_accounts(data: dict):
    tmp = _ACCOUNTS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    _os.replace(tmp, _ACCOUNTS_FILE)


def _current_session(dt: datetime.datetime = None):
    """
    按实际交易时段推导 (session, business_date)。
    - A: 08:00–11:30  早盘（含午间间隙，全天窗口闭合，无成交数据变化无需存）
    - P: 12:00–15:00  午盘（CFFEX 15:00 收，商品 13:30 开，统一窗口即可）
    - N: 20:00–24:00 归次日业务日；00:00–02:30 归当日业务日（夜盘跨午夜归次日）
    窗口外（03:00–07:59 等无成交时段）返回 (None, None) → 不保存。
    N 排最前（名称 N0 字典序 < A/P）。
    """
    if dt is None:
        dt = datetime.datetime.now()
    h = dt.hour + dt.minute / 60.0

    # 窗口右端各放宽 6min：收盘（11:30/15:00/02:30）之后仍留一次落盘机会，以取到收盘截面
    # ponytail: 30min 周期不保证正好落在窗口尾 6min 内；若仍取不到收盘价，再加"窗口尾强制写"
    if 20.0 <= h < 24.0:
        return ("N", (dt + datetime.timedelta(days=1)).strftime("%Y%m%d"))
    if 0.0 <= h < 2.6:
        return ("N", dt.strftime("%Y%m%d"))
    if 8.0 <= h < 11.6:
        return ("A", dt.strftime("%Y%m%d"))
    if 12.0 <= h < 15.1:
        return ("P", dt.strftime("%Y%m%d"))
    return (None, None)


def _positions_hash(positions):
    """positions 列表的 sha256，用于判定是否需要落盘（与旧文件对比）"""
    canonical = json.dumps(positions, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _snapshot_path(name: str) -> str:
    return _os.path.join(_SNAPSHOT_DIR, name)


def _ensure_snapshot_dir():
    _os.makedirs(_SNAPSHOT_DIR, exist_ok=True)


def _business_date(dt: datetime.datetime) -> str:
    """业务日：20:00 后归次日，其余归当日（与 _current_session 的夜盘规则一致）。
    ponytail: 仍由本机 clock 推导，未取 CTP TradingDay；升级路径 = 从 engine 读 trading_day。"""
    if dt.hour >= 20:
        return (dt + datetime.timedelta(days=1)).strftime("%Y%m%d")
    return dt.strftime("%Y%m%d")


# ── 收盘快照（v1.4）：每业务日一份，基准 = 14:55–15:00 Mark 算术平均 ──────────

# 快照叶子 direction 的两种写法（中文/英文）统一映射到 calc_pnl 的查找键
_DIR_MAP = {"多": "long", "空": "short", "long": "long", "short": "short"}

# 昨收基准按业务日缓存：避免每轮 poll 重解析整份快照 JSON
_BASE_CACHE: dict = {"bd": "", "result": {}}


def _close_snapshot_name(bd: str) -> str:
    return f"{_CLOSE_PREFIX}{bd}{_SNAPSHOT_EXT}"


def _close_dates() -> dict:
    """{业务日: 文件名}，只认 close_snapshot_*；历史 data_snapshot_* 一律不读，留盘审计。"""
    _ensure_snapshot_dir()
    out = {}
    for name in _os.listdir(_SNAPSHOT_DIR):
        if not name.startswith(_CLOSE_PREFIX) or not name.endswith(_SNAPSHOT_EXT):
            continue
        d = name[len(_CLOSE_PREFIX):-len(_SNAPSHOT_EXT)]
        if len(d) == 8 and d.isdigit():
            out[d] = name
    return out


def _leaf_marks(tree_nodes) -> dict:
    """从 tree 提取 L3 叶子 {f"{sym}_{direction}": adjust_price}（Mark；期权与期货同字段）"""
    out = {}

    def walk(nodes):
        for node in nodes or []:
            children = node.get("children")
            if children:
                walk(children)
                continue
            sym = node.get("symbol")
            d = _DIR_MAP.get(node.get("direction", ""))
            if sym and d:
                out[f"{sym}_{d}"] = node.get("adjust_price")

    walk(tree_nodes)
    return out


def _prev_trading_day_file(bd: str):
    """T-1 业务日的收盘快照文件名；无则 None（不跨业务日回退，基准降级结算价）。

    基准优先级（基线 §3.7）：**收盘快照是基准，结算单是降级备用**。
    快照日期本身即「该交易日已收盘」的实据，不靠结算单背书 → 默认直接命中最近一份 < bd 的快照。
    结算单日历只保留一个守卫职能：确认「T-1 是交易日、当日却无快照」（如服务停机）
    → 返回 None 由 calc_pnl 降级结算价，绝不用更老的快照冒充昨收。
    """
    older = {d: n for d, n in _close_dates().items() if d < bd}
    if not older:
        return None                       # 一份快照都没有 → 降级结算价
    latest = max(older)
    try:
        cal = max((d for d in _valid_dates() if d < bd), default="")
    except Exception as e:
        logger.warning(f"[close_snapshot] 读结算单交易日历失败: {e}")
        return older[latest]              # 日历不可用 → 快照照用（快照即实据）
    if cal and cal > latest:
        logger.warning(f"[close_snapshot] 结算单日历 T-1=%s 是交易日但无当日快照（服务停机？）"
                       "→ 基准降级结算价，不用更老的 %s" % (cal, latest))
        return None
    return older[latest]


def _load_yesterday_snapshot() -> dict:
    """
    加载上一交易日收盘快照的 leaves，返回 {f"{sym}_{direction}": {"adjust_price": p}}。
    只认 T-1 的 close_snapshot_*（14:55–15:00 Mark 算术平均，price_basis=close_avg）：
    不跨业务日回退、不读历史 data_snapshot_*（盘中价/末价冒充昨收即为错误基准）。
    无 T-1 快照 → 返回 {} → calc_pnl 降级昨结算价 → 再 None。
    """
    bd = _business_date(datetime.datetime.now())
    if _BASE_CACHE["bd"] == bd:
        return _BASE_CACHE["result"]

    result = {}
    fname = _prev_trading_day_file(bd)
    if not fname:
        logger.warning(f"[_load_yesterday_snapshot] 无 T-1（{bd}）收盘快照 → 基准降级结算价")
    else:
        try:
            with open(_snapshot_path(fname), "r", encoding="utf-8") as f:
                data = json.load(f)
            for key, leaf in (data.get("leaves") or {}).items():
                if not isinstance(leaf, dict):
                    continue
                if leaf.get("price_basis") != "close_avg":
                    logger.warning(f"[_load_yesterday_snapshot] {key} price_basis="
                                   f"{leaf.get('price_basis')!r} 非 close_avg，拒作基准")
                    continue
                p = leaf.get("adjust_price")
                if isinstance(p, (int, float)) and p == p and p > 0:
                    result[key] = {"adjust_price": float(p)}
            logger.info(f"[_load_yesterday_snapshot] 基准快照={fname}，提取 {len(result)} 条收盘 Mark")
        except Exception as e:
            logger.warning(f"[_load_yesterday_snapshot] 读 {fname} 失败: {e}")

    _BASE_CACHE["bd"], _BASE_CACHE["result"] = bd, result
    return result


def _close_snapshot_step():
    """
    收盘快照调度（v1.4）：14:55–15:00 每轮 poll 记一次 Mark；15:00 后第一轮落盘。
    守护1：仅 ctp_status == connected。
    守护2：采样窗内只采样；15:00–15:10 内且该业务日未写才尝试落盘（失败可重试）。
    守护3：持仓为空且本连接内未见过非空持仓 → 判数据通信未就绪，不落盘。
    守护4：采样窗错过（缓冲为空）→ 放弃当日并 WARNING，T+1 降级结算价。
    """
    global _SAMPLE_BD
    snap = _snapshot()
    if snap["ctp_status"] != "connected":
        return

    now = datetime.datetime.now()
    t = now.time()
    bd = _business_date(now)
    if bd != _SAMPLE_BD:
        _MARK_SAMPLES.clear()
        _SAMPLE_BD = bd

    if _CLOSE_SAMPLE_START <= t < _CLOSE_SAMPLE_END:
        for key, mark in _leaf_marks(snap["tree"]).items():
            if mark is not None:
                _MARK_SAMPLES.setdefault(key, []).append(mark)
        return

    if _CLOSE_SAMPLE_END <= t <= _CLOSE_RETRY_END and bd not in _CLOSE_SAVED:
        _save_close_snapshot(snap, bd, now)


def _save_close_snapshot(snap, bd: str, now: datetime.datetime):
    """写 close_snapshot_{bd}.json：leaves.adjust_price = 窗口内 Mark 算术平均（不去重、不加权）。"""
    positions = snap["positions"]
    if not positions and not _SEEN_NONEMPTY_POS:
        logger.warning("[close_snapshot] 持仓为空且本连接内未见过非空持仓，判数据通信未就绪 → 不落盘")
        return
    if not _MARK_SAMPLES:
        logger.warning("[close_snapshot] 采样缓冲为空（14:55–15:00 服务未运行？）"
                       "→ 本日无收盘快照，T+1 降级结算价")
        _CLOSE_SAVED.add(bd)
        return

    last = _leaf_marks(snap["tree"])
    leaves = {}
    for key, samples in _MARK_SAMPLES.items():
        if not samples:
            continue
        leaf = {"adjust_price": round(sum(samples) / len(samples), 4),
                "price_basis": "close_avg",
                "samples": len(samples)}
        if last.get(key) is not None:
            leaf["last_price"] = last[key]
        leaves[key] = leaf

    payload = {
        "version": 4,
        "trading_date": bd,
        "saved_at": now.isoformat(),
        "window": {"start": _CLOSE_SAMPLE_START.strftime("%H:%M:%S"),
                   "end": _CLOSE_SAMPLE_END.strftime("%H:%M:%S")},
        "ctp_status": snap["ctp_status"],
        "snapshot_kind": "empty" if not positions else "live",
        "data_hash": _positions_hash(positions),
        "leaves": leaves,
        "raw": {
            "positions": positions,
            "underlying_prices": snap["underlying_prices"],
        },
        "computed": {
            "summary": snap["summary"],
            "tree": snap["tree"],
        },
        "account": snap["account"],
    }
    _ensure_snapshot_dir()
    filepath = _snapshot_path(_close_snapshot_name(bd))
    try:
        tmp = filepath + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_clean_nan(payload), f, ensure_ascii=False, indent=2)
        _os.replace(tmp, filepath)   # 原子替换，避免读到半截文件
        _CLOSE_SAVED.add(bd)
        _MARK_SAMPLES.clear()
        logger.info(f"[close_snapshot] 已落盘 {filepath}：{len(leaves)} 条收盘 Mark，"
                    f"持仓 {len(positions)} 条")
    except Exception as e:
        logger.warning(f"[close_snapshot] 写盘失败（15:00–15:10 内重试）: {e}")


@api_bp.route("/snapshot/save", methods=["POST"])
def api_snapshot_save():
    """手动保存当前持仓快照到固定文件（覆盖写，仅供调试/取数，不作基准）。
    返回 {"message":"快照已保存","status":"ok"}。"""
    try:
        snap = _snapshot()
        now = datetime.datetime.now()
        payload = {
            "version": 4,
            "saved_at": now.isoformat(),
            "trading_date": _business_date(now),
            "ctp_status": snap["ctp_status"],
            "leaves": _leaf_marks(snap["tree"]),
            "raw": {
                "positions": snap["positions"],
                "underlying_prices": snap["underlying_prices"],
            },
            "computed": {
                "summary": snap["summary"],
                "tree": snap["tree"],
            },
            "account": snap["account"],
        }
        _ensure_snapshot_dir()
        filepath = _os.path.join(_SNAPSHOT_DIR, "data_snapshot_current.json")
        tmp = filepath + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_clean_nan(payload), f, ensure_ascii=False, indent=2)
        _os.replace(tmp, filepath)
        return jsonify({"status": "ok", "message": "快照已保存"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@api_bp.route("/snapshots", methods=["GET"])
def api_snapshots_list():
    """列出快照目录内所有快照（收盘快照 + 历史时段快照），
    返回 [{name, kind, trading_date, session, saved_at, position_count, leaves, price_basis}]"""
    _ensure_snapshot_dir()
    files = sorted(
        f for f in _os.listdir(_SNAPSHOT_DIR)
        if f.endswith(_SNAPSHOT_EXT) and (f.startswith(_CLOSE_PREFIX) or f.startswith(_SNAPSHOT_PREFIX))
    )
    result = []
    for fname in files:
        path = _os.path.join(_SNAPSHOT_DIR, fname)
        try:
            with open(path, "r", encoding="utf-8") as f:
                d = json.load(f)
            leaves = d.get("leaves") or {}
            bases = {v.get("price_basis") for v in leaves.values() if isinstance(v, dict)}
            result.append({
                "name": fname,
                "kind": "close" if fname.startswith(_CLOSE_PREFIX) else "legacy",
                "trading_date": d.get("trading_date", ""),
                "session": d.get("session", ""),
                "saved_at": d.get("saved_at", ""),
                "position_count": len(d.get("raw", {}).get("positions", [])),
                "ctp_status": d.get("ctp_status", ""),
                "leaves": len(leaves),
                "price_basis": ",".join(sorted(b for b in bases if b)),
            })
        except Exception:
            pass
    return jsonify(result)


@api_bp.route("/snapshot/load", methods=["POST"])
def api_snapshot_load():
    """
    加载快照（静态）。Body: {"name": "<filename>"}
    返回保存时已算好的 computed（tree/summary）+ positions，无后端重算。
    """
    data = request.get_json() or {}
    name = data.get("name", "").strip()
    if not name or "/" in name or "\\" in name or not name.endswith(_SNAPSHOT_EXT):
        return jsonify({"status": "error", "message": "name 不合法"}), 400

    filepath = _snapshot_path(name)
    if not _os.path.isfile(filepath):
        return jsonify({"status": "error", "message": f"快照文件不存在: {name}"}), 404

    try:
        with open(filepath, "r", encoding="utf-8") as f:
            raw_file = json.load(f)
    except Exception as e:
        return jsonify({"status": "error", "message": f"读文件失败: {e}"}), 500

    raw = raw_file.get("raw", {})
    computed = raw_file.get("computed", {})
    return jsonify({
        "status": "ok",
        "name": name,
        "session": raw_file.get("session", ""),
        "trading_date": raw_file.get("trading_date", ""),
        "saved_at": raw_file.get("saved_at", ""),
        "ctp_status": raw_file.get("ctp_status", ""),
        "summary": computed.get("summary", {}),
        "tree": computed.get("tree", []),
        "positions": raw.get("positions", []),
        "underlying_prices": raw.get("underlying_prices", {}),
        "account": raw_file.get("account", {}),
    })


# ══════════════════════════════════════════════════════════════════════════
# 监控预警引擎（四源：F 速率 / ATM IV 速率 / Premium Burn / Margin）
#   设计见 监控预警_后端实现设计_v0.2.md
#   原则：只复用 _poll_once 已拿到的数据，不新增定时任务，不落盘
# ══════════════════════════════════════════════════════════════════════════

_ALERT_WARMUP = True
_ALERT_COOLDOWN = {"warn": 600, "danger": 300}   # 冷却（秒）：黄10min 红5min

def _any_symbol_trading(now_ts=None):
    """全品种并集：任一持仓品种在交易时段即 True。无持仓/查询异常 → True 放行。"""
    try:
        from dashboard_v2.alert_config import in_trading_session
        now = now_ts or time.time()
        prods = _shared_state.get("_held_products") or set()
        if not prods:
            return True
        return any(in_trading_session(p, now) for p in prods)
    except Exception:
        return True

def _alert_window_ok(source, und, now_ts=None):
    """触发/消除弹窗的窗口门：非交易时段只拦弹窗，记录照写。"""
    try:
        from dashboard_v2.alert_config import in_trading_session
        if source == 'margin':
            return _any_symbol_trading(now_ts)   # margin 是账户级 → 全品种并集
        now = now_ts or time.time()
        return in_trading_session(und, now)
    except Exception:
        return True

_ALERT_DAILY_MAX = 3                             # 同一告警当日弹窗上限
_ALERT_RING_MAX = 100                            # 历史 ring buffer 上限
_IV_WINDOW_SEC = 300                             # IV 滚动窗口 5min
_IV_GAP_SEC = 4 * 3600                           # IV 样本间隔 > 4h → 断档清空
_F_RATE_ANNUAL_DAYS = 250                        # 口径：1年 = 250 交易日
_F_HOURS_FALLBACK = 4.0                          # trading_hours.csv 查不到时的降级时长(h)


def _level_of(value, warn, danger):
    """值与阈值比较 → 'danger' | 'warn' | None"""
    if value is None:
        return None
    if danger is not None and value >= danger:
        return "danger"
    if warn is not None and value >= warn:
        return "warn"
    return None


def _sigma_of(und):
    """品种 σ_ref（正常行情下的 IV 上限），缺失/非法 → 0.25。"""
    try:
        from dashboard_v2.alert_config import get_sigma
        sigma, _ = get_sigma(und)
    except Exception:
        sigma = None
    return sigma if (sigma and sigma > 0) else 0.25


def _hours_of(und):
    """
    品种日交易总时长（小时），查 config/trading_hours.csv。
    查不到 → 降级 4h，并「跳出提醒」：logger 警告一次 + hours_missing 随看板下发。
    """
    try:
        from dashboard_v2.alert_config import get_trading_hours
        row = get_trading_hours(und)
    except Exception:
        row = None
    if row and row.get("hours", 0) > 0:
        return row["hours"]
    missing = _shared_state.setdefault("hours_missing", set())
    if und not in missing:
        missing.add(und)
        logger.warning(
            f"[交易时段] 品种 {und} 不在 config/trading_hours.csv，"
            f"本次降级按 {_F_HOURS_FALLBACK}h 计算（请补表）"
        )
    return _F_HOURS_FALLBACK


def _conv_delta(symbol, F, K, days):
    """
    「换算Δ」= 固定 σ_ref 下、按该合约**自身剩余到期**算出的 Black-76 Δ（带符号）。
    与市场 Δ 的区别：市场 Δ 随 IV 漂移，换算Δ 只随 F/K/T 变，口径稳定，
    用于「行权价离标的有多远」的判定（平值腿选择、结构告警）。
    看板 Δ 列仍显示市场 Δ（列名就叫「Δ」）。
    缺 F/K/到期 → None。
    """
    if not F or F <= 0 or not K or K <= 0 or not days or days <= 0:
        return None
    und = normalize_underlying((symbol or "").split(".")[0])
    try:
        g = black76(_sigma_of(und), F, K, days / 365.0, cp=cp_from_symbol(symbol))
        d = g.get("delta")
    except Exception:
        return None
    return d if (d is not None and d == d) else None


def _compute_f_rate(underlying_prices, now_ts):
    """
    合约级 F 速率 = ln(5min窗口内该合约最高/最低) / (σ_ref × √(5min/年))。
    按月份合约分别计算（RU2611/RU2612 各自独立窗口，跨月价差不互相污染）；
    σ_ref / 交易时长仍是品种级配置。
    返回 {sym: (rate, 窗口最低价, 窗口最高价)}，sym 如 RU2611。
    """
    import math
    samples = _shared_state.setdefault("_f_samples", {})
    out = {}
    for full_und, price in (underlying_prices or {}).items():
        sym = full_und.split(".")[0].upper()
        und = normalize_underlying(sym)
        if not price or price <= 0:
            continue
        dq = samples.get(sym)
        if dq is None:
            dq = samples[sym] = deque()
        if dq and (now_ts - dq[-1][0]) > _IV_GAP_SEC:
            dq.clear()
        dq.append((now_ts, price))
        while dq and (now_ts - dq[0][0]) > _IV_WINDOW_SEC:
            dq.popleft()
        if len(dq) < 2:
            continue
        lo = min(v for _, v in dq)
        hi = max(v for _, v in dq)
        if lo <= 0 or hi <= lo:
            continue
        scale = _sigma_of(und) * math.sqrt(
            5.0 / (_F_RATE_ANNUAL_DAYS * 60.0 * _hours_of(und))
        )
        if scale <= 0:
            continue
        # (速率, 窗口最低, 窗口最高) —— 两端值与告警值严格一致
        out[sym] = (math.log(hi / lo) / scale, lo, hi)
    return out


def _atm_iv_of_group(pos_list, option_greeks):
    """
    取一组（同 underlying+expiry）的 ATM IV：
    最接近 |Δ|=0.5 且非 ITM 的合约，多候选取中位数。
    选腿用「换算Δ」（固定 σ_ref，不随市场 IV 漂移）；观测的 IV 仍是市场 IV。
    """
    cands = []
    for pos, contract in pos_list:
        g = option_greeks.get(pos.vt_symbol)
        if not g:
            continue
        iv = g.get("iv")
        if iv is None or iv <= 0:
            continue
        d = _conv_delta(pos.vt_symbol, g.get("underlying_price"), g.get("strike"), g.get("days_to_expiry"))
        if d is None:
            d = g.get("delta") or 0        # 换算Δ 算不出（缺 K/到期）→ 退回市场 Δ
        d = abs(d)
        itm = bool(g.get("is_itm"))
        cands.append((d, itm, iv))
    if not cands:
        return None
    # 非 ITM 优先
    non_itm = [c for c in cands if not c[1]] or cands
    non_itm.sort(key=lambda c: abs(c[0] - 0.5))
    top = non_itm[: min(3, len(non_itm))]
    ivs = sorted(c[2] for c in top)
    n = len(ivs)
    return ivs[n // 2] if n % 2 == 1 else (ivs[n // 2 - 1] + ivs[n // 2]) / 2.0


def _compute_iv_rate(by_underlying, option_greeks, now_ts):
    """
    合约级 ATM IV 速率 = (5min窗口 IV 极差) / σ_ref，5min 滚动窗口。
    按月份合约分别计算（SC2611/SC2612 各自独立窗口，互不平滑）；
    单位 = 百分数（值 5.0 即 5%）；断档 >4h 清空。
    返回 {sym: (rate, 窗口低点, 窗口高点)}，sym 如 SC2611（月键 = 标的期货合约代码）。
    """
    per_sym = {}   # {期货合约代码如 SC2611: 当前 ATM IV}
    for (und_raw, expiry), pos_list in (by_underlying or {}).items():
        if not und_raw:
            continue
        und = normalize_underlying(und_raw) if und_raw else ""
        if not und:
            continue
        iv = _atm_iv_of_group(pos_list, option_greeks)
        if iv is None:
            continue
        # 月键：标的期货合约代码（CFFEX 期权前缀 MO → 期货 IM；月号已在 und 尾部）
        sym_up = und_raw.upper()
        sym = ("IM" + sym_up[2:]) if sym_up.startswith("MO") else sym_up
        per_sym[sym] = iv

    samples = _shared_state.setdefault("_iv_samples", {})
    out = {}
    for sym, iv_now in per_sym.items():
        und = normalize_underlying(sym)
        dq = samples.get(sym)
        if dq is None:
            dq = deque()
            samples[sym] = dq
        # 断档检测：与上一样本间隔 > 4h → 清空
        if dq and (now_ts - dq[-1][0]) > _IV_GAP_SEC:
            dq.clear()
        dq.append((now_ts, iv_now))
        # 剔除 >5min 的过期样本
        while dq and (now_ts - dq[0][0]) > _IV_WINDOW_SEC:
            dq.popleft()
        # 5min 窗口 H/L（采样仍是秒级）
        if len(dq) >= 2:
            vals = [v for _, v in dq]
            lo, hi = min(vals), max(vals)
            if lo > 0:
                # 口径：5min IV 极差占 σ_ref 的比例，用百分数表示（值 5.0 = 5%）
                #   iv 是百分数、σ_ref 是小数 → (hi-lo)/σ_ref 即百分号上的数字
                out[sym] = ((hi - lo) / _sigma_of(und), lo, hi)
    return out


def _compute_burn(positions_out, yesterday_snapshot):
    """
    品种级 Premium Burn = 今日净浮亏 / |昨日卖方净收权利金|。
    昨日卖方净权利金 ≤0 → 该品种跳过。
    返回 {und: burn}。
    """
    agg = defaultdict(lambda: {"loss": 0.0, "premium": 0.0})
    for p in positions_out or []:
        # 期权才有 option_type（期货为 ""；CTP 值是中文 '看涨期权'/'看跌期权'，不能按 C/P 判）
        if not p.get("option_type"):
            continue
        und = normalize_underlying(p.get("symbol", "").split(".")[0])
        if not und:
            continue
        dir_sign = 1.0 if p.get("direction") == "short" else -1.0
        vol = abs(p.get("volume") or 0)
        size = p.get("size") or 1
        cur = p.get("adjust_price") or p.get("last_price") or 0
        # 昨收价：快照 adjust_price
        snap = yesterday_snapshot.get(f"{p.get('symbol')}_{p.get('direction')}")
        prev = None
        if isinstance(snap, dict):
            prev = snap.get("adjust_price")
        elif isinstance(snap, (int, float)):
            prev = snap
        if prev is None or prev <= 0 or cur <= 0:
            continue
        # 卖方的权利金收入（正）
        if dir_sign > 0:
            agg[und]["premium"] += prev * vol * size
        # 今日浮亏（对卖方：昨收 − 现价；对买方：现价 − 昨收）
        pnl = (prev - cur) * vol * size * dir_sign
        if pnl < 0:
            agg[und]["loss"] += -pnl
    out = {}
    for und, a in agg.items():
        prem = a["premium"]
        if prem and prem > 0:
            out[und] = a["loss"] / prem
    return out


def _compute_structure(positions_out):
    """
    合约级结构风险（设计稿 v0.2 §五·③）：
      逐腿 |换算Δ| ≥ Y → 黄；该腿已越界实值（K 与 F 异侧）→ 红。
      换算Δ = 固定 σ_ref 下的 Δ（口径稳定，不随市场 IV 漂移）；看板 Δ 列仍显示市场 Δ。
      档距（左右两档行权价之差）仅作展示字段，不参与触发。
    返回 {合约代码: (level, |换算Δ|, Y)}。
    """
    y = _get_thresh("conv_delta_warn")
    if not y or y <= 0:
        return {}
    out = {}
    for p in positions_out or []:
        # 期权才有 option_type（期货为 ""；CTP 值是中文 '看涨期权'/'看跌期权'，不能按 C/P 判）
        if not p.get("option_type"):
            continue
        code = (p.get("symbol") or "").split(".")[0]
        if not code:
            continue
        if abs(p.get("volume") or 0) <= 0:
            continue
        d = _conv_delta(p.get("symbol"), p.get("underlying_price"),
                        p.get("strike"), p.get("days_to_expiry"))
        if d is None:
            continue
        d = abs(d)
        if d < y:
            continue
        out[code] = ("danger" if p.get("is_itm") else "warn", round(d, 4), y)
    return out


def _evaluate_alerts(f_rate, iv_rate, burn, structure, contract_und, margin_status, now_ts):
    global _ALERT_WARMUP
    """
    汇总五源 → 判级 → 冷却/计数 → 写 alerts / active_flags / active_details / popups。
    返回本轮应弹窗的 popups 列表。
    """
    _replay_alert_state()   # 启动后首轮：回放落盘的 prev_active / alert_state / alerts
    today = datetime.datetime.fromtimestamp(now_ts).strftime("%Y%m%d")
    events = []   # (source, symbol, value, level, threshold)
    if _ALERT_WARMUP:
        _ALERT_WARMUP = False
        return []
    pts = {}      # (source, symbol) -> (起点值, 报警时值)，仅移动量类告警有
    def _push(source, symbol, value, warn_k, danger_k):
        w = _get_thresh(warn_k)
        d = _get_thresh(danger_k) if danger_k else None
        lv = _level_of(value, w, d)
        if lv:
            thr = d if lv == "danger" else w
            # 窗口门：非窗口期不弹窗（但记录照写）
            # 此处简化：直接让 _evaluate_alerts 处理窗口限制（后续交付中已包含）
            events.append((source, symbol, value, lv, thr))

    # F / IV 速率是移动量：连同"从多少到多少"的两端一起上报
    for und, (rate, p0, p1) in (f_rate or {}).items():
        _push("f_rate", und, rate, "f_rate_warn", "f_rate_danger")
        pts[("f_rate", und)] = (p0, p1)
    for und, (rate, p0, p1) in (iv_rate or {}).items():
        _push("iv_rate", und, rate, "iv_rate_warn", None)
        pts[("iv_rate", und)] = (p0, p1)
    for und, v in (burn or {}).items():
        _push("burn", und, v, "burn_warn", "burn_danger")
    # 结构风险：级别由「是否实值」定，不比较阈值（红=已越界实值）
    # 源键用 conv_delta（换算Δ），与看板的市场 Δ 列区分开
    for code, (lv, v, thr) in (structure or {}).items():
        events.append(("conv_delta", code, v, lv, thr))
    if margin_status and margin_status.get("level") in ("warn", "danger"):
        lv = margin_status["level"]
        thr = _get_thresh("margin_ratio_danger" if lv == "danger" else "margin_ratio_warn")
        events.append(("margin", "ACCOUNT", margin_status.get("ratio_pct"), lv, thr))

    # 当前仍触发的标记：active_flags 按品种聚合（行变色），active_details 按源分列（单元格标记）
    active = {}
    details = {}
    def _bump(und, lv):
        if not und:
            return
        prev = active.get(und)
        if prev == "danger":
            return
        active[und] = "danger" if lv == "danger" else (prev or "warn")

    und_of = {}
    for source, symbol, value, lv, thr in events:
        und = (contract_und or {}).get(symbol) if source == "conv_delta" else symbol
        # f_rate/iv_rate 的 symbol 是月份合约（RU2611）→ 归一成品种（RU），active_flags/窗口门/品种下拉保持品种级
        # margin 的 symbol="ACCOUNT" 非合约代码，normalize 会解析失败，跳过
        if source != "margin":
            und = normalize_underlying(und or symbol) or und
        und_of[(source, symbol)] = und
        details.setdefault(source, {})[symbol] = lv
        _bump(und, lv)
    _shared_state["active_flags"] = active
    _shared_state["active_details"] = details

    # 冷却 + 计数 → 决定是否弹窗 + 写历史
    state = _shared_state.setdefault("alert_state", {})
    prev_active = _shared_state.setdefault("prev_active", {})
    hist = _shared_state.setdefault("alerts", [])
    popups = []
    _dirty = False
    cur_active = {}
    for source, symbol, value, lv, thr in events:
        key = f"{source}|{symbol}"
        cur_active[key] = lv
        # 只在状态跳变（新出现 / 级别变化）时记录+弹窗，持续触发不重复刷屏
        if prev_active.get(key) == lv:
            continue
        _dirty = True
        alert_id = f"{source}|{symbol}|{lv}"
        st = state.setdefault(alert_id, {})
        if st.get("date") != today:
            st.update(date=today, count=0, last_popup=0.0)
        should = False
        und = und_of.get((source, symbol)) or symbol
        # 窗口门：非交易时段跳变只记录不弹窗、不耗当日计数（规格：非窗口期只拦弹窗不拦记录）
        if _alert_window_ok(source, und, now_ts):
            if st["count"] < _ALERT_DAILY_MAX:
                cd = _ALERT_COOLDOWN.get(lv, 600)
                if now_ts - st.get("last_popup", 0.0) >= cd:
                    st["count"] += 1
                    st["last_popup"] = now_ts
                    should = True
        p0, p1 = pts.get((source, symbol), (None, None))
        # 最简格式：{类型} {对象}: {值}，{低点→报警点}（仅移动量类有尾段）；背景色已表达级别，无红黄字样/阈值
        if source == "f_rate":
            tail = f"，{p0:.0f}→{p1:.0f}" if p0 is not None and p1 is not None else ""
            msg = f"F_rate {symbol}: {value:.2f}{tail}"
        elif source == "iv_rate":
            tail = f"，{p0:.2f}→{p1:.2f}" if p0 is not None and p1 is not None else ""
            msg = f"IV {symbol}: {value:.2f}{tail}"
        elif source == "burn":
            msg = f"Burn {symbol}: {value:.2f}"
        elif source == "conv_delta":
            msg = f"Δ {symbol}: {value:.2f}"
        else:  # margin（账户级，无对象）
            msg = f"Margin: {value:.2f}"
        rec = {
            "alert_id": alert_id,
            "ts": datetime.datetime.fromtimestamp(now_ts).strftime("%Y-%m-%d %H:%M:%S"),
            "source": source, "level": lv, "symbol": symbol, "underlying": und,
            "value": round(value, 6) if isinstance(value, float) else value,
            "v_from": round(p0, 6) if isinstance(p0, float) else p0,
            "v_to": round(p1, 6) if isinstance(p1, float) else p1,
            "threshold": thr, "msg": msg, "popup": should,
        }
        hist.append(rec)
        if should:
            popups.append(rec)
    # 消除事件：prev_active 中本轮消失的 warn/danger → normal（记录 + 条件弹窗，窗口门同触发）
    for key, lv in list(prev_active.items()):
        if key in cur_active or lv not in ("warn", "danger"):
            continue
        _dirty = True
        source, symbol = key.split("|", 1)
        und = (contract_und or {}).get(symbol) if source == "conv_delta" else symbol
        # 同触发侧：合约键归一成品种；ACCOUNT 跳过；und 缺失回退 symbol 本身
        if source != "margin":
            und = normalize_underlying(und or symbol) or und
        alert_id = f"{source}|{symbol}|normal"
        st = state.setdefault(alert_id, {})
        if st.get("date") != today:
            st.update(date=today, count=0, last_popup=0.0)
        should = False
        if _alert_window_ok(source, und, now_ts):
            if st["count"] < _ALERT_DAILY_MAX:
                cd = _ALERT_COOLDOWN.get(lv, 600)
                if now_ts - st.get("last_popup", 0.0) >= cd:
                    st["count"] += 1
                    st["last_popup"] = now_ts
                    should = True
        rec = {
            "alert_id": alert_id,
            "ts": datetime.datetime.fromtimestamp(now_ts).strftime("%Y-%m-%d %H:%M:%S"),
            "source": source, "level": "normal", "symbol": symbol, "underlying": und,
            "value": None, "v_from": None, "v_to": None,
            "threshold": None, "msg": f"{symbol} 已恢复正常", "popup": should,
        }
        hist.append(rec)
        if should:
            popups.append(rec)
    _shared_state["prev_active"] = cur_active
    if _dirty:
        _save_alert_state({
            "prev_active": cur_active,
            "alert_state": state,
            "alerts": hist[-_ALERT_RING_MAX:],
            "saved_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        })
    if len(hist) > _ALERT_RING_MAX:
        del hist[: len(hist) - _ALERT_RING_MAX]
    return popups


@api_bp.route("/dashboard", methods=["GET"])
def api_dashboard():
    """返回完整看板快照（持仓树 + Greeks + 账户 + CTP状态）"""
    snap = _snapshot()
    payload = {
        "status": snap["ctp_status"],
        "error": snap["ctp_error"],
        "last_update": snap["last_update"],
        "uptime_seconds": int(time.time() - _SERVER_START),
        "summary": snap["summary"],
        "tree": snap["tree"],
        "positions": snap["positions"],
        "underlying_prices": snap["underlying_prices"],
        "account": snap["account"],
        "worker_alive": snap["worker_alive"],
        "settlement_dict": snap["settlement_dict"],
        "settlement_prices": snap["settlement_prices"],
        "margin_status": snap.get("margin_status", {}),
        "hours_missing": sorted(snap.get("hours_missing") or []),
        "alerts": _alerts_today(snap.get("alerts", [])),
        "active_flags": snap.get("active_flags", {}),
        "active_details": snap.get("active_details", {}),
        "contract_und": snap.get("contract_und", {}),
        "popups": snap.get("popups", []),
    }
    return jsonify(_clean_nan(payload))


def _alerts_today(alerts):
    """告警历史只回当天（跨自然日自动清空，与弹窗计数口径一致）。"""
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    return [a for a in (alerts or []) if str(a.get("ts", "")).startswith(today)]


@api_bp.route("/ctp/connect", methods=["POST"])
def api_ctp_connect():
    """
    接收 CTP 连接参数（中文键），启动 Worker 线程。
    Body (JSON): {用户名, 密码, 经纪商代码, 交易服务器, 行情服务器, 产品名称, 授权编码}
    """
    global _worker_thread

    data = request.get_json() or {}
    # 7 个中文键均为用户填写、不可为空；前端没传时自动回退已保存配置
    required = ["用户名", "密码", "经纪商代码", "交易服务器", "行情服务器", "产品名称", "授权编码"]
    missing = [k for k in required if not (data.get(k) or "").strip()]
    if missing:
        # 回退：内存 _ctp_credential
        fb = dict(_ctp_credential)
        still_missing = [k for k in missing if not (fb.get(k) or "").strip()]
        if not still_missing:
            data = fb
        else:
            # 再回退：ctp_accounts.json active 账户
            try:
                acct = _load_accounts()
                active = acct.get("active", "") or "默认"
                fb2 = acct.get("accounts", {}).get(active, {})
                still_missing2 = [k for k in still_missing if not (fb2.get(k) or "").strip()]
                if not still_missing2:
                    data = fb2
                    _ctp_credential.clear()
                    _ctp_credential.update(fb2)
            except Exception:
                pass
        missing = [k for k in required if not (data.get(k) or "").strip()]
        if missing:
            return jsonify({"success": False, "error": "以下字段不可为空: " + "、".join(missing)}), 400

    # 保存凭证（中文键，直接作为 VNPYEngine.ctp_setting）
    _ctp_credential.clear()
    _ctp_credential.update(data)

    # 持久化到 ctp_accounts.json 的 active 账户
    try:
        acct = _load_accounts()
        active_name = acct.get("active", "") or "默认"
        acct["accounts"][active_name] = dict(data)
        acct["active"] = active_name
        _save_accounts(acct)
    except Exception:
        pass

    # 如果已有 Worker 在跑，先停止
    _stop_worker()

    # 启动新 Worker
    _ctp_stop_event.clear()
    settlement_dir = data.get("settlement_dir", "结算单")
    _worker_thread = threading.Thread(
        target=_worker_loop, args=(settlement_dir,), daemon=True
    )
    _worker_thread.start()

    return jsonify({"success": True, "message": "连接启动中", "status": "connecting"})


@api_bp.route("/ctp/config", methods=["GET"])
def api_ctp_config():
    """返回当前 active 账户的 CTP 凭证（中文键，供前端表单回填）。"""
    acct = _load_accounts()
    active = acct.get("active", "")
    creds = acct.get("accounts", {}).get(active, {})
    return jsonify({k: creds.get(k, "") for k in _ACCOUNT_KEYS})


@api_bp.route("/ctp/accounts", methods=["GET"])
def api_ctp_accounts():
    """返回所有账号名列表（按插入顺序）。"""
    acct = _load_accounts()
    names = list(acct.get("accounts", {}).keys())
    return jsonify({"accounts": names, "active": acct.get("active", "")})


@api_bp.route("/ctp/account/load", methods=["POST"])
def api_ctp_account_load():
    """选择账号 → 设为 active 并返回其 7 字段。Body: {name}"""
    data = request.get_json() or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"status": "error", "message": "name 为空"}), 400
    acct = _load_accounts()
    creds = acct.get("accounts", {}).get(name)
    if not creds:
        return jsonify({"status": "error", "message": f"账号不存在: {name}"}), 404
    acct["active"] = name
    _save_accounts(acct)
    return jsonify({"status": "ok", "name": name, "data": {k: creds.get(k, "") for k in _ACCOUNT_KEYS}})


@api_bp.route("/ctp/account/save", methods=["POST"])
def api_ctp_account_save():
    """保存/更新账号（不连接）。Body: {name, data:{7键}}"""
    data = request.get_json() or {}
    name = (data.get("name") or "").strip()
    fields = data.get("data") or {}
    if not name:
        return jsonify({"status": "error", "message": "name 为空"}), 400
    if not all(fields.get(k, "").strip() for k in _ACCOUNT_KEYS):
        return jsonify({"status": "error", "message": "7 个字段不可为空"}), 400
    acct = _load_accounts()
    acct.setdefault("accounts", {})[name] = {k: fields.get(k, "") for k in _ACCOUNT_KEYS}
    acct["active"] = name
    _save_accounts(acct)
    return jsonify({"status": "ok", "name": name})


@api_bp.route("/ctp/disconnect", methods=["POST"])
def api_ctp_disconnect():
    """停止 Worker 线程，断开 CTP"""
    _stop_worker()
    return jsonify({"success": True, "message": "已断开", "status": "disconnected"})


@api_bp.route("/_diag", methods=["GET"])
def api_diag():
    """临时诊断端点：暴露 worker/engine 原始状态，定位后删除"""
    eng = _engine
    out = {"engine": eng is not None}
    if eng is not None and eng.main_engine is not None:
        me = eng.main_engine
        out["positions"] = len(me.get_all_positions())
        out["contracts"] = len(me.get_all_contracts())
        out["ticks"] = len(me.get_all_ticks())
        out["accounts"] = len(me.get_all_accounts())
        try:
            out["subscribed"] = len(eng.get_all_subscribed())
        except Exception as e:
            out["subscribed"] = f"ERR {e}"
        gw = me.gateways.get("CTP")
        out["gateway"] = bool(gw)
        if gw:
            out["gw_login_status"] = bool(getattr(gw.td_api, "login_status", None)) if getattr(gw, "td_api", None) else None
            out["gw_positions_raw"] = len(getattr(gw, "positions", {}) or {})
            # gateway 内部状态
            try:
                from vnpy_ctp.gateway.ctp_gateway import symbol_contract_map
                out["symbol_contract_map_size"] = len(symbol_contract_map)
            except Exception as e:
                out["symbol_contract_map_error"] = str(e)
            try:
                td = gw.td_api
                out["td_reqid"] = td.reqid if hasattr(td, "reqid") else None
                out["td_connect_status"] = getattr(td, "connect_status", None)
                out["td_login_status"] = getattr(td, "login_status", None)
                out["td_investor_id"] = getattr(td, "userid", None)
                # 账户原始数据
                if hasattr(td, "account_data") and td.account_data:
                    out["td_account"] = {k: v for k, v in td.account_data.items() if k not in ("BrokerID",)}
                # gateway.positions 原始数据
                if hasattr(gw, "positions") and gw.positions:
                    out["td_gateway_positions"] = {k: {"vol": v.volume, "dir": str(v.direction), "symbol": v.symbol} for k, v in gw.positions.items()}
                # contract_inited
                out["td_contract_inited"] = getattr(td, "contract_inited", None)
                # 结算单/合约查询流程中间状态
                out["td_settlement_info_confirmed"] = getattr(td, "settlement_info_confirmed", "N/A")
                out["td_settlement_file_opened"] = getattr(td, "_settlement_file_opened", "N/A")
                out["td_last_trade_day"] = getattr(td, "last_trade_day", "N/A")
                out["td_reqid"] = td.reqid if hasattr(td, "reqid") else None
                # 查询结算单回调计数
                out["td_settlement_req_count"] = getattr(td, "_settlement_req_count", "N/A")
                # 查询合约回调计数
                out["td_instrument_req_count"] = getattr(td, "_instrument_req_count", "N/A")
                # 直接读 __dict__ 中所有以 _td_ 或带 settlement/instrument 的键
                td_keys = {k: str(v)[:50] for k, v in td.__dict__.items() 
                          if any(x in k.lower() for x in ["settlement", "instrument", "contract", "trade_day"])}
                out["td_matching_keys"] = td_keys
            except Exception as e:
                out["td_internal_error"] = str(e)
            # 主动触发一次持仓查询
            if request.args.get("force") == "1":
                gw.query_position()
                import time; time.sleep(3)
                out["positions_after_query"] = len(me.get_all_positions())
                out["gw_positions_raw_after"] = len(getattr(gw, "positions", {}) or {})
                # 再次检查 gateway.positions 原始内容
                if hasattr(gw, "positions") and gw.positions:
                    out["td_gateway_positions_after"] = {k: {"vol": v.volume, "dir": str(v.direction), "symbol": v.symbol} for k, v in gw.positions.items()}
    # 监控预警内部状态（排查弹窗用）
    out["alert_state"] = {k: dict(v) for k, v in _shared_state.get("alert_state", {}).items()}
    out["popups_current"] = len(_shared_state.get("popups", []))
    out["active_flags"] = dict(_shared_state.get("active_flags", {}))
    out["alert_rings"] = len(_shared_state.get("alerts", []))
    return jsonify(out)


@api_bp.route("/ctp/status", methods=["GET"])
def api_ctp_status():
    """返回 CTP 连接状态。status 为字符串，connected 为布尔（兼容前端两种读法）。"""
    snap = _snapshot()
    st = snap["ctp_status"]
    # 登录错误码/信息（若引擎暴露，best-effort）
    login_error_id, login_error_msg = 0, ""
    try:
        if _engine and _engine.main_engine:
            gw = _engine.main_engine.gateways.get("CTP")
            if gw and getattr(gw, "td_api", None):
                login_error_id = getattr(gw.td_api, "login_error_id", 0) or 0
                login_error_msg = getattr(gw.td_api, "login_error_msg", "") or ""
    except Exception:
        pass
    return jsonify({
        "status": st,
        "connected": st == "connected",
        "error": snap["ctp_error"],
        "worker_alive": snap["worker_alive"],
        "last_update": snap["last_update"],
        "login_error_id": login_error_id,
        "login_error_msg": login_error_msg,
    })


def _stop_worker():
    """停止 Worker 线程（最多等3秒）"""
    global _worker_thread
    _ctp_stop_event.set()
    if _worker_thread and _worker_thread.is_alive():
        _worker_thread.join(timeout=3.0)
    _worker_thread = None
    with _shared_lock:
        _shared_state["ctp_status"] = "disconnected"
        _shared_state["worker_alive"] = False


# ── App Factory ──────────────────────────────────────────────────────────────

def create_app(settlement_dir: str = "结算单", static_folder=None, template_folder=None,
                instance_info: Optional[dict] = None) -> Flask:
    """
    创建 Flask 应用

    Args:
        settlement_dir: 结算单目录路径
        static_folder: 静态文件目录（默认 dashboard_v2/static）
        template_folder: 模板目录（默认 dashboard_v2/templates）
    """
    # 推算项目根目录（api_server.py → dashboard_v2 → 项目根目录）
    _parent_dir = _os.path.dirname(_os.path.abspath(__file__))   # dashboard_v2
    _project_root = _os.path.dirname(_parent_dir)                # C:/qproj
    if static_folder is None:
        static_folder = _os.path.join(_project_root, "static")
    if template_folder is None:
        template_folder = _os.path.join(_project_root, "templates")

    app = Flask(__name__, static_folder=static_folder, template_folder=template_folder)

    # 实例信息（单实例管理用）
    _instance_info = instance_info or {}

    # ── /api/health ──────────────────────────────────────────────────────────────
    @app.route("/api/health")
    def _health():
        """健康检查 + 实例信息，供 Mutex 冲突时远程探测"""
        with _shared_lock:
            ctp = _shared_state.get("ctp_status", "unknown")
            worker = _shared_state.get("worker_alive", False)
        info = {
            "status": "running",
            "pid": _os.getpid(),
            "instance": _instance_info or {},
            "ctp_status": ctp,
            "worker_alive": worker,
        }
        return jsonify(info)

    # ── /api/shutdown ─────────────────────────────────────────────────────────────
    @app.route("/api/shutdown", methods=["POST"])
    def _shutdown():
        """受控停止服务（必须 POST）"""
        import signal as _signal
        # 改状态
        _shared_state["instance_status"] = "stopping"
        # 通知 atexit 清理
        def _deferred_shutdown():
            import os as _os
            _os.kill(_os.getpid(), _signal.SIGTERM)
        import threading as _t
        _t.Thread(target=_deferred_shutdown, daemon=True).start()
        return jsonify({"message": "shutdown scheduled"})

    # 注册 API Blueprint
    app.register_blueprint(api_bp)

    # 提供静态文件路由（可选：直接访问 /dashboard11.js）
    @app.route("/")
    def index():
        from flask import send_from_directory
        return send_from_directory(template_folder, "dashboard.html")

    @app.route("/favicon.ico")
    def favicon():
        return "", 204

    @app.route("/api/columns")
    def _get_columns():
        """返回当前列配置（含顺序、显隐、fmt掩码）；进程首次访问时从盘载入"""
        global _COLUMNS_LOADED
        if not _COLUMNS_LOADED:
            _COLUMNS_LOADED = True
            try:
                import os as _os
                from dashboard_v2.alert_config import CONFIG_DIR
                path = _os.path.join(CONFIG_DIR, "columns_config.json")
                if _os.path.exists(path):
                    with open(path, encoding="utf-8") as f:
                        cols = json.load(f)
                    if isinstance(cols, list) and len(cols) >= 14:
                        with _shared_lock:
                            _shared_state["column_config"] = cols
                        logger.info(f"[columns] 已从 {path} 载入列配置")
            except Exception as e:
                logger.error(f"[columns] 载入失败: {e}")
        with _shared_lock:
            cols = _shared_state.get("column_config", None)
        if cols is None:
            return jsonify({"error": "not configured"})
        return jsonify({"columns": cols})

    @app.route("/api/columns", methods=["POST"])
    def _save_columns():
        """保存列配置：内存 + 落盘 config/columns_config.json（重启/换设备不丢）"""
        data = request.get_json()
        if not data or "columns" not in data:
            return jsonify({"error": "invalid payload"}), 400
        cols = data["columns"]
        if not isinstance(cols, list) or len(cols) < 14:
            return jsonify({"error": "invalid payload"}), 400
        with _shared_lock:
            _shared_state["column_config"] = cols
        global _COLUMNS_LOADED
        try:
            import os as _os
            from dashboard_v2.alert_config import CONFIG_DIR
            _os.makedirs(CONFIG_DIR, exist_ok=True)
            path = _os.path.join(CONFIG_DIR, "columns_config.json")
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(cols, f, ensure_ascii=False)
            _os.replace(tmp, path)
            _COLUMNS_LOADED = True
        except Exception as e:
            logger.error(f"[/api/columns] 落盘失败: {e}")
            return jsonify({"ok": False, "error": str(e)}), 500
        return jsonify({"ok": True})

    # ══════════════════════════════════════════════════════════════════════════
    # 监控预警配置：阈值 + σ_ref
    # ══════════════════════════════════════════════════════════════════════════
    @app.route("/api/alert/settings")
    def _get_alert_settings():
        """返回当前阈值配置（含默认区间，供前端渲染设置菜单）。"""
        from dashboard_v2.alert_config import THRESHOLD_SPEC, DEFAULT_SETTINGS
        cur = _load_alerts(force=False)
        return jsonify({
            "settings": cur,
            "defaults": DEFAULT_SETTINGS,
            "spec": {k: {"lo": v["lo"], "hi": v["hi"], "step": v["step"], "label": v["label"]}
                     for k, v in THRESHOLD_SPEC.items()},
        })

    @app.route("/api/alert/settings", methods=["POST"])
    def _save_alert_settings():
        """保存阈值（部分更新即可）。"""
        patch_data = request.get_json(silent=True) or {}
        if not isinstance(patch_data, dict):
            return jsonify({"error": "invalid payload"}), 400
        saved = _save_alerts(patch_data)
        return jsonify({"ok": True, "settings": saved})

    @app.route("/api/sigma_ref")
    def _get_sigma_ref():
        """σ_ref 全量 + 空缺清单（供设置页签渲染 CSV 内容）。"""
        from dashboard_v2.alert_config import sigma_ref_pending, SIGMA_DEFAULT, SIGMA_CSV
        with _shared_lock:
            held = list(_shared_state.get("_held_products", []))
        rows = sigma_ref_pending(held or list(_load_sigma().keys()))
        # 把「有值但当前无持仓」的也返回，避免用户看不到自己填的行
        table = _load_sigma()
        held_set = {r["symbol"] for r in rows}
        for sym in sorted(table.keys()):
            if sym not in held_set:
                rows.append({"symbol": sym, "sigma_ref": table[sym],
                             "effective": table[sym], "source": "config", "held": False})
        for r in rows:
            r.setdefault("held", True)
        return jsonify({"rows": rows, "default": SIGMA_DEFAULT, "csv_path": SIGMA_CSV})

    @app.route("/api/sigma_ref/pending")
    def _get_sigma_pending():
        """弹窗专用：仅返回「有持仓但待填」的品种（已扣「忽略一次」）。"""
        with _shared_lock:
            held = list(_shared_state.get("_held_products", []))
        return jsonify({"missing": _sigma_missing(held)})

    @app.route("/api/sigma_ref/ack", methods=["POST"])
    def _post_sigma_ack():
        """「忽略一次」：本月内不再弹该品种。"""
        from dashboard_v2.alert_config import save_sigma_ack, current_month_key
        data = request.get_json(silent=True) or {}
        syms = data.get("symbols") or []
        if not isinstance(syms, list) or not syms:
            return jsonify({"error": "symbols 必填（数组）"}), 400
        acked = save_sigma_ack([str(s) for s in syms])
        return jsonify({"ok": True, "month": current_month_key(), "acked": acked})

    @app.route("/api/sigma_ref/save", methods=["POST"])
    def _save_sigma_ref_csv():
        """用户从前端面板手动提交 CSV 内容（纯文本保存）。"""
        raw = request.data.decode('utf-8')
        lines = [l.strip() for l in raw.split('\n') if l.strip() and not l.strip().startswith('#')]
        if not lines:
            return jsonify({"error": "empty content"}), 400
        try:
            import os as _os
            from dashboard_v2.alert_config import CONFIG_DIR
            path = _os.path.join(CONFIG_DIR, "iv_sigma_ref.csv")
            _os.makedirs(CONFIG_DIR, exist_ok=True)
            saved = 0
            with open(path, 'w', encoding='utf-8-sig', newline='') as f:
                f.write("symbol,sigma_ref\n")
                for line in lines:
                    # parse CSV row -> normalize symbol -> clamp sigma
                    parts = [p.strip() for p in line.split(',')]
                    if len(parts) < 2:
                        continue
                    sym = parts[0].strip().upper()
                    raw_val = parts[1].strip()
                    if not raw_val:
                        continue          # 空值 = 占位行，跳过（不写 0）
                    try:
                        sigma_val = float(raw_val)
                    except ValueError:
                        continue          # 非数字跳过，不吞掉整批
                    if sigma_val <= 0:
                        continue          # 0 或负数视为未填
                    if sigma_val > 1.5:
                        sigma_val = sigma_val / 100.0
                    if not (0.02 <= sigma_val <= 1.50):
                        continue
                    f.write(f"{sym},{sigma_val:.4f}\n")
                    saved += 1
            from dashboard_v2.alert_config import load_sigma_ref
            load_sigma_ref(force=True)
            return jsonify({"ok": True, "csv_path": path, "rows_saved": saved})
        except Exception as e:
            logger.error(f"[/api/sigma_ref/save] 落盘失败: {e}")
            return jsonify({"error": str(e)}), 500

    @app.route("/api/debug/pos")
    def _debug_pos():
        """临时诊断：直接调 query_positions() 看返回什么"""
        global _engine
        if _engine is None:
            return jsonify({"error": "no engine"})
        pos = _engine.query_positions()
        return jsonify({"count": len(pos), "positions": [{"vt": p.vt_symbol, "vol": p.volume, "dir": str(p.direction)} for p in pos]})

    @app.route("/api/debug/trigger_settlement", methods=["POST"])
    def _debug_trigger_settlement():
        """
        手动触发 CTP 结算单查询（调试用）。
        Body (JSON): {"TradingDay": "20260909"}  // 可选，默认今天
        返回 {"triggered": bool, "td_found": bool, "req_return": int, "trading_date_sent": str}
        """
        global _engine
        if _engine is None:
            return jsonify({"error": "CTP未连接"}), 503
        if not _engine.main_engine:
            return jsonify({"error": "engine未初始化"}), 503

        req_json = request.get_json(silent=True) or {}
        trading_date = req_json.get("TradingDay", datetime.datetime.now().strftime("%Y%m%d"))

        result = {"triggered": False, "td_found": False, "req_return": None, "trading_date_sent": trading_date}

        for gw_name, gw in _engine.main_engine.gateways.items():
            if not hasattr(gw, "td_api"):
                continue
            td = gw.td_api
            result["td_found"] = True
            result["td_class"] = type(td).__name__
            req = {
                "BrokerID": getattr(td, "brokerid", ""),
                "InvestorID": getattr(td, "userid", ""),
                "TradingDay": trading_date
            }
            rid = getattr(td, "reqid", 0) + 1
            n = td.reqQrySettlementInfo(req, rid)
            result["req_return"] = n
            result["triggered"] = True
            break

        return jsonify(result)

    return app

# ══════════════════════════════════════════════════════════════════════════
# 告警状态持久化（重启不丢状态、消除事件独立冷却）
# ══════════════════════════════════════════════════════════════════════════
_ALERT_STATE_FILE = "C:/qproj/快照/alert_state.json"

def _load_alert_state():
    try:
        import os
        if os.path.exists(_ALERT_STATE_FILE):
            with open(_ALERT_STATE_FILE, encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {}

def _save_alert_state(state):
    """原子写：tmp + os.replace，防止写一半崩溃留坏文件。"""
    try:
        import os
        tmp = _ALERT_STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
        os.replace(tmp, _ALERT_STATE_FILE)
    except Exception:
        pass

_ALERT_STATE_LOADED = False

def _replay_alert_state():
    """服务启动后首轮评估前，把落盘的 prev_active / alert_state / alerts 回放进 _shared_state（只回放一次）。"""
    global _ALERT_STATE_LOADED
    if _ALERT_STATE_LOADED:
        return
    _ALERT_STATE_LOADED = True
    data = _load_alert_state()
    if not data:
        return
    if data.get("prev_active") is not None:
        _shared_state["prev_active"] = dict(data["prev_active"])
    if data.get("alert_state") is not None:
        _shared_state["alert_state"] = dict(data["alert_state"])
    if data.get("alerts") is not None:
        _shared_state["alerts"] = list(data["alerts"])
    try:
        logger.info(f"[alert] 状态回放：prev={len(data.get('prev_active') or {})} "
                    f"state={len(data.get('alert_state') or {})} hist={len(data.get('alerts') or [])}")
    except Exception:
        pass

