#!/usr/bin/env python3
"""构建「在线预览版」静态站点数据（docs/preview/*）。

产出:
- docs/preview/scan.json         : 最近一次扫描的全部行（供网页表格/筛选）
- docs/preview/charts/{SYM}.json : 每个币的图表数据（K线/EMA/MACD/OI/Funding/事件标记）

用法:  python scripts/build_online_preview.py [--only-signals]
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.indicators import add_indicators
from src.storage import Storage
from src.utils import get_logger, load_config, setup_logging

LOG = get_logger("preview")

BARS = 150
OI_DAYS = 20
FUNDING_DAYS = 45
SIGNAL_STATUSES = {"SHORT_READY", "SHORT_TREND", "REVERSAL_CONFIRMING", "TOP_WATCH",
                   "FIRST_WEAKNESS", "EXTREME", "HOT"}


def _sig(x, digits: int = 8):
    """round to significant digits, robust to tiny prices; None-safe."""
    if x is None:
        return None
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return float(f"{f:.{digits}g}")


def _ts(v):
    if v is None:
        return None
    try:
        return int(v.timestamp())
    except Exception:
        try:
            return int(datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp())
        except Exception:
            return None


def _load_json(s):
    if s is None:
        return None
    try:
        return json.loads(s)
    except Exception:
        return None


def build_chart(storage: Storage, rec: dict):
    sym = rec["symbol"]
    try:
        k4 = storage.load_klines(sym, "4h")
        if k4.empty:
            return None
        k4 = k4.tail(BARS).reset_index(drop=True)
        k4i = add_indicators(k4)
        out = {"symbol": sym}
        out["bars"] = {
            "t": [_ts(t) for t in k4i["open_time"]],
            "o": [_sig(x) for x in k4i["open"]],
            "h": [_sig(x) for x in k4i["high"]],
            "l": [_sig(x) for x in k4i["low"]],
            "c": [_sig(x) for x in k4i["close"]],
            "v": [_sig(x, 6) for x in k4i["quote_volume"]],
        }
        out["ema20"] = [_sig(x) for x in k4i["ema20"]]
        out["ema60"] = [_sig(x) for x in k4i["ema60"]]
        out["macd"] = [_sig(x) for x in k4i["macd"]]
        out["signal"] = [_sig(x) for x in k4i["macd_signal"]]
        out["hist"] = [_sig(x) for x in k4i["macd_hist"]]

        t0 = int(k4i["open_time"].min().timestamp())

        # OI: resample to 1h, last OI_DAYS
        oi = storage.load_oi(sym)
        if not oi.empty:
            oi = oi.set_index("time").resample("1h").last().reset_index()
            cut = oi["time"].max() - __import__("pandas").Timedelta(days=OI_DAYS)
            oi = oi[oi["time"] >= cut].dropna(subset=["open_interest_value"])
            out["oi"] = {"t": [_ts(t) for t in oi["time"]],
                         "v": [_sig(x, 6) for x in oi["open_interest_value"]]}
        else:
            out["oi"] = None

        funding = storage.load_funding(sym)
        if not funding.empty:
            cut = funding["funding_time"].max() - __import__("pandas").Timedelta(days=FUNDING_DAYS)
            funding = funding[funding["funding_time"] >= cut]
            out["funding"] = {"t": [_ts(t) for t in funding["funding_time"]],
                              "v": [_sig(x, 5) for x in funding["funding_rate"]]}
        else:
            out["funding"] = None

        events = []
        for ev in (_load_json(rec.get("timeline_json")) or []):
            t = _ts(ev.get("time"))
            if t and t >= t0:
                events.append({"t": t, "label": ev.get("label")})
        out["events"] = events

        swings = []
        for p in (_load_json(rec.get("pivots_json")) or []):
            t = _ts(p.get("time"))
            if t and t >= t0:
                swings.append({"t": t, "p": _sig(p.get("price")), "k": p.get("kind")})
        out["swings"] = swings

        explain = _load_json(rec.get("explain_json")) or {}
        out["meta"] = {
            "status": rec.get("status"),
            "oh": _sig(rec.get("overheat_score"), 5),
            "rv": _sig(rec.get("reversal_score"), 5),
            "reasons": (_load_json(rec.get("status_reasons_json")) or [])[:4],
            "reached": [x.get("label") for x in (explain.get("reached") or []) if x.get("ok")][:8],
            "missing": [x.get("label") for x in (explain.get("missing") or []) if not x.get("ok")][:8],
            "blocks": _load_json(rec.get("block_reasons_json")) or [],
        }
        return out
    except Exception as e:
        LOG.warning("chart build failed for %s: %s", sym, e)
        return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only-signals", action="store_true", help="只为信号状态币生成图表")
    args = ap.parse_args()

    cfg = load_config()
    setup_logging(cfg)
    storage = Storage(cfg, read_only=True)
    root = Path(cfg["_project_root"])
    out_dir = root / "docs" / "preview"
    charts_dir = out_dir / "charts"
    if charts_dir.exists():
        shutil.rmtree(charts_dir)
    charts_dir.mkdir(parents=True, exist_ok=True)

    scan = storage.load_latest_scan()
    if scan.empty:
        LOG.error("no scan results; run `python main.py scan` first")
        return 1
    records = scan.to_dict("records")
    LOG.info("scan rows: %s", len(records))

    target = [r for r in records if (not args.only_signals) or r.get("status") in SIGNAL_STATUSES]
    LOG.info("building charts for %s symbols...", len(target))

    done = 0
    written = set()
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(build_chart, storage, r): r["symbol"] for r in target}
        for fut in as_completed(futs):
            sym = futs[fut]
            data = fut.result()
            done += 1
            if data:
                (charts_dir / f"{sym}.json").write_text(
                    json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
                written.add(sym)
            if done % 100 == 0:
                LOG.info("charts: %s/%s", done, len(target))

    # scan.json
    rows = []
    for r in records:
        rows.append({
            "symbol": r.get("symbol"),
            "status": r.get("status"),
            "price": _sig(r.get("price")),
            "r1h": _sig(r.get("return_1h"), 5),
            "r4h": _sig(r.get("return_4h"), 5),
            "r12h": _sig(r.get("return_12h"), 5),
            "r24h": _sig(r.get("return_24h"), 5),
            "r72h": _sig(r.get("return_72h"), 5),
            "r7d": _sig(r.get("return_7d"), 5),
            "volr": _sig(r.get("volume_ratio_4h"), 5),
            "rank": _sig(r.get("rank_now"), 6),
            "rankchg": _sig(r.get("rank_change_abs"), 6),
            "oi24": _sig(r.get("oi_change_24h"), 5),
            "fund": _sig(r.get("funding_current"), 6),
            "macd_neg": bool((r.get("macd_hist") or 0) < 0),
            "struct": _struct_label(r),
            "reb": _sig(r.get("rebound_ratio"), 5),
            "oh": _sig(r.get("overheat_score"), 5),
            "rv": _sig(r.get("reversal_score"), 5),
            "last": r.get("data_last_time"),
            "block": bool(r.get("block_short")),
            "chart": r.get("symbol") in written,
            "brief": (_load_json(r.get("status_reasons_json")) or [""])[0:1],
        })

    counts = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    data_last = None
    try:
        import pandas as pd
        s = pd.to_datetime([r.get("last") for r in rows], errors="coerce", utc=True)
        if s.notna().any():
            data_last = str(s.max())
    except Exception:
        pass
    rank_available = any(r.get("rank") is not None for r in rows)

    scan_json = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "scan_time": str(scan["scan_time"].max()),
        "data_through": data_last,
        "counts": counts,
        "total": len(rows),
        "rank_available": rank_available,
        "rows": rows,
    }
    (out_dir / "scan.json").write_text(
        json.dumps(scan_json, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    LOG.info("scan.json written (%s rows, %s charts)", len(rows), len(written))
    return 0


def _struct_label(r: dict) -> str:
    def b(v):
        try:
            return bool(v) if v is not None and v == v else False
        except Exception:
            return False
    lh, br = b(r.get("lower_high_confirmed")), b(r.get("swing_low_break"))
    hh, hl = b(r.get("higher_high_confirmed")), b(r.get("higher_low_confirmed"))
    if lh and br:
        return "LH+破位"
    if lh:
        return "LH"
    if br:
        return "破位"
    if hh and hl:
        return "HH+HL"
    return "-"


if __name__ == "__main__":
    sys.exit(main())
