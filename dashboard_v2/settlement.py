# settlement.py — 结算单双策略加载 VWAP + SettlementManager 增量同步
#
# 职责分割:
#   load_settlement_cost() / load_settlement_sync() — 纯函数，Greeks 计算调用
#   SettlementManager — 状态管理器，负责增量下载 + 日期标记
#
# 日期边界规则:
#   当前时间 < 20:00 → 最新有效结算单 = 昨日
#   当前时间 >= 20:00 → 最新有效结算单 = 今日

import json
import glob
import os
import pathlib
from datetime import date, datetime, timedelta
from collections import defaultdict
from loguru import logger

# -----------------------------------------------------------------------
# 方向映射表（兼容多种字段命名）
# -----------------------------------------------------------------------
_LONG_DIRS  = {'买', '多', 'B', '1', 'Buy', 'L', 'Long', 'long'}
_SHORT_DIRS = {'卖', '空', 'S', '-1', 'Sell', 'S', 'Short', 'short'}

def _parse_direction(raw: str) -> str | None:
    """
    方向解析。返回 'long'、'short' 或 None（未知方向，拒绝入账）。
    """
    if not raw:
        return None
    s = str(raw).strip()
    if s in _LONG_DIRS:
        return 'long'
    if s in _SHORT_DIRS:
        return 'short'
    return None

# -----------------------------------------------------------------------
# 策略1：嗅探券商预计算汇总均价
# -----------------------------------------------------------------------
# ---------------------------------------------------------------------------
# 净仓聚合辅助函数
# ---------------------------------------------------------------------------
def _net_aggregate(details: list, price_field: str) -> dict[str, float]:
    """
    按合约聚合净仓价格。
    net_vol  = Σ多vol - Σ空vol          （可正可负）
    net_price = (Σ多vol×多均价 - Σ空vol×空均价) / net_vol
    key = symbol（不带方向后缀）
    返回: { "IC2612": +7434.44, "IC2609": -6800.0, ... }
    """
    agg: dict[str, dict[str, float]] = defaultdict(lambda: {"long_vol": 0, "short_vol": 0, "long_cost": 0.0, "short_cost": 0.0})
    for item in details:
        sym = item.get('instrument', '')
        if not sym:
            continue
        # 方向来源：优先用 direction 字段（Dashboard schema），降级用 bs 字段（vnpy schema）
        raw_dir = item.get('direction', '') or item.get('bs', '')
        direction = _parse_direction(str(raw_dir))
        if direction is None:
            continue                      # 未知方向拒绝入账
        # 结算价字段两名并存：settl_price（vnpy schema）/ settlement_price（Dashboard schema）
        price = float(item.get(price_field) or item.get('settlement_price') or 0)
        vol = int(item.get('volume') or item.get('position') or 0)
        if price <= 0 or vol <= 0:
            continue
        a = agg[sym]
        if direction == 'long':
            a["long_cost"] += price * vol
            a["long_vol"] += vol
        else:
            a["short_cost"] += price * vol
            a["short_vol"] += vol

    result = {}
    for sym, a in agg.items():
        net_vol = a["long_vol"] - a["short_vol"]
        if net_vol == 0:
            continue
        net_price = (a["long_cost"] - a["short_cost"]) / net_vol
        result[sym] = round(net_price, 4)
    return result


# --------------------------------------------------------------------------
# 开仓均价 — 双策略（汇总优先，明细兜底）+ 方向后缀 key
# --------------------------------------------------------------------------
def load_settlement_cost(settlement_json: dict) -> dict:
    """
    净仓聚合开仓均价，key 带方向后缀 {sym}_{多|空}。
    返回: { "IC2612_多": 7434.44, "IC2609_空": 8048.6, ... }

    实际数据格式（CTP 结算单）：
      - long_pos / avg_buy  → 多仓均价
      - short_pos / avg_sell → 空仓均价
      - bs（账户类别：交易/投机/保值）非方向字段
      - 无 positions / positions_summary 预汇总

    双策略优先级：
      策略1（汇总）：positions / positions_summary 中的预计算均价（优先）
      策略2（明细）：long_pos×avg_buy + short_pos×avg_sell 净仓自算（兜底）

    差异告警：同一合约汇总 vs 明细价格差异超过 ±5% → warning 日志
    未知方向拒绝入账：无法映射到'多'/'空'时拒绝，日志记录
    """
    settlement_dict: dict[str, float] = {}
    detail_calc: dict[str, dict[str, float]] = defaultdict(lambda: {"total_cost": 0.0, "total_vol": 0})

    # positions_detail 逐笔明细 VWAP 自算（唯一路径）
    details = settlement_json.get('positions_detail') or []
    for item in details:
        sym = (item.get('instrument') or '').strip()
        if not sym:
            continue

        # 方向：优先 direction（Dashboard schema 多/空），降级 bs（vnpy schema 买/卖）
        raw_dir = str(item.get('direction') or item.get('bs') or '')
        direction = _parse_direction(raw_dir)
        if direction is None:
            logger.warning(f"[结算单] 未知方向拒绝入账: bs={raw_dir!r}, sym={sym}")
            continue

        open_price = float(item.get('open_price') or 0.0)
        position   = int(item.get('position') or item.get('vol') or item.get('volume') or 0)
        if open_price <= 0 or position <= 0:
            continue

        key = f"{sym}_{direction}"
        detail_calc[key]["total_cost"] += open_price * position
        detail_calc[key]["total_vol"]  += position

    # VWAP 回填
    for key, data in detail_calc.items():
        if data["total_vol"] > 0:
            settlement_dict[key] = round(data["total_cost"] / data["total_vol"], 4)

    return settlement_dict


# ---------------------------------------------------------------------------
# 结算价 — 净仓聚合
# ---------------------------------------------------------------------------
def load_settlement_prices(settlement_json: dict) -> dict:
    """
    净仓聚合今结算价（Settlement Price）。
    今结算价 = pnl_today 基准价（降级链：昨快照 adjust_price → 今结算价 → None 不计入）。
    返回: { "IC2612": 7347.4, "IC2609": 7565.0, ... }
    """
    details = settlement_json.get('positions_detail') or []
    return _net_aggregate(details, 'settl_price')


# ---------------------------------------------------------------------------
# 便捷函数
# ---------------------------------------------------------------------------
def get_cost(settlement_dict: dict, symbol: str, direction: str) -> float:
    """查询单合约净仓均价（key 不带方向后缀）。"""
    return settlement_dict.get(symbol, 0.0)


# ---------------------------------------------------------------------------
# 目录级加载器（同步，一次性）
# ---------------------------------------------------------------------------
def load_settlement_sync(dir_path: str) -> dict:
    """
    扫描 dir_path 目录，加载最新的 full_*.json，
    返回净仓聚合开仓均价: { "IC2612": 7434.44, ... }
    """
    pattern = glob.glob(str(dir_path).rstrip('/\\') + '/full_*.json')
    if not pattern:
        return {}
    latest = sorted(pattern, reverse=True)[0]
    try:
        with open(latest, encoding='utf-8') as f:
            data = json.load(f)
    except Exception:
        return {}
    return load_settlement_cost(data)

# =======================================================================
# SettlementManager — 状态管理器（增量同步 + 日期标记）
# =======================================================================
# 统一到项目内 结算单/（与 vnpy_ctp 网关硬编码写入路径一致）
SETTLEMENT_DIR   = os.path.abspath(
    os.path.join(os.path.realpath(os.path.dirname(os.path.dirname(__file__))),
                 "结算单"))
META_FILE        = os.path.join(SETTLEMENT_DIR, "settlement_meta.json")
LOOKBACK_DAYS    = 30          # 每次补缺漏扫描近 N 天

# CTP API endpoint（供外部调用）
SETTLEMENT_API   = "http://127.0.0.1:5000/api/debug/trigger_settlement"


def _trading_dates_up_to(end_date: date, days: int) -> list[str]:
    """返回 end_date 往前数 days 个交易日内所有日期（跳过周末）。"""
    dates = []
    d = end_date
    while len(dates) < days:
        if d.weekday() < 5:          # Mon-Fri
            dates.append(d.strftime("%Y%m%d"))
        d -= timedelta(days=1)
    return dates


def _cutoff_date() -> str:
    """
    根据当前时间返回"有效结算单日期"分界。
    20:00 之前 → 昨日；20:00 之后 → 今日。
    """
    now = datetime.now()
    if now.hour < 20:
        d = now.date() - timedelta(days=1)
    else:
        d = now.date()
    return d.strftime("%Y%m%d")


def _read_meta() -> dict:
    if os.path.exists(META_FILE):
        try:
            with open(META_FILE, encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass
    return {"latest": None, "loaded": False}


def _write_meta(meta: dict):
    os.makedirs(SETTLEMENT_DIR, exist_ok=True)
    with open(META_FILE, 'w', encoding='utf-8') as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)


def _scanned_dates() -> set[str]:
    """返回结算单目录中已有 full_*.json 的所有日期。"""
    pattern = glob.glob(os.path.join(SETTLEMENT_DIR, "full_*.json"))
    dates = set()
    for p in pattern:
        bn = os.path.basename(p)           # e.g. full_20260910.json
        if bn.startswith("full_") and bn.endswith(".json"):
            dates.add(bn[5:-5])            # e.g. 20260910
    return dates


def _is_valid_settlement(path: str) -> bool:
    """
    Schema 校验：必须是"真结算单"——含 positions_detail，且行带 instrument + 结算价字段。
    用于排除实时持仓 dump 冒充的 full_*.json（如 20260916：只有 positions/positions_summary）。
    """
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
    except Exception:
        return False
    rows = data.get('positions_detail') or []
    if not rows:
        return False
    r0 = rows[0]
    return bool(r0.get('instrument')) and ('settl_price' in r0 or 'settlement_price' in r0)


def _valid_dates() -> set[str]:
    """schema 有效的结算单日期（meta.latest / 基准价选择只用这些；不改名不删文件）。"""
    return {d for d in _scanned_dates()
            if _is_valid_settlement(os.path.join(SETTLEMENT_DIR, f"full_{d}.json"))}


def _missing_dates() -> list[str]:
    """
    返回近 LOOKBACK_DAYS 天内缺失的日期列表（旧日期在前）。
    只返回那些尚未有 full_*.json 的交易日期。
    """
    today = datetime.now().date()
    all_trading = _trading_dates_up_to(today, LOOKBACK_DAYS)
    scanned     = _scanned_dates()
    missing = [d for d in all_trading if d not in scanned]
    return missing   # 已按旧→新排序


class SettlementManager:
    """
    结算单管理器：增量扫描 + CTP 下载 + 解析入库 + 日期标记。

    用法（每次 CTP 连接时调用一次）：
        sm = SettlementManager()
        sm.sync()          # 补缺漏 + 下载当天
        costs = sm.get_all_costs()
    """

    def __init__(self, api_url: str = SETTLEMENT_API):
        self.api_url   = api_url
        self._cost_cache: dict[str, float] = {}
        self._price_cache: dict[str, float] = {}
        self._meta     = _read_meta()

    # ------------------------------------------------------------------
    # 公开 API
    # ------------------------------------------------------------------
    def sync(self) -> dict:
        """
        执行增量同步：
          1. 扫本地 full_*.json，找缺漏日期
          2. 补缺漏（CTP 下载 → 解析入库）
          3. 下载/更新当天结算单（20:00 后）
          4. 更新 meta 日期标记
        返回 sync 结果摘要。
        """
        import requests

        missing = _missing_dates()
        results = {"missing_filled": [], "today_updated": False, "errors": []}

        # Step 1: 补历史缺漏
        for d in missing:
            ok, err = self._download_and_parse(d)
            if ok:
                results["missing_filled"].append(d)
            else:
                results["errors"].append(f"{d}: {err}")

        # Step 2: 下载当天（如 20:20 后）
        # 20:20 起 CTP 当日结算单可查；口径：有 full_{date}.json 就是已下载，不另设标记
        today_str = _cutoff_date()
        _now = datetime.now()
        if _now.hour * 60 + _now.minute >= 20 * 60 + 20:
            # 检查当天是否已入库（防止重复下载）
            if today_str not in _scanned_dates():
                ok, err = self._download_and_parse(today_str)
                if ok:
                    results["today_updated"] = True
                else:
                    results["errors"].append(f"today({today_str}): {err}")
            else:
                # 当天已入库，检查是否需要更新（文件可能已过期）
                results["today_updated"] = False   # 无需更新

        # Step 3: 更新 meta
        self._refresh_meta()
        results["meta"] = self._meta
        return results

    def get_all_costs(self) -> dict[str, float]:
        """
        返回内存中缓存的完整开仓成本字典。
        Greeks 计算调用此方法，传入 risk_engine。
        """
        return self._cost_cache.copy()

    def get_all_prices(self) -> dict[str, float]:
        """返回内存中缓存的昨结算价字典（昨结算价 = pnl_today 基准）。"""
        return self._price_cache.copy()

    def get_cost(self, symbol: str, direction: str) -> float | None:
        """查询单条开仓成本，无数据返回 None。"""
        d = _parse_direction(direction)
        return self._cost_cache.get(f"{symbol}_{d}")

    def get_latest_date(self) -> str | None:
        """返回 meta 中标记的最新结算单日期（有效结算单日期，非入库时间）。"""
        return self._meta.get("latest")

    def load_costs_from_meta(self):
        """
        根据 meta.latest 加载对应 full_*.json 到内存缓存。
        启动时调用（CTP 断线重连后不重新下载，直接用已入库数据）。
        若 meta.latest 不存在（首次运行/ meta 文件丢失），自动扫描最新 full_*.json 兜底。
        """
        # 网关解析线程会更新磁盘 meta（如 20:00 后新结算单入库）→ 每轮重读，否则常驻进程永远停在旧日期
        self._meta = _read_meta()
        latest = self._meta.get("latest")
        valid = _valid_dates()
        # meta.latest 缺失 / 指向 schema 无效文件（如实时持仓 dump）→ 退回最近有效结算单
        if not latest or latest not in valid:
            if not valid:
                return
            logger.info(f"[SettlementManager] meta.latest={latest!r} 无效，改用 {max(valid)}")
            latest = max(valid)
            self._meta["latest"] = latest
        path = os.path.join(SETTLEMENT_DIR, f"full_{latest}.json")
        if not os.path.exists(path):
            return
        try:
            with open(path, encoding='utf-8') as f:
                data = json.load(f)
            self._cost_cache  = load_settlement_cost(data)
            self._price_cache = load_settlement_prices(data)
            logger.info(f"[SettlementManager] 加载结算单 full_{latest}.json，_price_cache {len(self._price_cache)} 条，_cost_cache {len(self._cost_cache)} 条")
        except Exception:
            pass

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------
    def _download_and_parse(self, trading_date: str) -> tuple[bool, str]:
        """
        下载指定日期结算单 txt，解析为 full_*.json。
        策略：POST 触发 CTP 请求后，轮询等待 gateway 异步回调写入文件，再读文件解析。
        返回 (成功, 错误信息)。
        """
        import requests, time

        # 1. CTP 触发请求（不等待回调）
        try:
            r = requests.post(
                self.api_url,
                json={"TradingDay": trading_date},
                timeout=5
            )
            http_code = r.status_code
            resp_text = r.text[:200] if r.text else ""
        except Exception as e:
            return False, f"请求异常: {e}"

        # 2. 轮询等待 gateway 写文件（最多 30s，每 0.5s 检查一次）
        txt_path = os.path.join(SETTLEMENT_DIR, f"ctp_settlement_{trading_date}.txt")
        file_found = False
        final_size = 0
        for i in range(60):
            if os.path.exists(txt_path):
                size1 = os.path.getsize(txt_path)
                time.sleep(0.5)
                size2 = os.path.getsize(txt_path)
                if size1 == size2 and size1 > 100:
                    file_found = True
                    final_size = size1
                    break
            time.sleep(0.5)
        else:
            if not os.path.exists(txt_path):
                return False, f"HTTP={http_code} resp={resp_text!r} 文件未出现（{trading_date}），服务器返回空"

        # 3. 解析文件
        time.sleep(0.3)
        ok, err = self._parse_txt(trading_date, txt_path)
        if not ok:
            return False, f"HTTP={http_code} 文件已找到({final_size}字节)但解析失败: {err}"

        return True, ""

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------
    @staticmethod
    def _find_section_header(lines: list[str], keywords: tuple[str, ...]) -> int | None:
        """返回包含所有 keywords 的行号（严格AND），未找到返回 None。"""
        for i, line in enumerate(lines):
            s = line.strip()
            if all(kw in s for kw in keywords):
                return i
        return None

    @staticmethod
    def _parse_pipe_header(header_line: str) -> tuple[list[str], dict[str, int]]:
        """
        解析 pipe 分隔的 header 行，返回 (col_names, col_index_map)。
        col_index_map key: 英文 lower → idx（取第一个匹配）。
        """
        raw_cols = [c.strip() for c in header_line.split('|')[1:-1]]
        col_map = {}
        for idx, col in enumerate(raw_cols):
            cl = col.lower()
            if cl and col_map.get(cl) is None:
                col_map[cl] = idx
        return raw_cols, col_map

    @staticmethod
    def _parse_pipe_row(line: str, all_col_names: list[str]) -> dict[str, str] | None:
        """解析单行 pipe 分隔数据，返回 {col_name: value} 或 None。"""
        s = line.strip()
        if not s or s.startswith('|---') or '-------' in s:
            return None
        if s.startswith('|'):
            s = s[1:]
        if s.endswith('|'):
            s = s[:-1]
        parts = [p.strip() for p in s.split('|')]
        if len(parts) < 2:
            return None
        return dict(zip(all_col_names, parts[:len(all_col_names)]))

    @staticmethod
    def _parse_account_summary(lines: list[str], start: int) -> dict:
        """
        解析资金状况区域（start 附近约40行）。
        每行格式:  Key：Value  Key：Value（双字节冒号，变长空格分隔）
        用正则匹配所有 key：value 对，key 支持中英文标签。
        """
        import re
        result = {}
        for line in lines[start:start + 50]:
            raw = line.strip()
            if not raw or '：' not in raw:
                continue
            if raw.startswith('|---') or '-------' in raw or raw.startswith('|'):
                continue
            # 匹配 [任意空白]key[任意空白]：value
            # 贪婪匹配key部分，值取冒号后到行尾或下一key前
            for seg_m in re.finditer(r'([^\s：]+)：\s*([\d.\-]+|[^\s：]+(?:\s+[^\s：]+：[^\s：]+)*)', raw):
                raw_key = seg_m.group(1).strip()
                raw_val = seg_m.group(2).strip()
                # 提取英文标签（括号内）
                en_key = raw_key
                if '(' in raw_key and ')' in raw_key:
                    try:
                        en_key = raw_key[raw_key.index('(') + 1:raw_key.index(')')]
                    except ValueError:
                        pass
                try:
                    result[en_key] = float(raw_val.replace(',', ''))
                except ValueError:
                    result[en_key] = raw_val
        return result

    @staticmethod
    def _is_symbol(s: str) -> bool:
        """判断是否疑似交易合约代码（非交易所内部编码）。"""
        return bool(s and len(s) >= 3 and s[0].isalpha() and any(c.isdigit() for c in s))

    # ------------------------------------------------------------------
    # 多表解析
    # ------------------------------------------------------------------
    def _parse_txt(self, trading_date: str, txt_path: str) -> tuple[bool, str]:
        """
        将 ctp_settlement_{date}.txt（pipe 分隔表格格式）解析为 full_*.json。
        动态检测所有表：资金状况、成交记录、行权明细（有则解析）、平仓明细、持仓明细。
        只剔除交易所内部字段（交易编码、交易所代码等），其余全部保留。
        """
        try:
            with open(txt_path, encoding="utf-8", errors="replace") as f:
                raw = f.read()
        except Exception as e:
            return False, str(e)
        if not raw or len(raw) < 50:
            return False, "文件内容过短"

        lines = raw.split('\n')
        total = len(lines)

        # ── 1. 资金状况 ──────────────────────────────────────────────
        sec = self._find_section_header(lines, ('资金状况', 'Account Summary'))
        account_summary = self._parse_account_summary(lines, sec or 0) if sec is not None else {}

        # ── 2. 持仓明细（最细粒度，含 Settlement Price） ───────────────
        pos_detail = []
        sec = self._find_section_header(lines, ('持仓明细', 'Positions Detail'))
        if sec is not None:
            header_line = ''
            data_start = sec + 3  # 跳过空行+分隔线
            for i in range(sec, min(sec + 5, total)):
                if lines[i].strip().startswith('|'):
                    header_line = lines[i].strip()
                    data_start = i + 2
                    break
            if header_line:
                all_cols, col_map = self._parse_pipe_header(header_line)
                for line in lines[data_start:]:
                    row = self._parse_pipe_row(line, all_cols)
                    if not row:
                        continue
                    # 提取合约代码（取英文 Instrument 列）
                    sym = row.get('Instrument', row.get('合约', '')).strip()
                    if not self._is_symbol(sym):
                        continue
                    # 提取结算价（Settlement Price）
                    sttl_price = row.get('Settlement Price', row.get('结算价', ''))
                    try:
                        sttl_price = float(sttl_price) if sttl_price else 0.0
                    except ValueError:
                        sttl_price = 0.0
                    # 提取开仓价
                    open_price = row.get('Pos. Open Price', row.get('开仓价', ''))
                    try:
                        open_price = float(open_price) if open_price else 0.0
                    except ValueError:
                        open_price = 0.0
                    # 提取昨结算（Prev. Sttl）
                    prev_sttl = row.get('Prev. Sttl', row.get('昨结算', ''))
                    try:
                        prev_sttl = float(prev_sttl) if prev_sttl else 0.0
                    except ValueError:
                        prev_sttl = 0.0
                    # 提取多空方向
                    bs = row.get('B/S', row.get('买/卖', row.get('B/S ', ''))).strip()
                    direction = _parse_direction(bs)
                    # 提取持仓量
                    vol_val = row.get('Positon', row.get('持仓量', ''))
                    try:
                        vol = int(float(vol_val)) if vol_val else 0
                    except ValueError:
                        vol = 0
                    pos_detail.append({
                        "instrument": sym,
                        "direction": direction,
                        "volume": vol,
                        "open_price": open_price,
                        "settlement_price": sttl_price,
                        "prev_sttl_price": prev_sttl,
                        "_raw": row,
                    })

        # ── 3. 成交记录 ──────────────────────────────────────────────
        tx_records = []
        sec = self._find_section_header(lines, ('成交记录', 'Transaction Record'))
        if sec is not None:
            header_line = ''
            data_start = sec + 3
            for i in range(sec, min(sec + 5, total)):
                if lines[i].strip().startswith('|'):
                    header_line = lines[i].strip()
                    data_start = i + 2
                    break
            if header_line:
                all_cols, col_map = self._parse_pipe_header(header_line)
                for line in lines[data_start:]:
                    row = self._parse_pipe_row(line, all_cols)
                    if not row:
                        continue
                    sym = row.get('Instrument', row.get('合约', '')).strip()
                    if not self._is_symbol(sym):
                        continue
                    # 剔除交易编码（内部字段）
                    clean_row = {k: v for k, v in row.items()
                                 if k.lower() not in ('tradingcode', '交易编码')}
                    tx_records.append({"_raw": clean_row})

        # ── 4. 平仓明细 ──────────────────────────────────────────────
        closed = []
        sec = self._find_section_header(lines, ('平仓明细', 'Position Closed'))
        if sec is not None:
            header_line = ''
            data_start = sec + 3
            for i in range(sec, min(sec + 5, total)):
                if lines[i].strip().startswith('|'):
                    header_line = lines[i].strip()
                    data_start = i + 2
                    break
            if header_line:
                all_cols, col_map = self._parse_pipe_header(header_line)
                for line in lines[data_start:]:
                    row = self._parse_pipe_row(line, all_cols)
                    if not row:
                        continue
                    sym = row.get('Instrument', row.get('合约', '')).strip()
                    if not self._is_symbol(sym):
                        continue
                    clean_row = {k: v for k, v in row.items()
                                 if k.lower() not in ('tradingcode', '交易编码')}
                    closed.append({"_raw": clean_row})

        # ── 5. 行权明细（有则解析，无则空） ──────────────────────────
        exercise = []
        sec = self._find_section_header(lines, ('行权明细', 'Exercise Statement'))
        if sec is not None:
            header_line = ''
            data_start = sec + 3
            for i in range(sec, min(sec + 5, total)):
                if lines[i].strip().startswith('|'):
                    header_line = lines[i].strip()
                    data_start = i + 2
                    break
            if header_line:
                all_cols, col_map = self._parse_pipe_header(header_line)
                for line in lines[data_start:]:
                    row = self._parse_pipe_row(line, all_cols)
                    if not row:
                        continue
                    sym = row.get('Instrument', row.get('合约', '')).strip()
                    if not self._is_symbol(sym):
                        continue
                    clean_row = {k: v for k, v in row.items()
                                 if k.lower() not in ('tradingcode', '交易编码')}
                    exercise.append({"_raw": clean_row})

        if not pos_detail and not tx_records and not closed:
            return False, f"未识别到任何持仓/成交/平仓表（{total}行）"

        # ── 写入 full_*.json ────────────────────────────────────────
        full_json = {
            "trading_date": trading_date,
            "account_summary": account_summary,
            "positions_detail": pos_detail,
            "transaction_records": tx_records,
            "closed_positions": closed,
            "exercise_records": exercise,
        }
        out_path = os.path.join(SETTLEMENT_DIR, f"full_{trading_date}.json")
        with open(out_path, 'w', encoding='utf-8') as f:
            json.dump(full_json, f, ensure_ascii=False, indent=2)

        self._cost_cache  = load_settlement_cost(full_json)
        self._price_cache = load_settlement_prices(full_json)
        return True, ""

    def _refresh_meta(self):
        """扫描 full_*.json，更新 meta.latest 为 schema 有效的最近结算单日期。"""
        dates = _valid_dates()
        if not dates:
            self._meta = {"latest": None, "loaded": False}
            _write_meta(self._meta)
            return

        # 取最大日期（字符串比较即正确排序）
        latest = max(dates)
        self._meta = {"latest": latest, "loaded": True}
        _write_meta(self._meta)


# -----------------------------------------------------------------------
# 冒烟测试
# -----------------------------------------------------------------------
if __name__ == "__main__":
    sm = SettlementManager()
    print(f"meta: {sm._meta}")
    print(f"已有日期: {sorted(_scanned_dates())}")
    print(f"缺漏日期: {_missing_dates()}")
    print(f"有效结算单日期: {_cutoff_date()}")
