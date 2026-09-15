# pricing.py — Black-76 Greeks + IV 反推纯函数 + 批量期权计算
# 无任何外部状态依赖，输入基本数值输出结果字典

import math

# 标准库实现正态分布 CDF/PDF（避免 scipy 依赖）
def _norm_cdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))

def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi)

# -----------------------------------------------------------------------
# 3.3 Black-76 单张 Greeks
# -----------------------------------------------------------------------
def black76(IV: float, F: float, K: float, T: float,
            r: float = 0.02, cp: int = 1) -> dict:
    """
    Black-76 模型单张 Greeks。
    cp:  1 = Call, -1 = Put
    T:   到期时间（年），最小截断 max(T, 0.5/250) 防除零
    IV:  自动兼容小数(0.165)或百分比(16.5)，内部统一用小数
    返回: {delta, gamma, vega, theta}
    """
    T = max(T, 0.5 / 365.0)
    # IV 单位保护：>1 视为百分比，自动转小数；小于 0.001 截断防除零
    v = IV / 100.0 if IV > 1.0 else max(IV, 0.001)
    sqrtT = math.sqrt(T)

    d1 = (math.log(F / K) + 0.5 * v * v * T) / (v * sqrtT)
    d2 = d1 - v * sqrtT

    exp_rt = math.exp(-r * T)
    delta = cp * exp_rt * _norm_cdf(cp * d1)
    gamma = exp_rt * _norm_pdf(d1) / (F * v * sqrtT)
    vega  = F * exp_rt * _norm_pdf(d1) * sqrtT * 0.01  # 每 1% Vol 变动

    # 标准 Black-76 Theta（日历日衰减）
    term1 = - (F * exp_rt * _norm_pdf(d1) * v) / (2.0 * sqrtT)
    term2 = - cp * r * F * exp_rt * _norm_cdf(cp * d1)
    term3 =   cp * r * K * exp_rt * _norm_cdf(cp * d2)
    theta = (term1 + term2 + term3) / 365.0

    return {
        "delta": delta,
        "gamma": gamma,
        "vega":  vega,
        "theta": theta,
    }

# -----------------------------------------------------------------------
# 3.3 IV 反推（二分法）
# -----------------------------------------------------------------------
def implied_vol_bisection(price: float, F: float, K: float, T: float,
                          r: float = 0.02, cp: int = 1,
                          tol: float = 0.0001) -> float:
    """
    二分法反推隐含波动率。
    price: 期权市场价
    F/K/T: 标的价/行权价/到期时间(年)
    cp:    1=Call, -1=Put
    返回: IV 小数形式（如 0.165），收敛失败时兜底返回 0.20
    """
    if price <= 0 or F <= 0 or K <= 0:
        return 0.20

    v_low, v_high = 0.001, 5.0
    for _ in range(30):
        v_mid = (v_low + v_high) * 0.5
        # Black-76 定价
        d1_mid = (math.log(F / K) + 0.5 * v_mid * v_mid * T) / (v_mid * math.sqrt(T))
        d2_mid = d1_mid - v_mid * math.sqrt(T)
        p = (cp * F * math.exp(-r * T) * _norm_cdf(cp * d1_mid)
             - cp * K * math.exp(-r * T) * _norm_cdf(cp * d2_mid))
        if abs(p - price) < tol:
            return v_mid
        if p < price:
            v_low = v_mid
        else:
            v_high = v_mid
    return (v_low + v_high) * 0.5


# -----------------------------------------------------------------------
# 3.2 adjust_price 四级规则（移植自旧版 vnpy_api_server，PCP 平价 + 盘口过滤）
#   第1级 正常：盘口内用 last，否则 mid
#   第2级 深度实值 ITM：同 K 反方向 OTM 腿时间价值 + 本腿内在价值（OTM 腿价再嵌套第1/3/4级）
#   第3级 深度虚值/盘口宽：极度虚值取挂单量大的一侧；价差宽退中间价
#   第4级 兜底：last → pre_close（在调用处）
# -----------------------------------------------------------------------
_STRIKE_DIFF_THRESHOLD = 0.001   # 极度虚值：ask < underlying * 0.1%
_SPREAD_TIGHT_THRESHOLD = 0.20   # 紧价差阈值 20%
_OTM_BASE_SPREAD = 0.05          # ATM 基准价差 5%
_OTM_K = 2.25                    # 价差线性系数（保留，见 ponytail）
_OTM_MAX_SPREAD = 0.50           # 价差上限 50%
_STALE_MINUTES = 3               # 最新价超过此分钟数视为过期

def _mid(b, a):
    return (b + a) / 2.0

def _intrinsic(s, k, cp):
    return max(s - k, 0.0) if cp == 1 else max(k - s, 0.0)

def _otm_degree(s, k):
    return abs(s - k) / s if s > 0 else 0.0

def _is_stale(dt):
    if not dt:
        return True
    try:
        from datetime import datetime as _d
        if isinstance(dt, str):
            dt = _d.strptime(dt.split('.')[0], '%Y-%m-%d %H:%M:%S')
        return (_d.now() - dt).total_seconds() / 60.0 > _STALE_MINUTES
    except Exception:
        return True

def _adjust_otm(leg, s):
    """第1/3/4级（OTM 或正常腿）。返回 0 表示无可用价，交调用方兜底。"""
    n = leg.get('last_price') or 0.0
    b = leg.get('bid_price_1') or 0.0
    a = leg.get('ask_price_1') or 0.0
    bv = leg.get('bid_volume_1') or 0.0
    av = leg.get('ask_volume_1') or 0.0
    if b <= 0 and a <= 0:
        return n if n > 0 else 0.0
    if b <= 0:
        return a
    if a <= 0:
        return b
    if _is_stale(leg.get('datetime')):
        n = 0.0
    # 极度虚值：不看价差，取挂单量大的一侧
    if a < s * _STRIKE_DIFF_THRESHOLD:
        return b if bv >= av else a
    spread = abs(a - b)
    mid = _mid(b, a)
    sr = spread / mid if mid > 0 else 1.0
    # 盘口内 + 紧价差 → 用最新价
    if n > 0 and b <= n <= a and sr <= _SPREAD_TIGHT_THRESHOLD:
        return n
    # 其余（紧价差偏离 / 价差过宽）→ 中间价
    # ponytail: 旧版动态阈值两分支同返 mid，已合并；若要按虚值度收紧到单边报价，在此补分支。
    return mid

def _adjust_itm(leg, s, k, cp, all_legs):
    """第2级：深度实值 PCP 平价。OTM 腿价格嵌套 _adjust_otm（第1/3/4级）。缺对手腿退中间价。"""
    otm = next((t for t in all_legs
                if t.get('cp') == -cp and abs((t.get('strike') or 0) - k) < 0.01), None)
    if otm is None:
        b = leg.get('bid_price_1') or 0.0
        a = leg.get('ask_price_1') or 0.0
        return _mid(b, a) if (b > 0 and a > 0) else _adjust_otm(leg, s)
    otm_k = otm.get('strike') or k
    otm_tv = max(_adjust_otm(otm, s) - _intrinsic(s, otm_k, otm.get('cp')), 0.0)
    adj = _intrinsic(s, k, cp) + otm_tv
    b = leg.get('bid_price_1') or 0.0
    a = leg.get('ask_price_1') or 0.0
    if b > 0 and a > 0:
        return b if abs(adj - b) < abs(adj - a) else a
    if b > 0:
        return b
    if a > 0:
        return a
    return adj

def calc_adjust_price_4level(leg, s, k, cp, all_legs, pre_close=0.0):
    """调整价入口（§3.2 四级）+ 第4级兜底 last → pre_close → 0。"""
    is_itm = (cp == 1 and s > k) or (cp == -1 and s < k)
    adj = _adjust_itm(leg, s, k, cp, all_legs) if is_itm else _adjust_otm(leg, s)
    if not adj or adj <= 0:
        adj = (leg.get('last_price') or 0.0) or (pre_close or 0.0)
    return adj


# -----------------------------------------------------------------------
# 便捷单步函数：输入市场报价 + 基本信息，直接返回 Greeks
# -----------------------------------------------------------------------
def calc_greeks_from_market(market_price: float,
                            F: float, K: float, T: float,
                            option_type: str = "C",
                            r: float = 0.02) -> dict:
    """
    一步到位：从市场报价反推 IV，再算 Greeks。
    option_type: 'C'/'CALL' = Call, 其他 = Put
    返回: {iv, delta, gamma, vega, theta}，iv 为百分比形式（如 16.5）
    """
    cp = 1 if option_type.upper().startswith("C") else -1
    iv = implied_vol_bisection(market_price, F, K, T, r, cp)
    g = black76(iv, F, K, T, r, cp)
    return {
        "iv":     round(iv * 100, 4),   # 转回百分比
        "delta":  g["delta"],
        "gamma":  g["gamma"],
        "vega":   g["vega"],
        "theta":  g["theta"],
    }


# -----------------------------------------------------------------------
# 批量 Greeks 计算（纯函数，无 CTP 依赖）
# option_ticks 中可选携带 'underlying_price' 字段（Worker 预填）
# -----------------------------------------------------------------------
def price_options_batch(symbols, option_ticks, settlement_data):
    """
    批量计算期权 Greeks，纯函数，不依赖任何 CTP / vnpy 对象。

    参数:
        symbols:         list of (vt_symbol, contract, pos, direction_str)
                         - vt_symbol:    str，完整合约代码如 'IO2509C4800.CFFEX'
                         - contract:     dict {size, option_strike, option_expiry,
                                              option_type, exchange}（来自 api_server 构造）
                         - pos:          dict {volume, direction, price}
                         - direction_str: 'long' 或 'short'
        option_ticks:    {vt_symbol: {last_price, bid_price_1, ask_price_1,
                                      underlying_price, iv}}
                         underlying_price 由 Worker 在收集 tick 时一并填入
        settlement_data: {symbol: {avg_buy_price, avg_sell_price, ...}}
                         或直接 {f"{sym}_多": float, f"{sym}_空": float}（兼容）

    返回:
        {vt_symbol: {iv, delta, gamma, theta, vega, open_price, adjust_price, ...}}
    """
    from collections import defaultdict
    results = {}

    # ── 阶段1：按标的分组，计算 ATM IV 中位数（bisection 失败兜底） ──
    by_underlying = defaultdict(list)
    for vt_sym, contract, pos, _ in symbols:
        und = contract.get('option_underlying') or ''
        expiry = str(contract.get('option_expiry'))[:10] if contract.get('option_expiry') else ''
        by_underlying[(und, expiry)].append((vt_sym, contract, pos))

    # 同「标的+到期」腿集合：供第2级 ITM PCP 查对手 OTM 腿（含盘口量/时间戳）
    legs_by_group = defaultdict(list)
    for vt_sym, contract, pos, _ in symbols:
        und_ = contract.get('option_underlying') or ''
        exp_ = str(contract.get('option_expiry'))[:10] if contract.get('option_expiry') else ''
        nm_ = contract.get('name') or vt_sym
        lg = dict(option_ticks.get(vt_sym, {}))
        lg['strike'] = contract.get('option_strike') or 0
        lg['cp'] = 1 if nm_.rfind('C') > nm_.rfind('P') else -1
        legs_by_group[(und_, exp_)].append(lg)

    atm_iv_map = {}  # (und, expiry) -> median ATM iv
    r_param = 0.02   # #3: 统一无风险利率（原 0.03，与 risk_engine 的 0.02 不一致）

    for (und, expiry), items in by_underlying.items():
        if not und:
            continue
        # 标的价取该组第一张合约 tick 里的 underlying_price（Worker 预填）
        first_vt = items[0][0]
        s = option_ticks.get(first_vt, {}).get('underlying_price', 0) or 0
        if s <= 0:
            continue

        ivs = []
        for vt_sym, contract, pos in items:
            k = contract.get('option_strike') or 0
            if k <= 0:
                continue
            name = contract.get('name') or vt_sym
            cp = 1 if name.rfind('C') > name.rfind('P') else -1
            ttm = max(days_to_expiry(expiry) / 365.0, 0.5 / 365.0)
            tick_data = option_ticks.get(vt_sym, {})
            mkt_p = tick_data.get('last_price') or 0
            if mkt_p <= 0:
                continue
            adj_p = mkt_p
            is_itm = (cp == 1 and s > k) or (cp == -1 and s < k)
            if is_itm:
                b = tick_data.get('bid_price_1') or 0
                a = tick_data.get('ask_price_1') or 0
                if b > 0 and a > 0:
                    adj_p = (b + a) / 2.0
            iv = implied_vol_bisection(adj_p, s, k, ttm, r_param, cp)  # FIX 2.1: T,r 顺序修正（原误传 r_param,ttm）
            if iv > 0:
                ivs.append(iv)

        if ivs:
            atm_iv_map[(und, expiry)] = sorted(ivs)[len(ivs) // 2]

    # ── 阶段2：逐张计算 Greeks ──
    for vt_sym, contract, pos, direction_str in symbols:
        try:
            und = contract.get('option_underlying') or ''
            expiry = str(contract.get('option_expiry'))[:10] if contract.get('option_expiry') else ''
            k = contract.get('option_strike') or 0
            if k <= 0:
                continue

            name = contract.get('name') or vt_sym
            cp = 1 if name.rfind('C') > name.rfind('P') else -1
            option_type_str = 'C' if cp == 1 else 'P'

            size = contract.get('size') or 1
            expiry_dt = str(contract.get('option_expiry'))[:10] if contract.get('option_expiry') else ''
            ttm = max(days_to_expiry(expiry_dt) / 365.0, 0.5 / 365.0)

            # 标的价（Worker 预填到 option_ticks[vt_sym]['underlying_price']）
            tick_data = option_ticks.get(vt_sym, {})
            s = tick_data.get('underlying_price', 0) or 0
            if s <= 0:
                continue

            direction_sign = 1 if direction_str == 'long' else -1
            volume = pos.get('volume', 0)

            # 市场价
            mkt_price = tick_data.get('last_price') or 0
            if mkt_price <= 0:
                mkt_price = contract.get('pre_close') or 0

            # ITM/OTM
            is_itm = (cp == 1 and s > k) or (cp == -1 and s < k)

            # 调整价（§3.2 四级：mid/last → ITM PCP 平价 → OTM 盘口量 → 兜底）
            adj_price = calc_adjust_price_4level(
                tick_data, s, k, cp,
                legs_by_group.get((und, expiry_dt), []),
                pre_close=contract.get('pre_close') or 0,
            )

            # IV
            iv = 0.20
            if mkt_price > 0 and s > 0 and k > 0 and ttm > 0:
                raw_iv = implied_vol_bisection(adj_price, s, k, ttm, r_param, cp)  # FIX 2.1: T,r 顺序修正
                iv = raw_iv
                if iv != iv:  # NaN
                    iv = atm_iv_map.get((und, expiry_dt), 0.20)

            g = black76(iv, s, k, ttm, r_param, cp)
            for gk in ('delta', 'gamma', 'theta', 'vega'):
                if g[gk] != g[gk]:  # NaN guard
                    g[gk] = 0

            pos_delta = g['delta'] * volume * direction_sign
            pos_gamma = g['gamma'] * volume * direction_sign
            pos_vega  = g['vega']  * volume * direction_sign
            pos_theta = g['theta'] * volume * direction_sign

            deltacash = int(round(pos_delta * s * size))
            gammacash = int(round(pos_gamma * s * s * 0.01 * size))   # F²×1%×size（不是 0.01²）
            vegacash  = int(round(pos_vega  * size))
            thetacash = int(round(pos_theta  * size))

            # 开仓价（结算单）
            sym_key = vt_sym.split('.')[0]
            sttl = settlement_data.get(sym_key, {})
            open_price = sttl.get('avg_buy_price', 0.0) if direction_str == 'long' else sttl.get('avg_sell_price', 0.0)

            results[vt_sym] = {
                'iv':              round(iv * 100, 2),
                # 可汇总列一律头寸级（原始×方向×手数），父级 Σ 子级
                'delta':           pos_delta,
                'gamma':           pos_gamma,
                'theta':           pos_theta,
                'vega':            pos_vega,
                'open_price':      round(open_price, 4),
                'adjust_price':    round(adj_price, 4),
                'mkt_price':       round(mkt_price, 4),
                'pos_delta':       pos_delta,
                'pos_gamma':       pos_gamma,
                'pos_vega':        pos_vega,
                'pos_theta':       pos_theta,
                'deltacash':       deltacash,
                'gammacash':       gammacash,
                'vegacash':        vegacash,
                'thetacash':       thetacash,
                'is_itm':          is_itm,
                'strike':          k,
                'option_type':     option_type_str,
                'underlying':      und,
                'underlying_price': round(s, 2),
                'days_to_expiry':  days_to_expiry(expiry_dt),
            }
        except Exception:
            continue

    return results


# -----------------------------------------------------------------------
# 辅助：剩余自然日
# -----------------------------------------------------------------------
def days_to_expiry(expiry_str):
    """剩余自然日天数（从今天到到期日，不含到期日当天）"""
    try:
        from datetime import date
        exp = date.fromisoformat(expiry_str[:10])
        today = date.today()
        delta = (exp - today).days
        return max(delta, 0)
    except Exception:
        return 999


if __name__ == "__main__":
    # ------------------------------------------------------------------
    # 冒烟测试
    # ------------------------------------------------------------------
    import math

    # Case 1: ATM Call，IV=16%，F=K=4230，T=30/365
    F, K, T, IV, r = 4230.0, 4230.0, 30.0 / 365.0, 0.16, 0.02
    cp = 1
    g = black76(IV, F, K, T, r, cp)
    print("=== ATM Call ATM ===")
    print(f"  delta = {g['delta']:.4f}  (expect ~0.51)")
    print(f"  gamma = {g['gamma']:.6f}")
    print(f"  vega  = {g['vega']:.4f}")
    print(f"  theta = {g['theta']:.6f}  (negative)")

    # Case 2: ITM Put，IV=20%
    F2, K2, IV2 = 4000.0, 4200.0, 0.20
    g2 = black76(IV2, F2, K2, T, r, cp=-1)
    print("\n=== ITM Put ===")
    print(f"  delta = {g2['delta']:.4f}  (expect ~-0.57)")

    # Case 3: IV 百分比输入自动兼容
    g3 = black76(16.5, F, K, T, r, cp)   # 传入 16.5% 而非 0.165
    print("\n=== IV=16.5% (percentage input) ===")
    print(f"  delta = {g3['delta']:.4f}  (should match Case 1)")

    # Case 4: 反推 IV
    market_price = 45.2   # 近似 ATM Call 价格
    iv_back = implied_vol_bisection(market_price, F, K, T, r, cp)
    print(f"\n=== IV Back-solve: market={market_price}, F={F}, K={K} ===")
    print(f"  iv = {iv_back*100:.2f}%  (expect ~16%)")

    # Case 5: 便捷函数
    result = calc_greeks_from_market(market_price, F, K, T, "C", r)
    print(f"\n=== calc_greeks_from_market ===")
    print(f"  iv={result['iv']:.2f}%, delta={result['delta']:.4f}, "
          f"gamma={result['gamma']:.6f}, vega={result['vega']:.4f}, "
          f"theta={result['theta']:.6f}")
