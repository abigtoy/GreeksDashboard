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
def black76_price(IV: float, F: float, K: float, T: float,
                  r: float = 0.02, cp: int = 1) -> float:
    """
    Black-76 理论价（IV 为小数，不做百分比自动转换——单位由调用方保证）。
    black76 的反方向：已知 IV 求价格。
    """
    if F <= 0 or K <= 0 or T <= 0 or IV <= 0:
        return 0.0
    v = max(IV, 0.001)
    sqrtT = math.sqrt(T)
    d1 = (math.log(F / K) + 0.5 * v * v * T) / (v * sqrtT)
    d2 = d1 - v * sqrtT
    return cp * math.exp(-r * T) * (F * _norm_cdf(cp * d1) - K * _norm_cdf(cp * d2))


def implied_vol_bisection(price: float, F: float, K: float, T: float,
                          r: float = 0.02, cp: int = 1,
                          tol: float = 0.0001, strict: bool = False) -> float:
    """
    二分法反推隐含波动率。
    price: 期权市场价
    F/K/T: 标的价/行权价/到期时间(年)
    cp:    1=Call, -1=Put
    返回: IV 小数形式（如 0.165）
          strict=False（旧行为）：收敛失败兜底返回 0.20
          strict=True：无解返回 NaN，由调用方决定降级（不静默造 20%）
    """
    if price <= 0 or F <= 0 or K <= 0:
        return float('nan') if strict else 0.20

    v_low, v_high = 0.001, 5.0
    if strict and not (black76_price(v_low, F, K, T, r, cp) <= price
                       <= black76_price(v_high, F, K, T, r, cp)):
        return float('nan')   # 价格落在 [0.1%, 500%] IV 可及区间之外 → 无解
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
# 3.2 adjust_price 阶梯（移植自旧版 vnpy_api_server，PCP 平价 + 盘口过滤）
#   1 正常：盘口内 + 紧价差 → last
#   2 极度虚值：ask < 标的×0.1% → 取挂单量大的一侧
#   3 深度实值 ITM：同 K 反方向 OTM 腿时间价值 + 本腿内在价值（PCP 平价）
#   4 盘口不可用（价差过宽 / 单边挂价）→ 参考 IV 模型价夹进盘口
#   5 兜底：last → pre_close（在 calc_adjust_price_4level 内）
#   陈旧行情（_is_stale）整条作废，盘口不再当证据
# -----------------------------------------------------------------------
_STRIKE_DIFF_THRESHOLD = 0.001   # 极度虚值：ask < 标的 × 0.1%
_SPREAD_TIGHT_THRESHOLD = 0.20   # 紧价差阈值 20%
_STALE_MINUTES = 3               # 行情超过此分钟数视为过期
_R_PARAM = 0.02                  # 统一无风险利率
_REF_IV_MIN, _REF_IV_MAX = 0.05, 2.0   # 参考 IV 合理区间（反推结果过滤）
_REF_IV_DEFAULT = 0.20           # 参考 IV 缺失时的默认值

def _pick_ref_iv(legs, F, r=_R_PARAM):
    """
    参考 IV：同「标的+到期」组内 put(K<F 最大) / call(K>F 最小) 两张平值腿的中位数。
    legs: [{strike, cp, last_price, ttm}]，只收 last>0 且反推收敛在 [_REF_IV_MIN,_REF_IV_MAX] 内的。
    两张都不可用 → None（调用方用 _REF_IV_DEFAULT）。
    """
    if F <= 0:
        return None
    put_k = max((lg['strike'] for lg in legs
                 if lg['cp'] == -1 and 0 < lg['strike'] < F), default=None)
    call_k = min((lg['strike'] for lg in legs
                  if lg['cp'] == 1 and lg['strike'] > F), default=None)
    want = {(-1, put_k), (1, call_k)}
    ivs = []
    for lg in legs:
        if (lg['cp'], lg['strike']) not in want:
            continue
        p = lg.get('last_price') or 0
        if p <= 0:
            continue
        ttm = max(lg.get('ttm') or 0.0, 0.5 / 365.0)
        iv = implied_vol_bisection(p, F, lg['strike'], ttm, r, lg['cp'], strict=True)
        if iv == iv and _REF_IV_MIN <= iv <= _REF_IV_MAX:
            ivs.append(iv)
    if not ivs:
        return None
    return sorted(ivs)[len(ivs) // 2]

def _mid(b, a):
    return (b + a) / 2.0

def _intrinsic(s, k, cp):
    return max(s - k, 0.0) if cp == 1 else max(k - s, 0.0)

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

def _live(leg):
    """取 last/bid/ask。陈旧行情整条作废（旧版只清 last，陈旧盘口仍被当证据）。"""
    if _is_stale(leg.get('datetime')):
        return 0.0, 0.0, 0.0
    n = leg.get('last_price') or 0.0
    b = leg.get('bid_price_1') or 0.0
    a = leg.get('ask_price_1') or 0.0
    return n, b, a

def _model_in_bracket(leg, s, ref_iv, b, a):
    """阶梯4：参考 IV 模型价夹进盘口（双边=区间，单边=该侧界）。缺参考 IV 用默认 20%。"""
    iv = ref_iv if ref_iv else _REF_IV_DEFAULT
    tag = 'ref_iv' if ref_iv else 'ref_iv_default'
    p = black76_price(iv, s, leg.get('strike') or 0, leg.get('ttm') or 0,
                      _R_PARAM, leg.get('cp') or 1)
    if p <= 0:
        return (_mid(b, a) if (b > 0 and a > 0) else (b or a)), 'no_quote'
    if b > 0 and p < b:
        return b, tag + '_bid'
    if a > 0 and p > a:
        return a, tag + '_ask'
    return p, tag

def _adjust_otm(leg, s, ref_iv=None):
    """阶梯 1/2/4（OTM 或正常腿）。返回 (价, basis)；价 0 = 无可用价，交调用方兜底。"""
    n, b, a = _live(leg)
    if b <= 0 and a <= 0:
        return (n, 'last') if n > 0 else (0.0, 'none')
    if b <= 0 or a <= 0:
        return _model_in_bracket(leg, s, ref_iv, b, a)   # 单边挂价不成价，只当界
    bv = leg.get('bid_volume_1') or 0.0
    av = leg.get('ask_volume_1') or 0.0
    # 极度虚值：不看价差，取挂单量大的一侧
    if a < s * _STRIKE_DIFF_THRESHOLD:
        return (b if bv >= av else a), 'otm_side'
    spread = abs(a - b)
    mid = _mid(b, a)
    sr = spread / mid if mid > 0 else 1.0
    # 盘口内 + 紧价差 → 用最新价
    if n > 0 and b <= n <= a and sr <= _SPREAD_TIGHT_THRESHOLD:
        return n, 'last'
    # 价差过宽：旧版退中间价，改为参考 IV 模型价夹进盘口
    return _model_in_bracket(leg, s, ref_iv, b, a)

def _adjust_itm(leg, s, k, cp, all_legs, ref_iv=None):
    """阶梯3：深度实值 PCP 平价。OTM 腿价格嵌套 _adjust_otm。缺对手腿 → 参考 IV 模型价。"""
    _, b, a = _live(leg)
    otm = next((t for t in all_legs
                if t.get('cp') == -cp and abs((t.get('strike') or 0) - k) < 0.01), None)
    if otm is None:
        return _model_in_bracket(leg, s, ref_iv, b, a)
    otm_k = otm.get('strike') or k
    otm_p, _ = _adjust_otm(otm, s, ref_iv)
    otm_tv = max(otm_p - _intrinsic(s, otm_k, otm.get('cp')), 0.0)
    adj = _intrinsic(s, k, cp) + otm_tv
    if b > 0 and a > 0:
        return (b if abs(adj - b) < abs(adj - a) else a), 'pcp'
    if b > 0 and adj < b:
        return b, 'pcp_bid'
    if a > 0 and adj > a:
        return a, 'pcp_ask'
    return adj, 'pcp'

def calc_adjust_price_4level(leg, s, k, cp, all_legs, pre_close=0.0, ref_iv=None):
    """调整价入口（§3.2 阶梯）+ 兜底 last → pre_close → 0。返回 (价, basis)。"""
    is_itm = (cp == 1 and s > k) or (cp == -1 and s < k)
    adj, basis = (_adjust_itm(leg, s, k, cp, all_legs, ref_iv) if is_itm
                  else _adjust_otm(leg, s, ref_iv))
    if adj and adj > 0:
        return adj, basis
    n, _, _ = _live(leg)
    if n > 0:
        return n, 'last'
    return (pre_close or 0.0), 'pre_close'


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
def price_options_batch(symbols, option_ticks, settlement_data, ref_legs=None):
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
        ref_legs:        可选，{(und, expiry): [{strike, cp, last_price, ttm}]}
                         参考 IV 的腿池（可含未持仓的平值腿）；缺省用持仓腿

    返回:
        {vt_symbol: {iv, delta, gamma, theta, vega, open_price, adjust_price,
                     price_basis, ...}}
    """
    from collections import defaultdict
    results = {}

    # ── 阶段1：按「标的+到期」提炼参考 IV（平值 put/call 中位数） ──
    by_underlying = defaultdict(list)
    for vt_sym, contract, pos, _ in symbols:
        und = contract.get('option_underlying') or ''
        expiry = str(contract.get('option_expiry'))[:10] if contract.get('option_expiry') else ''
        by_underlying[(und, expiry)].append((vt_sym, contract, pos))

    # 同「标的+到期」腿集合：供阶梯3 ITM PCP 查对手 OTM 腿（含盘口量/时间戳）
    legs_by_group = defaultdict(list)
    for vt_sym, contract, pos, _ in symbols:
        und_ = contract.get('option_underlying') or ''
        exp_ = str(contract.get('option_expiry'))[:10] if contract.get('option_expiry') else ''
        nm_ = contract.get('name') or vt_sym
        lg = dict(option_ticks.get(vt_sym, {}))
        lg['strike'] = contract.get('option_strike') or 0
        lg['cp'] = 1 if nm_.rfind('C') > nm_.rfind('P') else -1
        lg['ttm'] = max(days_to_expiry(exp_) / 365.0, 0.5 / 365.0)
        legs_by_group[(und_, exp_)].append(lg)

    ref_iv_map = {}  # (und, expiry) -> 参考 IV（小数）或 None
    for (und, expiry) in by_underlying:
        if not und:
            continue
        s0 = option_ticks.get(by_underlying[(und, expiry)][0][0], {}).get('underlying_price', 0) or 0
        if s0 <= 0:
            continue
        # 参考腿池：优先用调用方给的 ref_legs（含未持仓的平值腿），否则退持仓腿
        pool = (ref_legs or {}).get((und, expiry)) or legs_by_group[(und, expiry)]
        ref_iv_map[(und, expiry)] = _pick_ref_iv(pool, s0)

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

            # _model_in_bracket 需要这三个字段定位模型价
            tick_data['strike'], tick_data['cp'], tick_data['ttm'] = k, cp, ttm

            # 调整价（§3.2 阶梯：last → 极度虚值 → ITM PCP → 参考 IV 夹盘口 → 兜底）
            ref_iv = ref_iv_map.get((und, expiry_dt))
            adj_price, price_basis = calc_adjust_price_4level(
                tick_data, s, k, cp,
                legs_by_group.get((und, expiry_dt), []),
                pre_close=contract.get('pre_close') or 0,
                ref_iv=ref_iv,
            )

            # IV：价来自市场证据时反推；反推无解/出界/只有昨收 → 退参考 IV → 默认 20%
            iv = ref_iv or _REF_IV_DEFAULT
            if (mkt_price > 0 and price_basis != 'pre_close'
                    and s > 0 and k > 0 and ttm > 0):
                raw_iv = implied_vol_bisection(adj_price, s, k, ttm, _R_PARAM, cp, strict=True)
                if raw_iv == raw_iv and _REF_IV_MIN <= raw_iv <= _REF_IV_MAX:
                    iv = raw_iv

            g = black76(iv, s, k, ttm, _R_PARAM, cp)
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
                'price_basis':     price_basis,
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

    # ------------------------------------------------------------------
    # 自测：阶梯规则（参考 IV 模型价夹盘口）
    # ------------------------------------------------------------------
    print("\n=== 自测：阶梯 1-5 ===")
    from datetime import datetime as dt
    _T = 0.08   # ≈29 自然日；ATM@30% 模型价 ≈75
    # Test A: bid=50/ask=100, 无成交 → 模型价 75 ∈ 区间 → 取模型价（非 mid 75? 见下）
    legA = {'last_price': 0.0, 'bid_price_1': 50.0, 'ask_price_1': 100.0,
            'datetime': dt.now(), 'strike': 2200.0, 'cp': -1, 'ttm': _T}
    ref_iv_A = 0.30
    adjA, basisA = _adjust_otm(legA, s=2200.0, ref_iv=ref_iv_A)
    assert basisA == 'ref_iv' and 50 < adjA < 100, f"TestA failed: {adjA}/{basisA}"
    print(f"  无成交 bid50/ask100, ref_iv=30% → mark={adjA:.2f} (basis={basisA}) ✓")
    # Test B: 单边 bid=65（旧版直接取 65）→ 65 只当下界，取模型价
    legB = {'last_price': 0.0, 'bid_price_1': 65.0, 'ask_price_1': 0.0,
            'datetime': dt.now(), 'strike': 2200.0, 'cp': 1, 'ttm': _T}
    adjB, basisB = _adjust_otm(legB, s=2200.0, ref_iv=ref_iv_A)
    assert basisB == 'ref_iv' and adjB != 65.0, f"TestB failed: {adjB}/{basisB}"
    print(f"  单边 bid=65, ref_iv=30% → mark={adjB:.2f} (basis={basisB}) ✓ 不再取 65")
    # Test B2: 模型价低于唯一买价时夹回 bid（65 当下界成立）
    legB2 = dict(legB, **{'ttm': 0.001})
    adjB2, basisB2 = _adjust_otm(legB2, s=2200.0, ref_iv=0.01)
    assert basisB2 == 'ref_iv_bid' and adjB2 == 65.0, f"TestB2 failed: {adjB2}/{basisB2}"
    print(f"  低 IV 模型价<bid → 夹回 65 (basis={basisB2}) ✓")
    # Test C: 陈旧行情 → 整条作废 → pre_close
    legC = {'last_price': 999.0, 'bid_price_1': 900.0, 'ask_price_1': 800.0,
            'datetime': dt(2020, 1, 1), 'strike': 2200.0, 'cp': -1, 'ttm': _T}
    adjC, basisC = calc_adjust_price_4level(legC, s=2200.0, k=2200.0, cp=-1,
                                            all_legs=[], pre_close=471.59, ref_iv=None)
    assert basisC == 'pre_close' and adjC == 471.59, f"TestC failed: {adjC}/{basisC}"
    print(f"  Stale → pre_close=471.59 (basis={basisC}) ✓")
    # Test D: black76_price <-> implied_vol_bisection 往返
    p = black76_price(0.16, 4230.0, 4230.0, 30.0 / 365.0)
    iv_back = implied_vol_bisection(p, 4230.0, 4230.0, 30.0 / 365.0, strict=True)
    assert abs(iv_back - 0.16) < 0.01, f"Round-trip failed: {iv_back*100:.2f}% vs 16%"
    print(f"  Round-trip 16% → price → {iv_back*100:.2f}% ✓")
    # Test E: 紧价差 + 盘口内 last → last
    legE = {'last_price': 55.0, 'bid_price_1': 50.0, 'ask_price_1': 60.0,
            'datetime': dt.now(), 'strike': 2200.0, 'cp': -1, 'ttm': _T,
            'bid_volume_1': 10, 'ask_volume_1': 10}
    adjE, basisE = _adjust_otm(legE, s=2200.0, ref_iv=0.20)
    assert basisE == 'last' and abs(adjE - 55.0) < 0.01, f"TestE failed: {adjE}/{basisE}"
    print(f"  Last=55∈[50,60]+spread≤20% → mark={adjE:.2f} (basis={basisE}) ✓")
    # Test F: 参考 IV 提炼 —— put 取 K<F 最大、call 取 K>F 最小，中间的腿不参与
    pool = [{'strike': 2000.0, 'cp': -1, 'last_price': 90.0, 'ttm': _T},     # 更虚 value 的 put，不参与
            {'strike': 2150.0, 'cp': -1, 'last_price': 62.0, 'ttm': _T},     # K<F 最大 put ✓
            {'strike': 2250.0, 'cp': 1, 'last_price': 58.0, 'ttm': _T},      # K>F 最小 call ✓
            {'strike': 2400.0, 'cp': 1, 'last_price': 20.0, 'ttm': _T}]      # 更远 call，不参与
    refF = _pick_ref_iv(pool, 2200.0)
    assert refF and _REF_IV_MIN <= refF <= _REF_IV_MAX, f"TestF failed: {refF}"
    assert abs(implied_vol_bisection(62.0, 2200.0, 2150.0, _T, cp=-1) - refF) < 1e-6, "TestF 取值非中位数?"
    print(f"  平值池 4 腿 → ref_iv={refF*100:.2f}% (取 put2150/call2250 中位数) ✓")
    # Test G: 平值腿全无成交 → None → 调用方退 20%
    assert _pick_ref_iv([dict(x, last_price=0.0) for x in pool], 2200.0) is None, "TestG failed"
    adjG, basisG = _adjust_otm(dict(legA, last_price=0.0), 2200.0, ref_iv=None)
    assert basisG.startswith('ref_iv_default'), f"TestG failed: {basisG}"
    print(f"  无平值成交 → ref_iv=None → 默认 20% 模型价 mark={adjG:.2f} (basis={basisG}) ✓")
    print("\n✓ Self-check passed (all tests OK)\n")
