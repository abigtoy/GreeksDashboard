# alert_config.py — 监控预警配置层（阈值 + σ_ref 基准 IV）
#
# 职责：
#   1. 风控阈值读写（config/alert_settings.json）—— 看板设置菜单实时可调
#   2. σ_ref 基准 IV 读写（config/iv_sigma_ref.csv）—— 用户手工填，系统自动补空缺行
#   3. σ_ref 弹窗"忽略一次"账本（config/sigma_ref_ack.json）—— 按月免打扰
#   4. Margin 风险度分档（95% / 110% 两级，仅提示不阻断）
#
# 设计口径（对齐 监控预警_设计稿 v0.2）：
#   - 阈值初期全部走经验值起步，便于观察微调，不做落盘统计标定
#   - 数据采集职责不在本模块（等通盘考虑）
#   - 本模块不 import api_server，避免循环依赖

import csv
import io
import os
import re
from datetime import datetime

from loguru import logger

# ── 路径 ────────────────────────────────────────────────────────────────────
_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_DIR = os.path.join(_PARENT, "config")

SETTINGS_FILE = os.path.join(CONFIG_DIR, "alert_settings.json")
SIGMA_CSV     = os.path.join(CONFIG_DIR, "iv_sigma_ref.csv")
SIGMA_ACK     = os.path.join(CONFIG_DIR, "sigma_ref_ack.json")

# 默认 σ_ref（找不到任何记录时的最终兜底）
SIGMA_DEFAULT = 0.25

# ── 阈值默认值（经验值起步）+ 允许调节范围（防手滑）────────────────────────
#   lo/hi 为闭区间；step 供前端滑杆/输入框用
THRESHOLD_SPEC = {
    "f_rate_warn":        {"lo": 0.10, "hi": 1.00, "step": 0.05, "label": "F 速率·黄（ΔlnF/σ√T）"},
    "f_rate_danger":      {"lo": 0.15, "hi": 1.50, "step": 0.05, "label": "F 速率·红（ΔlnF/σ√T）"},
    "iv_rate_warn":       {"lo": 0.50, "hi": 5.00, "step": 0.10, "label": "ATM IV 速率·黄（vol/5min）"},
    "net_delta_warn":     {"lo": 1,    "hi": 50,   "step": 1,    "label": "品种净 Δ 限额（手）"},
    "burn_warn":          {"lo": 0.05, "hi": 0.60, "step": 0.05, "label": "Premium Burn·黄"},
    "burn_danger":        {"lo": 0.10, "hi": 1.00, "step": 0.05, "label": "Premium Burn·红"},
    "margin_ratio_warn":  {"lo": 50,   "hi": 100,  "step": 1,    "label": "风险度·黄（%）"},
    "margin_ratio_danger": {"lo": 60,  "hi": 200,  "step": 1,    "label": "风险度·红（%）"},
}

DEFAULT_SETTINGS = {
    "f_rate_warn":         0.30,
    "f_rate_danger":       0.50,
    "iv_rate_warn":        1.50,
    "net_delta_warn":      10,
    "burn_warn":           0.20,
    "burn_danger":         0.50,
    "margin_ratio_warn":   95,
    "margin_ratio_danger": 110,
}

# ── 模块内缓存 ───────────────────────────────────────────────────────────────
_settings_cache: dict | None = None
_sigma_cache: dict[str, float] = {}
_sigma_mtime: float = 0.0


def _clamp_clip(key: str, val: float) -> float:
    """把值夹进 THRESHOLD_SPEC 允许区间。"""
    spec = THRESHOLD_SPEC.get(key)
    if not spec:
        return val
    lo, hi = spec["lo"], spec["hi"]
    clipped = max(lo, min(hi, val))
    if clipped != val:
        logger.warning(f"[alert_config] {key}={val} 超出区间 [{lo},{hi}]，夹为 {clipped}")
    return clipped


# ══════════════════════════════════════════════════════════════════════════
# 1) 阈值设置
# ══════════════════════════════════════════════════════════════════════════
def load_alert_settings(force: bool = False) -> dict:
    """
    读取阈值配置。文件不存在 / 解析失败 → 返回默认经验值。
    返回：{key: value}，键与 THRESHOLD_SPEC 对齐。
    """
    global _settings_cache
    if _settings_cache is not None and not force:
        return dict(_settings_cache)

    merged = dict(DEFAULT_SETTINGS)
    try:
        if os.path.exists(SETTINGS_FILE):
            import json
            with open(SETTINGS_FILE, encoding="utf-8") as f:
                raw = json.load(f)
            for k, v in (raw or {}).items():
                if k in merged and isinstance(v, (int, float)):
                    merged[k] = _clamp_clip(k, float(v))
    except Exception as e:
        logger.error(f"[alert_config] 阈值配置读取失败，用默认经验值: {e}")

    _settings_cache = merged
    return dict(merged)


def save_alert_settings(patch: dict) -> dict:
    """
    部分更新（前端设置菜单提交）：只接受 THRESHOLD_SPEC 已知键，夹区间后落盘。
    返回落盘后的完整配置。
    """
    global _settings_cache
    cur = load_alert_settings()
    changed = []
    for k, v in (patch or {}).items():
        if k not in DEFAULT_SETTINGS:
            logger.warning(f"[alert_config] 忽略未知阈值键 {k}")
            continue
        try:
            fv = float(v)
        except (TypeError, ValueError):
            logger.warning(f"[alert_config] {k} 值非数字，忽略: {v!r}")
            continue
        # 单调性保护：黄线不得高于红线
        if k.endswith("_warn") and k.replace("_warn", "_danger") in cur:
            danger_key = k.replace("_warn", "_danger")
            if fv > cur[danger_key] and danger_key not in (patch or {}):
                logger.warning(f"[alert_config] {k}={fv} > {danger_key}={cur[danger_key]}，拒收")
                continue
        if k.endswith("_danger") and k.replace("_danger", "_warn") in cur:
            warn_key = k.replace("_danger", "_warn")
            new_warn = (patch or {}).get(warn_key, cur[warn_key])
            try:
                new_warn = float(new_warn)
            except (TypeError, ValueError):
                new_warn = cur[warn_key]
            if fv < new_warn:
                logger.warning(f"[alert_config] {k}={fv} < {warn_key}={new_warn}，拒收")
                continue
        fv = _clamp_clip(k, fv)
        if cur[k] != fv:
            changed.append(k)
        cur[k] = fv

    try:
        import json
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(cur, f, ensure_ascii=False, indent=2)
        _settings_cache = cur
        logger.info(f"[alert_config] 阈值已保存，变更键: {changed or '无'}")
    except Exception as e:
        logger.error(f"[alert_config] 阈值落盘失败（内存值仍生效）: {e}")

    return dict(cur)


def get_threshold(key: str, default=None):
    """单个阈值读取（供触发器热路径调用，走缓存）。"""
    if _settings_cache is None:
        load_alert_settings()
    v = _settings_cache.get(key)
    return v if v is not None else (default if default is not None else DEFAULT_SETTINGS.get(key))


# ══════════════════════════════════════════════════════════════════════════
# 2) σ_ref 基准 IV
# ══════════════════════════════════════════════════════════════════════════
def _norm_symbol(s: str) -> str:
    """品种码规范化：去空格、转大写。IF/if → IF。"""
    return (s or "").strip().upper()


def load_sigma_ref(force: bool = False) -> dict[str, float]:
    """
    读取 iv_sigma_ref.csv → {PRODUCT: sigma}（sigma 存小数，0.30 而非 30）。
    - 跳过空行 / # 注释行 / 值缺失或非法的行
    - 文件 mtime 变化时自动重载（用户手工改完 CSV 不用重启服务）
    """
    global _sigma_cache, _sigma_mtime
    try:
        mtime = os.path.getmtime(SIGMA_CSV) if os.path.exists(SIGMA_CSV) else 0.0
    except OSError:
        mtime = 0.0

    if not force and mtime == _sigma_mtime and _settings_cache is not None:
        return dict(_sigma_cache)

    out: dict[str, float] = {}
    bad_rows: list[str] = []
    try:
        if os.path.exists(SIGMA_CSV):
            with open(SIGMA_CSV, encoding="utf-8-sig", newline="") as f:
                for i, row in enumerate(csv.reader(f)):
                    if not row or not any((c or "").strip() for c in row):
                        continue
                    head = (row[0] or "").strip()
                    if head.startswith("#") or head.lower() == "symbol":
                        continue
                    if len(row) < 2:
                        bad_rows.append(f"L{i+1}: 列数不足")
                        continue
                    sym = _norm_symbol(head)
                    val_raw = (row[1] or "").strip()
                    if not val_raw:
                        # 空值 = 该品种占位行（用户还没填），不进字典，由 missing 列表提示
                        continue
                    try:
                        v = float(val_raw)
                    except ValueError:
                        bad_rows.append(f"L{i+1}: {sym} 值非法 {val_raw!r}")
                        continue
                    if v > 1.5:            # 用户填了 30 而不是 0.30 → 自动换算
                        v = v / 100.0
                        bad_rows.append(f"L{i+1}: {sym} 按百分比输入，已换算为 {v:.4f}")
                    if not (0.02 <= v <= 1.50):
                        bad_rows.append(f"L{i+1}: {sym}={v} 超出 [2%,150%]，忽略")
                        continue
                    out[sym] = v
    except Exception as e:
        logger.error(f"[alert_config] σ_ref CSV 读取失败: {e}")

    if bad_rows:
        logger.warning(f"[alert_config] σ_ref 数据行问题 {len(bad_rows)} 条: {bad_rows[:5]}")

    _sigma_cache = out
    _sigma_mtime = mtime
    return dict(out)


def get_sigma(product: str) -> tuple[float, str]:
    """
    取某品种基准 IV。返回 (sigma, source)。
    source: 'config'（用户填的） | 'default'（兜底 0.25）
    降级链：品种精确匹配 → 默认值（单值不分月，无月内退阶）
    """
    table = load_sigma_ref()
    key = _norm_symbol(product)
    if key in table:
        return table[key], "config"
    return SIGMA_DEFAULT, "default"


def sigma_ref_pending(held_products: list[str]) -> list[dict]:
    """
    列出「有持仓但 σ_ref 未填」的品种（弹窗只提示这些，全月无持仓不打扰）。
    返回按品种名排序的 [{symbol, sigma_ref, effective, source}]
    """
    table = load_sigma_ref()
    rows = []
    for p in sorted({_norm_symbol(x) for x in held_products if x}):
        has = p in table
        rows.append({
            "symbol":    p,
            "sigma_ref": table.get(p),                 # None = 待填
            "effective": table.get(p, SIGMA_DEFAULT),
            "source":    "config" if has else "default",
        })
    return rows


def sigma_ref_missing(held_products: list[str]) -> list[str]:
    """待填品种列表（已扣除「忽略一次」的）。"""
    acked = set(load_sigma_ack())
    return [r["symbol"] for r in sigma_ref_pending(held_products)
            if r["source"] == "default" and r["symbol"] not in acked]


def append_sigma_symbols(products: list[str]) -> int:
    """
    把「有持仓但 CSV 里没有行」的品种自动追加成空值占位行，用户只需填数字。
    保留原文件已有内容与注释（逐行原样重写 + 追加）。
    返回新追加的行数。
    """
    table = load_sigma_ref()
    have = set(table.keys())
    # CSV 里已有行（含空值占位）也要认，避免重复追加
    try:
        if os.path.exists(SIGMA_CSV):
            with open(SIGMA_CSV, encoding="utf-8-sig", newline="") as f:
                for row in csv.reader(f):
                    if row and row[0].strip() and not row[0].strip().startswith("#") \
                       and _norm_symbol(row[0]) != "SYMBOL":
                        have.add(_norm_symbol(row[0]))
    except Exception:
        pass

    todo = [_norm_symbol(p) for p in sorted(set(products)) if _norm_symbol(p) and _norm_symbol(p) not in have]
    todo = [t for t in todo if t]
    if not todo:
        return 0

    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        exists = os.path.exists(SIGMA_CSV)
        with open(SIGMA_CSV, "a", encoding="utf-8", newline="") as f:
            if not exists:
                f.write("symbol,sigma_ref\n")
            else:
                # 末行无换行时补一个，避免粘连
                with open(SIGMA_CSV, "rb") as rf:
                    rf.seek(0, io.SEEK_END)
                    if rf.tell() > 0:
                        rf.seek(-1, io.SEEK_END)
                        if rf.read(1) not in (b"\n", b"\r"):
                            f.write("\n")
            for sym in todo:
                f.write(f"{sym},\n")
        logger.info(f"[alert_config] σ_ref 自动补占位行: {todo}")
        load_sigma_ref(force=True)
        return len(todo)
    except Exception as e:
        logger.error(f"[alert_config] σ_ref 追加占位行失败: {e}")
        return 0


# ══════════════════════════════════════════════════════════════════════════
# 3) σ_ref 弹窗「忽略一次」账本（按月免打扰）
# ══════════════════════════════════════════════════════════════════════════
def current_month_key(now: datetime | None = None) -> str:
    """YYYYMM。跨月自动失效（新月份重新提示）。"""
    return (now or datetime.now()).strftime("%Y%m")


def load_sigma_ack(month: str | None = None) -> list[str]:
    """返回本月已被「忽略一次」的品种列表。"""
    month = month or current_month_key()
    try:
        if os.path.exists(SIGMA_ACK):
            import json
            with open(SIGMA_ACK, encoding="utf-8") as f:
                data = json.load(f)
            return [_norm_symbol(s) for s in (data.get(month) or [])]
    except Exception as e:
        logger.error(f"[alert_config] σ_ref ack 读取失败: {e}")
    return []


def save_sigma_ack(symbols: list[str], month: str | None = None) -> list[str]:
    """
    追加「忽略一次」品种（幂等，只增不减；跨月由 month 键天然隔离）。
    返回该月当前已忽略的全量列表。
    """
    global _sigma_cache
    month = month or current_month_key()
    data = {}
    try:
        if os.path.exists(SIGMA_ACK):
            import json
            with open(SIGMA_ACK, encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                data = {}
    except Exception:
        data = {}

    cur = {_norm_symbol(s) for s in (data.get(month) or [])}
    cur |= {_norm_symbol(s) for s in symbols if _norm_symbol(s)}
    data[month] = sorted(cur)

    # 只保留最近 3 个月，防止无限膨胀
    keys = sorted(data.keys())
    for k in keys[:-3]:
        data.pop(k, None)

    try:
        import json
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(SIGMA_ACK, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"[alert_config] σ_ref ack 落盘失败: {e}")
    return data[month]


# ══════════════════════════════════════════════════════════════════════════
# 4) Margin 风险度分档（95% / 110% 两级，仅提示不阻断）
# ══════════════════════════════════════════════════════════════════════════
def margin_status(margin: float, balance: float) -> dict:
    """
    风险度 = 占用保证金 / 权益（期货公司口径）。
    两级：warn=95%、danger=110%（可在设置菜单调）。
    返回 {ratio_pct, level, headroom}
      level: 'ok' | 'warn' | 'danger'
      headroom: 距红线还有多少权益（负数=已超）
    """
    warn = get_threshold("margin_ratio_warn")
    danger = get_threshold("margin_ratio_danger")
    if not balance or balance <= 0:
        return {"ratio_pct": None, "level": "unknown", "headroom": None}
    ratio = margin / balance * 100.0
    if ratio >= danger:
        level = "danger"
    elif ratio >= warn:
        level = "warn"
    else:
        level = "ok"
    headroom = balance * danger / 100.0 - margin
    return {"ratio_pct": round(ratio, 2), "level": level, "headroom": round(headroom, 2)}
