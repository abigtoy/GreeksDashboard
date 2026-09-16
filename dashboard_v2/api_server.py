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
from collections import defaultdict
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
from dashboard_v2.risk_engine import build_tree
from dashboard_v2.pricing import price_options_batch, days_to_expiry
from dashboard_v2.settlement import SettlementManager

# ── 共享状态（Worker 写，API 读，无锁 Python 对象）────────────────────────────
# 均为 Python 对象，无锁，Worker 线程写，Flask API 读
# API 读取时是原子快照引用，不会有撕裂问题

_shared_lock = threading.RLock()          # 保护 _ctp_status / _worker_thread
_SERVER_START = time.time()               # 服务进程启动时间（uptime 基准，非客户端计时）
_shared_state = {
    "positions": [],          # list[dict]  最新持仓快照
    "underlying_prices": {},  # {symbol: price}
    "tree": [],               # list  树形结构
    "contracts": {},          # {symbol: contract_dict}  合约元数据（含 size/strike/days_to_expiry）
    "settlement_dict": {},    # {symbol: net_cost}       净仓开仓均价
    "account": {},            # dict  账户信息
    "ctp_status": "disconnected",   # "disconnected" | "connecting" | "connected" | "error"
    "ctp_error": "",          # str   错误信息
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
        {"col": "pnl_daily",        "label": "盯日盈亏",  "visible": True, "fmt": "0"},
        {"col": "pnl_today",        "label": "当日盈亏",  "visible": True, "fmt": "0"},
        {"col": "pnl_history",      "label": "浮动盈亏",  "visible": True, "fmt": "0"},
    ],
}

# ── CTP 连接参数（由 /api/ctp/connect 设置，Worker 启动时读取）────────────────
# 全链路中文键：{用户名, 密码, 经纪商代码, 交易服务器, 行情服务器, 产品名称, 授权编码}
_ctp_credential = {}       # 中文键 dict，直接作为 VNPYEngine.cctp_setting 传入
_ctp_stop_event = threading.Event()
_worker_thread: Optional[threading.Thread] = None

# ── 工具函数 ────────────────────────────────────────────────────────────────

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
    with _shared_lock:
        _shared_state["ctp_status"] = status
        if error is not None:
            _shared_state["ctp_error"] = error
        if alive is not None:
            _shared_state["worker_alive"] = alive


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


def _worker_loop(settlement_dir: str):
    """
    CTP Worker 主循环（连接 + 重连阶梯 + 轮询 + 自动快照）：
    - 外层：连接/重连循环。连接失败按 fast(3s×10)→idle(30min) 阶梯重试。
    - 内层：connected 后每秒轮询持仓；探测到掉线（login_status=False）→ 丢弃旧引擎，break 回外层重连。
    - 自动快照只在内层 _poll_once 成功后调 _maybe_auto_save()（内部还有 ctp_status==connected 守卫）。
      重连与快照解耦：本模块只负责让 engine 活着，快照只读 ctp_status 作数据边界。
    """
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
    _local_complete = (
        len(_settlement_manager._cost_cache) > 0
        and _settlement_manager._meta.get("loaded", False)
    )

    def _do_settlement_sync():
        try:
            # 本地数据齐全时，跳过网络请求
            if _local_complete:
                logger.info("[结算单] 本地数据已齐全，跳过 sync")
                return
            sync_result = _settlement_manager.sync()
            logger.info(f"[结算单] sync_result={sync_result}")
            # sync 成功后标记为完整
            if sync_result.get("missing_filled") or sync_result.get("today_updated"):
                _local_complete = True
        except Exception as e:
            logger.error(f"[结算单] sync 异常: {e}")

    attempts = 0
    disconnect_retry_count = 0   # 记录连续掉线次数（用于自动重连上限）
    while not _ctp_stop_event.is_set():
        _set_status("connecting", None, alive=True)
        eng, err, retryable = _connect_engine(cred)
        if eng is not None:
            # 连接成功
            _engine = eng
            attempts = 0
            disconnect_retry_count = 0
            _set_status("connected", "")

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
                            continue
                    else:
                        # 10次重连均失败 → 通知前端弹窗，等用户手动处理
                        _set_status("error",
                                    f"CTP 连续掉线{disconnect_retry_count}次，请检查网络或重连",
                                    alive=False)
                        return
                # 每次轮询都取最新结算数据（sync 线程可能已更新 _settlement_manager）
                _settlement_manager.load_costs_from_meta()   # 确保缓存是最新的
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
                _maybe_auto_save()
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
    global _engine
    _engine = engine

    # ── 读取持仓 ──────────────────────────────────────────────────────────────
    raw_positions = engine.query_positions()
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
        if ft:
            lp = ft.last_price or (getattr(ft, "pre_close", 0) or 0)
        option_ticks[pos.vt_symbol] = {
            "last_price":       lp,
            "bid_price_1":      0,
            "ask_price_1":      0,
            "underlying_price": lp,
            "iv":               None,
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
            contracts[sym] = {
                "size":           contract.size or 1,
                "product_type":   "OPTION",
                "option_type":    contract.option_type.value if contract.option_type else "",
                "strike":         contract.option_strike or 0,
                "days_to_expiry": days_to_expiry(expiry_str),
            }

    settlement_cost_dict  = settlement_data
    settlement_prices_dict = settlement_prices

    # ── 写共享状态 ────────────────────────────────────────────────────────────
    # build_tree 是纯函数，需要 ticks + contracts + settlement_dict
    try:
        tree = build_tree(positions_out, ticks, contracts,
                          settlement_cost_dict, settlement_prices_dict)
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
    logger.info(f"[_poll_once] positions_out={len(positions_out)}, contracts={len(contracts)}, tree_nodes={len(tree.get('tree',[]))}")
    with _shared_lock:
        _shared_state["positions"] = positions_out
        _shared_state["underlying_prices"] = underlying_prices
        _shared_state["tree"] = tree
        _shared_state["contracts"] = contracts
        _shared_state["settlement_dict"] = settlement_cost_dict
        _shared_state["settlement_prices"] = settlement_prices_dict
        _shared_state["account"] = account_data
        _shared_state["last_update"] = datetime.datetime.now()


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

    if 20.0 <= h < 24.0:
        return ("N", (dt + datetime.timedelta(days=1)).strftime("%Y%m%d"))
    if 0.0 <= h < 2.5:
        return ("N", dt.strftime("%Y%m%d"))
    if 8.0 <= h < 11.5:
        return ("A", dt.strftime("%Y%m%d"))
    if 12.0 <= h < 15.0:
        return ("P", dt.strftime("%Y%m%d"))
    return (None, None)


def _snapshot_name_for(dt: datetime.datetime = None):
    """返回 (filename, business_date, session)，窗口外 filename=None"""
    session, business_date = _current_session(dt)
    if not session:
        return (None, None, None)
    return (f"data_snapshot_{business_date}_{session}.json", business_date, session)


def _positions_hash(positions):
    """positions 列表的 sha256，用于判定是否需要落盘（与旧文件对比）"""
    canonical = json.dumps(positions, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _snapshot_path(name: str) -> str:
    return _os.path.join(_SNAPSHOT_DIR, name)


def _ensure_snapshot_dir():
    _os.makedirs(_SNAPSHOT_DIR, exist_ok=True)


# ── 自动快照模块（与重连模块解耦：只读 _shared_state["ctp_status"] 作数据边界）──
# 触发：connected 且（时段边界 或 距上次满 30min）
# 落盘：positions hash 与旧文件不同才覆盖；时段边界强制覆盖；连接中的真空仓亦存
_SNAP_INTERVAL = 30 * 60          # 秒
_snap_state = {
    "last_ts": 0.0,               # 上次尝试时间（time.time()）
    "last_key": None,             # 上次 (业务日, session)
}


def _maybe_auto_save():
    """
    自动保存当前快照 → 快照/data_snapshot_{业务日}_{session}.json（单文件覆盖）。
    守护1：仅 ctp_status == "connected" 才存 → 掉线期/重连期绝不覆盖好数据。
    守护2：窗口外（无 session）不存。
    守护3：数据未变且非时段边界 → 跳过（空仓首次仍会写）。
    """
    snap = _snapshot()
    if snap["ctp_status"] != "connected":
        return  # 连接守卫：掉线/重连期不落盘

    now = datetime.datetime.now()
    filename, business_date, session = _snapshot_name_for(now)
    if not filename:
        return  # 非交易窗口

    key = (business_date, session)
    now_ts = time.time()
    boundary = (key != _snap_state["last_key"])
    if not boundary and (now_ts - _snap_state["last_ts"]) < _SNAP_INTERVAL:
        return

    positions = snap["positions"]
    data_hash = _positions_hash(positions)

    _ensure_snapshot_dir()
    filepath = _snapshot_path(filename)

    # 非时段边界 + 数据未变 → 跳过写盘
    if not boundary and _os.path.isfile(filepath):
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                old = json.load(f)
            if old.get("data_hash") == data_hash:
                _snap_state["last_ts"] = now_ts
                return
        except Exception:
            pass

    payload = {
        "version": 3,
        "saved_at": now.isoformat(),
        "trading_date": business_date,
        "session": session,
        "ctp_status": snap["ctp_status"],
        "data_hash": data_hash,
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
    try:
        tmp = filepath + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        _os.replace(tmp, filepath)   # 原子替换，避免读到半截文件
        _snap_state["last_ts"] = now_ts
        _snap_state["last_key"] = key
    except Exception:
        pass


@api_bp.route("/snapshot/save", methods=["POST"])
def api_snapshot_save():
    """手动保存当前持仓快照到固定文件（覆盖写）。返回 {"message":"快照已保存","status":"ok"}。"""
    try:
        snap = _snapshot()
        now = datetime.datetime.now()
        _, business_date, session = _snapshot_name_for(now)
        payload = {
            "version": 3,
            "saved_at": now.isoformat(),
            "trading_date": business_date,
            "session": session,
            "ctp_status": snap["ctp_status"],
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
            json.dump(payload, f, ensure_ascii=False, indent=2)
        _os.replace(tmp, filepath)
        return jsonify({"status": "ok", "message": "快照已保存"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@api_bp.route("/snapshots", methods=["GET"])
def api_snapshots_list():
    """列出快照目录内所有快照，返回 [{name, trading_date, session, saved_at, position_count}]"""
    _ensure_snapshot_dir()
    files = sorted(
        f for f in _os.listdir(_SNAPSHOT_DIR)
        if f.startswith(_SNAPSHOT_PREFIX) and f.endswith(_SNAPSHOT_EXT)
    )
    result = []
    for fname in files:
        path = _os.path.join(_SNAPSHOT_DIR, fname)
        try:
            with open(path, "r", encoding="utf-8") as f:
                d = json.load(f)
            result.append({
                "name": fname,
                "trading_date": d.get("trading_date", ""),
                "session": d.get("session", ""),
                "saved_at": d.get("saved_at", ""),
                "position_count": len(d.get("raw", {}).get("positions", [])),
                "ctp_status": d.get("ctp_status", ""),
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
    }
    return jsonify(_clean_nan(payload))


@api_bp.route("/ctp/connect", methods=["POST"])
def api_ctp_connect():
    """
    接收 CTP 连接参数（中文键），启动 Worker 线程。
    Body (JSON): {用户名, 密码, 经纪商代码, 交易服务器, 行情服务器, 产品名称, 授权编码}
    """
    global _worker_thread

    data = request.get_json() or {}
    # 7 个中文键均为用户填写、不可为空
    required = ["用户名", "密码", "经纪商代码", "交易服务器", "行情服务器", "产品名称", "授权编码"]
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

def create_app(settlement_dir: str = "结算单", static_folder=None, template_folder=None) -> Flask:
    """
    创建 Flask 应用

    Args:
        settlement_dir: 结算单目录路径
        static_folder: 静态文件目录（默认 dashboard_v2/static）
        template_folder: 模板目录（默认 dashboard_v2/templates）
    """
    # 推算项目根目录（api_server.py → dashboard_v2 → 项目根目录）
    _project_root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
    if static_folder is None:
        static_folder = _os.path.join(_project_root, "dashboard_v2", "static")
    if template_folder is None:
        template_folder = _os.path.join(_project_root, "dashboard_v2", "templates")

    app = Flask(__name__, static_folder=static_folder, template_folder=template_folder)

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
        """返回当前列配置（含顺序、显隐、fmt掩码）"""
        with _shared_lock:
            cols = _shared_state.get("column_config", None)
        if cols is None:
            return jsonify({"error": "not configured"})
        return jsonify({"columns": cols})

    @app.route("/api/columns", methods=["POST"])
    def _save_columns():
        """保存列配置"""
        data = request.get_json()
        if not data or "columns" not in data:
            return jsonify({"error": "invalid payload"}), 400
        cols = data["columns"]
        with _shared_lock:
            _shared_state["column_config"] = cols
        return jsonify({"ok": True})

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
