# -*- coding: utf-8 -*-
"""
旧快照（vnpy接口封装\data_snapshot_<date>_<sess>.json）→ 新格式 v3（GreeksDashboard_v0.1\快照\）。

ponytail: 不调 build_tree 重算——旧文件 underlying_ticks 为空，重算会归零；
历史快照语义 = 静态截面存档，旧 positions 自带终值，直接照搬进 L3 节点。
只复用 risk_engine 的分组/累加/标签纯函数以保证与实时快照结构一致。
用完即弃，不进服务。

用法: python tools/convert_snapshots.py [--force]
"""
import argparse
import datetime
import hashlib
import json
import os
import sys

_OLD_DIR = r"C:\Quant_2026\期货执行策略\vnpy接口封装"
_NEW_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "快照")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dashboard_v2.risk_engine import (  # noqa: E402
    normalize_underlying, extract_expiry, tag_delta, tag_gamma, tag_pnl,
    _make_metrics, _make_summary, _accumulate_metrics, _accumulate_summary,
)

_FILES = [
    "data_snapshot_20260908_P.json",
    "data_snapshot_20260909_A.json",
    "data_snapshot_20260910_N.json",
    "data_snapshot_20260911_A.json",
    "data_snapshot_20260911_P.json",
]


def _l3_from_old(pos: dict) -> dict:
    """旧 position → 新 L3 树节点（与 risk_engine._build_l3_node 同键）"""
    sym = pos["symbol"].split(".")[0]
    direction_str = "多" if pos.get("direction") in ("long", "多") else "空"
    dc = pos.get("deltacash", 0) or 0
    gc = pos.get("gammacash", 0) or 0
    ph = pos.get("pnl_history", 0) or 0
    return {
        "key": f"{sym}_{direction_str}",
        "symbol": sym,
        "direction": direction_str,
        "direction_raw": pos.get("direction", "long"),
        "volume": pos.get("volume", 0),
        "last_price": pos.get("price", 0) or 0,
        "adjust_price": pos.get("adjust_price", 0) or 0,
        "open_price": pos.get("open_price", pos.get("pos_price", 0)) or 0,
        "underlying_price": pos.get("underlying_price", 0) or 0,
        "iv": pos.get("iv"),
        "days_to_expiry": pos.get("days_to_expiry"),
        "itm": bool(pos.get("is_itm")),
        "delta": pos.get("delta", 0) or 0,
        "gamma": pos.get("gamma", 0) or 0,
        "vega": pos.get("vega", 0) or 0,
        "theta": pos.get("theta", 0) or 0,
        "deltacash": dc, "gammacash": gc,
        "vegacash": pos.get("vegacash", 0) or 0,
        "thetacash": pos.get("thetacash", 0) or 0,
        "pnl_daily": pos.get("pnl_daily", 0) or 0,
        "pnl_today": pos.get("pnl_today", 0) or 0,
        "pnl_history": ph,
        "delta_tag": tag_delta(dc),
        "gamma_tag": tag_gamma(gc),
        "pnl_tag": tag_pnl(ph),
    }


def _flat_from_old(pos: dict) -> dict:
    """旧 position → 新 raw.positions 平铺（对齐 api_server 构造处）"""
    return {
        "symbol": pos.get("symbol", ""),
        "direction": pos.get("direction", "long"),
        "volume": pos.get("volume", 0),
        "available": pos.get("volume", 0),
        "price": pos.get("open_price", pos.get("pos_price", 0)) or 0,
        "last_price": pos.get("price", 0) or 0,
        "underlying_price": pos.get("underlying_price", 0) or 0,
        "iv": pos.get("iv"),
        "underlying": pos.get("underlying", ""),
        "expiry": "",
        "strike": pos.get("strike", 0) or 0,
        "option_type": pos.get("option_type", "") or "",
        "size": pos.get("size", 1) or 1,
        "delta": pos.get("delta", 0) or 0,
        "gamma": pos.get("gamma", 0) or 0,
        "vega": pos.get("vega", 0) or 0,
        "theta": pos.get("theta", 0) or 0,
        # ponytail: 旧文件仅存位置级 delta（期货 delta=2 即 2 手），pos_* 照搬；gammacash 等已有
        "pos_delta": pos.get("delta", 0) or 0,
        "pos_gamma": pos.get("gamma", 0) or 0,
        "pos_vega": pos.get("vega", 0) or 0,
        "pos_theta": pos.get("theta", 0) or 0,
        "deltacash": pos.get("deltacash", 0) or 0,
        "gammacash": pos.get("gammacash", 0) or 0,
        "vegacash": pos.get("vegacash", 0) or 0,
        "thetacash": pos.get("thetacash", 0) or 0,
        "is_itm": bool(pos.get("is_itm")),
        "days_to_expiry": pos.get("days_to_expiry"),
    }


def _build_tree_from_old(positions: list):
    """按 build_tree 同规则分组聚合，但 L3 用旧终值"""
    from collections import defaultdict
    product_map = defaultdict(lambda: defaultdict(list))
    for pos in positions:
        sym = pos["symbol"].split(".")[0]
        product_map[normalize_underlying(sym)][extract_expiry(sym)].append(pos)

    total = _make_summary()
    tree = []
    for product in sorted(product_map.keys()):
        l1_children = []
        l1_metrics = _make_metrics()
        for month in sorted(product_map[product].keys()):
            l3_nodes = [_l3_from_old(p) for p in product_map[product][month]]
            if not l3_nodes:
                continue
            l2_metrics = _make_metrics()
            for n in l3_nodes:
                _accumulate_metrics(l2_metrics, n)
            l2_node = {
                "key": f"{product}_{month}",
                "name": f"{month}月份",
                "type": "L2_MONTH",
                "metrics": l2_metrics,
                "children": l3_nodes,
            }
            l1_children.append(l2_node)
            _accumulate_metrics(l1_metrics, l2_node)  # ponytail: 传节点(含metrics)，否则裸dict被当成L3→取{}累加0
        if l1_children:
            tree.append({
                "key": product, "name": product, "type": "L1_PRODUCT",
                "metrics": l1_metrics, "children": l1_children,
            })
            _accumulate_summary(total, l1_metrics)
    return {"summary": total, "tree": tree}


def _account_from_old(acct: dict) -> dict:
    return {
        "balance": acct.get("balance", 0) or 0,
        "available": acct.get("available", 0) or 0,
        "commission": acct.get("commission", 0) or 0,
        "margin": acct.get("margin", 0) or 0,
        "position_pnl": acct.get("position_profit", acct.get("position_pnl", 0)) or 0,
        "close_pnl": acct.get("close_profit", acct.get("close_pnl", 0)) or 0,
    }


def convert(old_dir=_OLD_DIR, new_dir=_NEW_DIR, files=None, force=False):
    files = files or _FILES
    os.makedirs(new_dir, exist_ok=True)
    out = []
    for fname in files:
        src = os.path.join(old_dir, fname)
        if not os.path.isfile(src):
            print(f"[skip] 源缺失 {fname}")
            continue
        with open(src, "r", encoding="utf-8") as f:
            old = json.load(f)
        positions = old.get("positions", [])
        if not positions:
            print(f"[skip] 空持仓 {fname}")
            continue

        trading_date = old.get("trading_date", "").replace("-", "")
        session = old.get("session", "")
        dst_name = f"data_snapshot_{trading_date}_{session}.json"
        dst = os.path.join(new_dir, dst_name)
        if os.path.isfile(dst) and not force:
            print(f"[skip] 已存在 {dst_name}（--force 覆盖）")
            continue

        built = _build_tree_from_old(positions)
        flat = [_flat_from_old(p) for p in positions]
        payload = {
            "version": 3,
            "saved_at": str(old.get("saved_at", "")).replace(" ", "T"),
            "trading_date": trading_date,
            "session": session,
            "ctp_status": "connected",
            "data_hash": hashlib.sha256(
                json.dumps(flat, sort_keys=True, ensure_ascii=False).encode("utf-8")
            ).hexdigest(),
            "raw": {
                "positions": flat,
                "underlying_prices": {
                    p["underlying"]: p["underlying_price"]
                    for p in flat
                    if p.get("underlying") and p.get("underlying_price")
                },
            },
            "computed": {
                "summary": built["summary"],
                "tree": built["tree"],
            },
            "account": _account_from_old(old.get("account", {}) or {}),
        }
        tmp = dst + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp, dst)
        s = built["summary"]
        out.append({
            "file": dst_name, "positions": len(positions),
            "products": len(built["tree"]),
            "total_deltacash": s["total_deltacash"],
            "total_pnl_daily": s["total_pnl_daily"],
            "total_pnl_today": s["total_pnl_today"],
            "total_pnl_history": s["total_pnl_history"],
        })
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="覆盖已存在的目标文件")
    args = ap.parse_args()
    rows = convert(force=args.force)
    print(json.dumps(rows, ensure_ascii=False, indent=2))
    print(f"\n完成 {len(rows)} 个 → {_NEW_DIR}")

    # 自检：L1 metrics 之和 == summary
    for r in rows:
        with open(os.path.join(_NEW_DIR, r["file"]), "r", encoding="utf-8") as f:
            d = json.load(f)
        s = d["computed"]["summary"]
        for k, mk in (("deltacash", "total_deltacash"), ("gammacash", "total_gammacash"),
                      ("pnl_daily", "total_pnl_daily"), ("pnl_history", "total_pnl_history")):
            acc = sum(n["metrics"][k] for n in d["computed"]["tree"])
            assert acc == s[mk], (r["file"], k, acc, s[mk])
    print("自检通过: L1 聚合 == summary")
