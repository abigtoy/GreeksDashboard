# risk_engine.py — Greeks + PnL + 树形聚合 + 阈值打标
# 纯函数，输入基本数值，输出结果字典
# 依赖: pricing.py, settlement.py（均为纯函数，无外部状态）

from dashboard_v2.pricing import black76
from dashboard_v2.settlement import load_settlement_cost

import math
import datetime
import re
from collections import defaultdict

# =======================================================================
# 常量
# =======================================================================
CONTRACT_SIZE = {
    'IF': 300, 'IH': 300, 'IC': 200, 'IM': 200,
    'IO': 100, 'MO': 100, 'HO': 100,
    'CU': 5,  'AL': 5,  'ZN': 5,  'PB': 5,  'NI': 1,  'SN': 1,
    'AU': 1000, 'AG': 15,
    'RB': 10, 'HC': 10, 'WR': 10, 'SS': 5, 'RU': 10,
    'M': 10,  'Y': 10,  'A': 10,  'B': 10,  'P': 10,
    'JM': 60, 'J': 100,
    'MA': 10, 'TA': 5,  'EG': 10,  'PF': 5,
    'L': 10,  'V': 10,  'PP': 10,
    'SC': 100,
    'SF': 5,  'SM': 5,
    'C': 10,  'CS': 10,
}

CFFEX_MAP = {'IO': 'IF', 'MO': 'IM', 'HO': 'IH'}

# =======================================================================
# 合约解析
# =======================================================================
def _parse_symbol(symbol: str) -> tuple:
    """
    解析合约代码，返回 (underlying, expiry, option_type, strike)。
    支持：CFFEX期权(IO2609-C-4000)、SHFE商品(CU2612C75000)、
         标准期货(IF2609)、郑商所期权(MA609C2400)
    """
    # CFFEX 格式: IO2609-C-4000
    m = re.match(r'^([A-Za-z]+)(\d{4})[-]([CP])[-]?(\d+)$', symbol)
    if m:
        raw = m.group(1).upper()
        und = CFFEX_MAP.get(raw, raw)
        return und, m.group(2), m.group(3), int(m.group(4))

    # SHFE/INE/商品期权: CU2612C75000, AU2612P400
    m = re.match(r'^([A-Za-z]+)(\d{4})([CPcp])(\d+)$', symbol)
    if m:
        und = m.group(1).upper()
        return und, m.group(2), m.group(3).upper(), int(m.group(4))

    # 标准期货: IF2609, CU2612
    m = re.match(r'^([A-Za-z]+)(\d{4})$', symbol)
    if m:
        und = m.group(1).upper()
        und = CFFEX_MAP.get(und, und)
        return und, m.group(2), '', 0

    # 郑商所期权: MA609C2400
    m = re.match(r'^([A-Za-z]+)(\d{3,4})([CPcp])(\d+)$', symbol)
    if m:
        und = m.group(1).upper()
        raw_month = m.group(2)
        month = raw_month if len(raw_month) == 4 else '2' + raw_month
        return und, month, m.group(3).upper(), int(m.group(4))

    # Fallback
    digits = ''.join(ch for ch in symbol if ch.isdigit())
    month = digits[-4:] if len(digits) >= 4 else symbol[-4:]
    und = ''.join(ch for ch in symbol.upper() if ch.isalpha())
    return und, month, '', 0


def normalize_underlying(symbol: str) -> str:
    und, _, _, _ = _parse_symbol(symbol)
    return und


def extract_expiry(symbol: str) -> str:
    _, month, _, _ = _parse_symbol(symbol)
    return month


def cp_from_symbol(symbol: str) -> int:
    """从合约代码可靠判定 C(1)/P(-1)。
    注意：CTP 的 contract.option_type 值是中文（'看涨期权'/'看跌期权'），
    不能用作 startswith('C') 判据（永远为假 → 全部误判为 Put）。"""
    _, _, otype, _ = _parse_symbol(symbol.split('.')[0])
    return 1 if otype.upper() == 'C' else -1


# =======================================================================
# 阈值打标
# =======================================================================
THRESHOLDS = {
    "delta":  {"warning": 50000,  "danger": 100000},
    "gamma":  {"warning": 30000,  "danger": 60000},
    "pnl":    {"danger": -50000},
}

def tag_delta(dc: float) -> str:
    a = abs(dc)
    if a >= THRESHOLDS["delta"]["danger"]:  return "red"
    if a >= THRESHOLDS["delta"]["warning"]: return "yellow"
    return "green"

def tag_gamma(gc: float) -> str:
    a = abs(gc)
    if a >= THRESHOLDS["gamma"]["danger"]:  return "red"
    if a >= THRESHOLDS["gamma"]["warning"]: return "yellow"
    return "green"

def tag_pnl(pnl: float) -> str:
    if pnl < THRESHOLDS["pnl"]["danger"]: return "red"
    if pnl < 0: return "yellow"
    return "green"


# =======================================================================
# adjust_price
# =======================================================================
def calc_adjust_price(tick: dict, contract: dict) -> float:
    """
    三级调整价（详见基线 §3.2）：
      1. 正常流动性合约：mid_price 或 last_price
      2. 深度实值 ITM：PCP 平价 + OTM 腿时间价值反推（需同到期日 OTM 腿 ask/bid）
      3. 深度虚值 OTM / 盘口宽价差：盘口挂单量 + 动态价差过滤
    实现：当前统一用 last_price（或 pre_close 兜底）；ITM/OTM 三级精细化可在
          Worker 两阶段计算后回填 tick['adjust_price']，此处作为最终兜底。
    """
    # 优先用 Worker（pricing.price_options_batch）预算的四级调整价
    adj = tick.get('adjust_price')
    if adj and adj > 0:
        return adj
    price = tick.get('last_price', 0)
    if price and price > 0:
        return price
    return tick.get('pre_close', 0) or tick.get('prev_close', 0) or 0.0


# =======================================================================
# 3.4 calc_greeks（兼容期货与期权）
# =======================================================================
# 中金所股指系：期货乘数 300/200，期权乘数 100 —— 同一列里 Δ 不可比。
# 归一：delta_norm = delta × (size/100)。期货腿 ×3/×2，期权腿 ×1 不变，
# 全列统一到"100 元/点"口径（文华习惯）。仅股指系生效，商品品种一律 ×1。
# 注意：deltacash 始终用原始 pos_delta×F×size 计算，与本归一无关（不受影响）。
_IDX_CFFEX = {'IF', 'IH', 'IC', 'IM', 'IO', 'HO', 'MO'}

def _idx_norm_factor(symbol, size) -> float:
    s = str(symbol or '').split('.')[0].upper()
    prod = s[:2]
    if prod in _IDX_CFFEX:
        try:
            f = float(size) / 100.0
            return f if f > 0 else 1.0
        except (TypeError, ValueError):
            return 1.0
    return 1.0


def calc_greeks(tick: dict, position: dict, contract: dict) -> dict:
    """
    计算持仓 Greeks + Cash Greeks。
    position: { symbol, direction, volume, size }
    contract: { size, option_type, strike, expiry, ttm, product_type }
    tick:     { underlying_price, last_price, iv }

    返回: { delta, gamma, vega, theta, pos_delta, pos_gamma, pos_vega,
            pos_theta, deltacash, gammacash, vegacash, thetacash }
    """
    direction_sign = 1 if position['direction'] in ('long', '多') else -1
    vol = abs(position['volume'])  # FIX 2.4: 数量已带符号，方向只由 direction_sign 决定，取绝对值防双重取号
    size = contract.get('size', 1)
    F = tick.get('underlying_price', 0) or tick.get('last_price', 0)

    # --- 分支1: 期货 ---
    if contract.get('product_type') == 'FUTURES' or not contract.get('option_type'):
        pos_delta = vol * direction_sign * 1.0
        return {
            # 可汇总列一律头寸级（原始×方向×手数）；父级直接 Σ 子级
            "delta": pos_delta, "gamma": 0.0, "vega": 0.0, "theta": 0.0,
            "delta_norm": pos_delta * _idx_norm_factor(position['symbol'], size),
            "pos_delta": pos_delta,
            "pos_gamma": 0.0, "pos_vega": 0.0, "pos_theta": 0.0,
            "deltacash": round(pos_delta * F * size),
            "gammacash": 0, "vegacash": 0, "thetacash": 0,
        }

    # 分支2: 期权合约（iv 来自 tick['iv']，由 §3.2 链路计算）
    # `or 0.20`：iv 可能是显式 None（key 存在但值为 None，.get 默认值不生效）
    iv    = tick.get('iv') or 0.20
    strike = contract.get('strike', 0)
    T      = max(contract.get('days_to_expiry', 90) / 365.0, 0.5 / 365.0)  # C1/#3: 统一 T 下限 0.5/365
    cp    = cp_from_symbol(position['symbol'])  # FIX #1: 从合约代码判定，勿用中文 option_type 字段

    # 无标的价（F≤0）：无法计算 Greeks，返回零值（避免 math domain error）
    if not F or F <= 0:
        return {
            "delta": 0, "gamma": 0, "vega": 0, "theta": 0, "delta_norm": 0,
            "pos_delta": 0, "pos_gamma": 0, "pos_vega": 0, "pos_theta": 0,
            "deltacash": 0, "gammacash": 0, "vegacash": 0, "thetacash": 0,
        }

    g = black76(iv, F, strike, T, r=0.02, cp=cp)

    pos_delta = g['delta'] * direction_sign * vol
    pos_gamma = g['gamma'] * direction_sign * vol
    pos_vega  = g['vega']  * direction_sign * vol
    pos_theta = g['theta'] * direction_sign * vol  # 统一规则：多头原始值×方向sign（空头theta自然转正）

    return {
        # 可汇总列一律头寸级 = 多头原始值×方向sign×手数；父级直接 Σ 子级
        "delta": pos_delta, "gamma": pos_gamma,
        "vega":  pos_vega,  "theta": pos_theta,
        "delta_norm": pos_delta * _idx_norm_factor(position['symbol'], size),
        "pos_delta": pos_delta, "pos_gamma": pos_gamma,
        "pos_vega":  pos_vega,  "pos_theta": pos_theta,
        "deltacash": round(pos_delta * F * size),
        "gammacash": round(pos_gamma * (F ** 2) * 0.01 * size),  # 1% 标的变动
        "vegacash":  round(pos_vega  * size),
        "thetacash": round(pos_theta * size),
    }


# =======================================================================
# 3.5 PnL 三口径（含新开仓/已平仓处理）
# =======================================================================
def _is_no_tick(tick: dict) -> bool:
    """
    "无行情" 判定 —— 当日盈亏的准入门槛。两种情形都算无行情：
      1) tick 为 None 或无最新价（缓存空、query_tick 超时返回 None）
      2) tick.datetime 不是今天（重连后 vnpy 缓存里的往日报价：有价但非今日推送）
    理由：这两类价都不能证明"今天这个合约的盈亏变化"，计入会把陈旧成本基准当今日盈亏。
    ponytail: 自然日近似 CTP 交易日，跨零点的夜盘品种（sc/au 等）在 00:00 后会被判无行情；
              升级路径 = 注入 trading_day 参数替代本机 date()。
    """
    if not tick:
        return True
    if not (tick.get('last_price') or tick.get('price')):
        return True
    dt = tick.get('datetime')
    if dt is not None and dt.date() != datetime.date.today():
        return True
    return False


def calc_pnl(position: dict, contract: dict, tick: dict,
             settlement_dict: dict,
             today_open_cost: dict = None) -> dict:
    """开仓至今的浮动盈亏（pnl_history）与开仓成本。

    当日盈亏不在这里——它是现金口径，见 calc_pnl_today()。
    """
    sym = position['symbol'].split('.')[0]
    direction_str = 'long' if position['direction'] in ('long', '多') else 'short'
    pos_key = f"{sym}_{direction_str}"

    direction_sign = 1 if direction_str == 'long' else -1
    vol  = abs(position['volume'])
    size = contract.get('size', 1)

    if vol == 0:
        return {"pnl_history": 0.0, "cost_price": 0.0, "cost_basis": "closed"}

    # 开仓成本（pnl_history 基准）：结算单开仓均价 → 今日账本开仓价 → CTP 持仓均价
    # 结算单 key 带方向后缀（IC2612_long），裸名读不到 → 必须用 pos_key
    cost_price = settlement_dict.get(pos_key, 0.0) or 0.0
    cost_basis = "settlement_cost"
    if cost_price <= 0:
        ledger_open = (today_open_cost or {}).get(pos_key) or 0.0
        if ledger_open > 0:
            cost_price, cost_basis = ledger_open, "ledger_open_cost"
        else:
            cost_price = position.get('price', 0.0) or 0.0
            cost_basis = "position_price"

    adj_price = tick.get('adjust_price', tick.get('last_price', 0))
    pnl_history = direction_sign * (adj_price - cost_price) * vol * size

    return {
        "pnl_history": round(pnl_history, 2),
        "cost_price":  cost_price,
        "cost_basis":  cost_basis,
    }


def calc_pnl_today(pos_key: str, direction_sign: int, vol: int, size: int,
                   adj_price: float, cash_flow: dict, prev_mv: dict) -> float:
    """当日盈亏（现金口径），品种级逐级相加即得合计，无需摊派。

        当日盈亏 = 今持仓市值 − 昨持仓市值 + 今日成交净现金

    cash_flow: {pos_key → 今日卖出成交额 − 买入成交额}，由成交账本汇总。
    prev_mv:   {pos_key → 昨持仓市值}，取自 T-1 收盘快照。
    """
    mv_now  = direction_sign * vol * size * adj_price
    mv_prev = float(prev_mv.get(pos_key, 0.0) or 0.0)
    cash    = float(cash_flow.get(pos_key, 0.0) or 0.0)
    return round(cash + mv_now - mv_prev, 2)


# =======================================================================
# 树形聚合
# =======================================================================
def build_tree(positions: list, ticks: dict, contracts: dict,
               settlement_dict: dict,
               today_open_cost: dict = None,
               cash_flow: dict = None,
               prev_mv: dict = None,
               night_gated: set = None) -> dict:
    """
    构建 L1→L2→L3 嵌套树。
    返回: { summary, tree }
    """
    # 按品种→月份分组
    product_map = defaultdict(lambda: defaultdict(list))

    for pos in positions:
        sym     = pos['symbol'].split('.')[0]
        product = normalize_underlying(sym)
        expiry  = extract_expiry(sym)
        product_map[product][expiry].append(pos)

    total = _make_summary()
    tree  = []

    for product in sorted(product_map.keys()):
        l1_children = []
        l1_metrics  = _make_metrics()

        for month in sorted(product_map[product].keys()):
            l3_nodes = []
            for pos in product_map[product][month]:
                node = _build_l3_node(pos, ticks, contracts, settlement_dict,
                                      today_open_cost, cash_flow, prev_mv,
                                      night_gated)
                if node:
                    l3_nodes.append(node)
                    # L3 只进 L2，L1 汇总在 L2→L1 阶段做（避免 L1 双计）

            if not l3_nodes:
                continue

            # L2 节点：聚合子 L3 Greeks（不在此层做 L1 聚合）
            l2_metrics = _make_metrics()
            for n in l3_nodes:
                _accumulate_metrics(l2_metrics, n)

            l2_node = {
                "key": f"{product}_{month}",
                "name": f"{product}{month}",     # 如 AU2610
                "type": "L2_MONTH",
                "metrics": l2_metrics,
                "children": l3_nodes,
            }
            l1_children.append(l2_node)
            _accumulate_metrics(l1_metrics, l2_node)  # L1 = Σ L2（含所有 L3）

        if l1_children:
            l1_node = {
                "key": product,
                "name": product,
                "type": "L1_PRODUCT",
                "metrics": l1_metrics,
                "children": l1_children,
            }
            tree.append(l1_node)
            _accumulate_summary(total, l1_metrics, sum(len(l2['children']) for l2 in l1_children))

    return {"summary": total, "tree": tree}


def _make_metrics() -> dict:
    return {
        "volume": 0,
        "delta": 0.0, "gamma": 0.0, "vega": 0.0, "theta": 0.0,
        "deltacash": 0, "gammacash": 0, "vegacash": 0, "thetacash": 0,
        "pnl_today": 0.0, "pnl_history": 0.0,
    }


def _make_summary() -> dict:
    return {
        "total_deltacash": 0, "total_gammacash": 0,
        "total_vegacash": 0,  "total_thetacash": 0,
        "total_pnl_today": 0.0, "total_pnl_history": 0.0, "position_count": 0,
        "pnl_basis_counts": {},
        # PnL 基准计数用于告警 + 义务仓权利金（CTP 口径：空头期权 × open_price × size）
        "obligation_premium": 0.0,
    }


def _accumulate_metrics(metrics: dict, node: dict) -> None:
    """L3 有 symbol 无 children；L1/L2 有 children"""
    if node.get('children') is None and node.get('symbol'):
        m = node          # L3
    else:
        m = node.get('metrics', {})  # L1/L2

    metrics["volume"]       += abs(m.get('volume', 0))
    metrics["delta"]       += m.get('delta', 0)
    metrics["gamma"]       += m.get('gamma', 0)
    metrics["vega"]        += m.get('vega', 0)
    metrics["theta"]       += m.get('theta', 0)
    metrics["deltacash"]   += m.get('deltacash', 0)
    metrics["gammacash"]   += m.get('gammacash', 0)
    metrics["vegacash"]    += m.get('vegacash', 0)
    metrics["thetacash"]   += m.get('thetacash', 0)
    # PnL 基准可见性：L3 叶子带 price_basis，逐级合并计数（仅告警/核对用，不参与数值）
    _pb = m.get('price_basis')
    if _pb:
        _c = metrics.setdefault('pnl_basis_counts', {})
        _c[_pb] = _c.get(_pb, 0) + 1
    elif m.get('pnl_basis_counts'):
        _c = metrics.setdefault('pnl_basis_counts', {})
        for _k, _v in m['pnl_basis_counts'].items():
            _c[_k] = _c.get(_k, 0) + _v
    # NaN 安全：跳过 None/NaN 不累加，防止整链污染
    _v = m.get('pnl_today', 0)
    if _v is not None and _v == _v:  # NaN != NaN
        metrics["pnl_today"] += _v
    _v2 = m.get('pnl_history', 0)
    if _v2 is not None and _v2 == _v2:
        metrics["pnl_history"] += _v2
    # 义务仓权利金累加：L3 取自身字段；L1/L2 向上传递子级已累计的 _obligation_premium_sum
    if node.get('children') is None and node.get('symbol'):
        op = m.get('obligation_premium', 0)
    else:
        op = m.get('_obligation_premium_sum', 0)
    if op > 0:
        metrics['_obligation_premium_sum'] = metrics.get('_obligation_premium_sum', 0) + op


def _accumulate_summary(total: dict, metrics: dict, l3_count: int = 1) -> None:
    total["total_deltacash"]   += metrics.get("deltacash", 0)
    total["total_gammacash"]   += metrics.get("gammacash", 0)
    total["total_vegacash"]    += metrics.get("vegacash", 0)
    total["total_thetacash"]   += metrics.get("thetacash", 0)
    # F1 基准计数并入 summary
    if metrics.get("pnl_basis_counts"):
        _c = total.setdefault("pnl_basis_counts", {})
        for _k, _v in metrics['pnl_basis_counts'].items():
            _c[_k] = _c.get(_k, 0) + _v
    # NaN 安全
    _tv = metrics.get("pnl_today", 0)
    if _tv is not None and _tv == _tv:
        total["total_pnl_today"] += _tv
    _hv = metrics.get("pnl_history", 0)
    if _hv is not None and _hv == _hv:
        total["total_pnl_history"] += _hv
    total["position_count"]    += l3_count
    # 义务仓权利金汇总
    op = metrics.get('_obligation_premium_sum', 0)
    if op > 0:
        total['obligation_premium'] = total.get('obligation_premium', 0) + op


def _build_l3_node(pos: dict, ticks: dict, contracts: dict,
                   settlement_dict: dict,
                   today_open_cost: dict = None,
                   cash_flow: dict = None,
                   prev_mv: dict = None,
                   night_gated: set = None) -> dict | None:
    sym      = pos['symbol'].split('.')[0]
    contract = contracts.get(sym, {})
    tick     = ticks.get(sym, {})

    adj_price = calc_adjust_price(tick, contract) if tick else (pos.get('price', 0) or 0.0)

    g    = calc_greeks(tick, pos, contract)
    tick_with_adj = dict(tick) if tick else {}
    tick_with_adj['adjust_price'] = adj_price
    direction_raw = pos.get('direction', 'long')
    direction_str = 'long' if direction_raw in ('long', '多') else 'short'
    pos_key = f"{sym}_{direction_str}"

    pnl = calc_pnl(pos, contract, tick_with_adj, settlement_dict, today_open_cost)
    # 没开盘就没有盈亏：夜盘窗口内、没有夜盘时段的品种（IF/IC/IM/MO/IO…）
    # 此时无 tick → adj_price 拿不到 → 今市值算成 0，公式只剩 −昨市值，
    # 编出上千万假亏。这类腿当日盈亏强制 0，不进现金口径。
    gated = normalize_underlying(sym) in (night_gated or ())
    if gated:
        pnl_today = 0.0
    else:
        pnl_today = calc_pnl_today(
            pos_key, 1 if direction_str == 'long' else -1,
            abs(pos.get('volume', 0) or 0), contract.get('size', 1) or 1,
            adj_price, cash_flow or {}, prev_mv or {})

    # ITM 判断
    itm = False
    if contract.get('option_type'):
        F  = tick.get('underlying_price', 0) or tick.get('last_price', 0)
        K  = contract.get('strike', 0)
        cp = cp_from_symbol(sym)  # FIX #1: 同 calc_greeks，勿用中文 option_type
        itm = (F > K) if cp == 1 else (F < K)
    
    # 义务仓权利金（当前市值口径：空头期权 × 最新价 × size）
    obligation_premium = 0.0
    is_option = bool(contract.get('option_type'))
    if is_option and direction_str == 'short':
        size = contract.get('size', 1)
        vol = abs(pos.get('volume', 0))
        last_px = tick.get('last_price', 0) or 0
        if last_px != last_px:  # NaN 防传染
            last_px = 0.0
        if last_px > 0:
            obligation_premium = round(vol * last_px * size, 2)
    
    return {
        "key":             f"{sym}_{direction_str}",
        "name":            f"{sym}{direction_str}",
        "symbol":          sym,
        "direction":       direction_str,
        "direction_raw":   direction_raw,
        "volume":          pos.get('volume', 0),
        "last_price":      tick.get('last_price', 0),
        "adjust_price":    adj_price,
        "open_price":      pnl.get('cost_price', settlement_dict.get(f"{sym}_{direction_str}", pos.get('price', 0))),
        "underlying_price":tick.get('underlying_price', 0),
        "iv":              tick.get('iv', None),
        "days_to_expiry":  contract.get('days_to_expiry', None),
        "itm":             itm,
        "delta":  g.get('delta_norm', g.get('delta', 0)),   # 显示/聚合用归一口径（股指系期货×3/×2）；deltacash 用原始 pos_delta 不受影响
        "gamma":  g.get('gamma', 0),
        "vega":   g.get('vega', 0),
        "theta":  g.get('theta', 0),
        "deltacash": g.get('deltacash', 0),
        "gammacash": g.get('gammacash', 0),
        "vegacash":  g.get('vegacash', 0),
        "thetacash": g.get('thetacash', 0),
        "pnl_today":   pnl_today,
        "pnl_history": pnl.get('pnl_history', 0),
        "price_basis": "not_open" if gated else "cash_flow",
        "cost_basis":  pnl.get('cost_basis', 'n/a'),
        "delta_tag": tag_delta(g.get('deltacash', 0)),
        "gamma_tag": tag_gamma(g.get('gammacash', 0)),
        "pnl_tag":   tag_pnl(pnl.get('pnl_history', 0)),
        # 义务仓权利金（仅用于 summary 汇总）
        "obligation_premium": obligation_premium,
    }


# =======================================================================
# 冒烟测试
# =======================================================================
if __name__ == "__main__":
    import json, pathlib
    from pprint import pprint

    settlement_dir = pathlib.Path(
        "C:/Quant_2026/期货执行策略/vnpy接口封装/结算单"
    )
    candidates = sorted(settlement_dir.glob("full_*.json"))
    if candidates:
        with open(candidates[-1], encoding='utf-8') as f:
            sdata = json.load(f)
        sdict = load_settlement_cost(sdata)
        print(f"结算单 {len(sdict)} 条")

    positions = [
        {"symbol": "IF2609",         "direction": "long",  "volume": 2,  "size": 300, "price": 4230.0},
        {"symbol": "IC2609",         "direction": "short", "volume": 1,  "size": 200, "price": 7746.2},
        {"symbol": "IO2609-C-4000",  "direction": "short", "volume": 20, "size": 100, "price": 45.2},
        {"symbol": "CU2612",         "direction": "long",  "volume": 3,  "size": 5,   "price": 72000.0},
    ]

    ticks = {
        "IF2609":         {"last_price": 4230.0, "underlying_price": 4230.0, "iv": None},
        "IC2609":         {"last_price": 7746.2, "underlying_price": 7746.2, "iv": None},
        "IO2609-C-4000":  {"last_price": 45.2,  "underlying_price": 4230.0, "iv": 16.5},
        "CU2612":         {"last_price": 72000.0,"underlying_price": 72000.0,"iv": None},
    }

    contracts = {
        "IF2609":         {"size": 300, "product_type": "FUTURES"},
        "IC2609":         {"size": 200, "product_type": "FUTURES"},
        "IO2609-C-4000":  {"size": 100, "option_type": "C", "strike": 4000.0,
                           "ttm": 30/365, "days_to_expiry": 25, "product_type": "OPTION"},
        "CU2612":         {"size": 5,   "product_type": "FUTURES"},
    }

    result = build_tree(positions, ticks, contracts, sdict)

    print("\n=== Summary ===")
    pprint(result["summary"])

    print("\n=== Tree (L1 keys) ===")
    for l1 in result["tree"]:
        print(f"  {l1['key']}: deltacash={l1['metrics']['deltacash']}, "
              f"gammacash={l1['metrics']['gammacash']}, "
              f"pnl_history={l1['metrics']['pnl_history']}")

    # 验证 IO2609-C-4000 的品种归一化
    print("\n=== 品种归一化验证 ===")
    for s in ["IO2609-C-4000", "MO2609-P-6500", "IF2609", "CU2612"]:
        print(f"  {s} → {normalize_underlying(s)}, expiry={extract_expiry(s)}")
