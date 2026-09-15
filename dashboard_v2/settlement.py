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

# -----------------------------------------------------------------------
# 方向映射表（兼容多种字段命名）
# -----------------------------------------------------------------------
_LONG_DIRS  = {'买', '多', 'B', '1', 'Buy', 'L', 'Long', 'long'}
_SHORT_DIRS = {'卖', '空', 'S', '-1', 'Sell', 'S', 'Short', 'short'}

def _parse_direction(raw: str) -> str:
    if not raw:
        return '空'
    s = str(raw).strip()
    if s in _LONG_DIRS:
        return '多'
    if s in _SHORT_DIRS:
        return '空'
    if s[0] in ('买', '多', 'B', 'L', '1'):
        return '多'
    return '空'

# -----------------------------------------------------------------------
# 策略1：嗅探券商预计算汇总均价
# -----------------------------------------------------------------------
def _load_from_summary(settlement_json: dict) -> dict:
    result = {}
    summary_list = (
        settlement_json.get('positions')
        or settlement_json.get('positions_summary')
        or []
    )
    for item in summary_list:
        sym = (
            item.get('instrument')
            or item.get('symbol')
            or item.get('instrument_id')
            or ''
        )
        if not sym:
            continue
        avg_buy  = item.get('avg_buy') or item.get('avg_open_price') or item.get('vwap') or 0.0
        long_pos  = item.get('long_pos', 0)
        if long_pos and float(avg_buy) > 0:
            result[f"{sym}_多"] = round(float(avg_buy), 4)

        avg_sell = item.get('avg_sell') or item.get('avg_open_price_short') or 0.0
        short_pos = item.get('short_pos', 0)
        if short_pos and float(avg_sell) > 0:
            result[f"{sym}_空"] = round(float(avg_sell), 4)
    return result

# -----------------------------------------------------------------------
# 策略2：逐笔明细加权自算 VWAP
# -----------------------------------------------------------------------
def _load_from_detail(settlement_json: dict) -> dict:
    details = settlement_json.get('positions_detail') or []
    calc = defaultdict(lambda: {"total_cost": 0.0, "total_vol": 0})
    for item in details:
        sym = item.get('instrument') or item.get('symbol') or item.get('instrument_id') or ''
        if not sym:
            continue
        raw_dir = item.get('bs') or item.get('direction') or item.get('side') or ''
        direction = _parse_direction(raw_dir)
        price = item.get('open_price') or item.get('price') or item.get('trade_price') or 0.0
        vol   = item.get('volume') or item.get('vol') or item.get('qty') or 0
        try:
            p, v = float(price), int(vol)
            if p > 0 and v > 0:
                key = f"{sym}_{direction}"
                calc[key]["total_cost"] += p * v
                calc[key]["total_vol"]  += v
        except (ValueError, TypeError):
            continue

    result = {}
    for key, data in calc.items():
        if data["total_vol"] > 0:
            result[key] = round(data["total_cost"] / data["total_vol"], 4)
    return result

# -----------------------------------------------------------------------
# 主入口：双策略加载
# -----------------------------------------------------------------------
def load_settlement_cost(settlement_json: dict) -> dict:
    """
    双策略加载结算单加权开仓成本。
    策略1 → 策略2 顺序执行，后者覆盖前者（明细 VWAP 优先于汇总均价）。
    返回: { "IF2609_多": 4210.0, "IC2609_空": 8048.6, ... }
    """
    result = {}
    s1 = _load_from_summary(settlement_json)
    result.update(s1)
    s2 = _load_from_detail(settlement_json)
    result.update(s2)
    return result

# -----------------------------------------------------------------------
# 便捷函数：给定 symbol + direction，返回开仓成本
# -----------------------------------------------------------------------
def get_cost(settlement_dict: dict, symbol: str, direction: str) -> float:
    d = _parse_direction(direction)
    return settlement_dict.get(f"{symbol}_{d}", 0.0)

# -----------------------------------------------------------------------
# 目录级加载器（同步，一次性）
# -----------------------------------------------------------------------
def load_settlement_sync(dir_path: str) -> dict:
    """
    扫描 dir_path 目录，加载最新的 full_*.json，
    返回 {f"{symbol}_多": avg_buy_price, f"{symbol}_空": avg_sell_price}。
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

    result = {}
    for p in data.get('positions', []):
        sym = p.get('instrument', '') or p.get('symbol', '') or ''
        if not sym:
            continue
        avg_buy  = round(float(p.get('avg_buy_price')  or 0), 4)
        avg_sell = round(float(p.get('avg_sell_price') or 0), 4)
        if avg_buy > 0:
            result[f"{sym}_多"] = avg_buy
        if avg_sell > 0:
            result[f"{sym}_空"] = avg_sell
    return result

# =======================================================================
# SettlementManager — 状态管理器（增量同步 + 日期标记）
# =======================================================================
SETTLEMENT_DIR   = r"C:\Quant_2026\期货执行策略\GreeksDashboard_v0.1\结算单"
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

        # Step 2: 下载当天（如 20:00 后）
        today_str = _cutoff_date()
        if datetime.now().hour >= 20:
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
        """
        latest = self._meta.get("latest")
        if not latest:
            return
        path = os.path.join(SETTLEMENT_DIR, f"full_{latest}.json")
        if not os.path.exists(path):
            return
        try:
            with open(path, encoding='utf-8') as f:
                data = json.load(f)
            self._cost_cache = load_settlement_cost(data)
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

    def _parse_txt(self, trading_date: str, txt_path: str) -> tuple[bool, str]:
        """
        将 ctp_settlement_{date}.txt（pipe分隔表格格式）解析为 full_*.json。
        CTP结算单格式：|AccountID|BrokerID|Product|Instrument|LongPos.|AvgBuyPrice|ShortPos.|AvgSellPrice|...|
        """
        try:
            with open(txt_path, encoding="utf-8", errors="replace") as f:
                raw = f.read()
        except Exception as e:
            return False, str(e)

        if not raw or len(raw) < 50:
            return False, "文件内容过短"

        positions = []
        lines = raw.split('\n')

        for line in lines:
            line = line.strip()
            if not line or line.startswith('|---') or '-------' in line:
                continue
            # 去掉首尾|
            if line.startswith('|'):
                line = line[1:]
            if line.endswith('|'):
                line = line[:-1]

            parts = [p.strip() for p in line.split('|')]
            if len(parts) < 9:
                continue

            # 合约代码在第4列（index 3）
            sym = parts[3]
            if not (sym and len(sym) >= 3 and sym[0].isalpha() and any(c.isdigit() for c in sym)):
                continue

            try:
                long_vol  = int(float(parts[4])) if parts[4].strip() else 0
                avg_buy   = float(parts[5]) if parts[5].strip() else 0.0
                short_vol = int(float(parts[6])) if parts[6].strip() else 0
                avg_sell  = float(parts[7]) if parts[7].strip() else 0.0
            except (ValueError, IndexError):
                continue

            if long_vol > 0 and avg_buy > 0:
                positions.append({
                    "instrument":     sym,
                    "direction":      "多",
                    "volume":         long_vol,
                    "open_price":     avg_buy,
                    "avg_buy_price":  avg_buy,
                    "avg_sell_price": 0.0,
                })
            if short_vol > 0 and avg_sell > 0:
                positions.append({
                    "instrument":     sym,
                    "direction":      "空",
                    "volume":         short_vol,
                    "open_price":     avg_sell,
                    "avg_buy_price":  0.0,
                    "avg_sell_price": avg_sell,
                })

        if not positions:
            return False, f"未能从结算单提取到持仓数据（{len(lines)}行）"

        # 写入 full_*.json
        full_json = {
            "trading_date": trading_date,
            "positions": positions,
            "positions_summary": positions,
        }
        out_path = os.path.join(SETTLEMENT_DIR, f"full_{trading_date}.json")
        with open(out_path, 'w', encoding='utf-8') as f:
            json.dump(full_json, f, ensure_ascii=False, indent=2)

        # 同步更新内存缓存
        self._cost_cache = load_settlement_cost(full_json)
        return True, ""

    def _refresh_meta(self):
        """扫描 full_*.json，更新 meta.latest 为有效结算单日期。"""
        dates = _scanned_dates()
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
