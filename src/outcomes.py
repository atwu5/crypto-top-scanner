"""Scan outcomes backfill (spec §29).

对历史扫描结果，在行情数据允许时计算其后的实际走势：
- future_return_4h/12h/24h/48h
- future_max_drop_4h/12h/24h/48h   （相对扫描时价格的最大下跌）
- future_max_rise_4h/12h/24h/48h   （相对扫描时价格的最大上涨）

时间窗数据不足时该字段保持 NULL（不造假）。结果写入 DuckDB 表 scan_outcomes。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import pandas as pd

from .storage import Storage
from .utils import get_logger

LOG = get_logger("outcomes")

HORIZONS = [4, 12, 24, 48]
OUTCOME_COLS = (
    ["scan_time", "symbol", "anchor_time", "anchor_price"]
    + [f"future_return_{h}h" for h in HORIZONS]
    + [f"future_max_drop_{h}h" for h in HORIZONS]
    + [f"future_max_rise_{h}h" for h in HORIZONS]
)


def _window_stats(k1h: pd.DataFrame, t0: pd.Timestamp, price: float, hours: int):
    """Return (ret, max_drop, max_rise) or (None, None, None) if window incomplete."""
    if k1h is None or k1h.empty or not price:
        return None, None, None
    end = t0 + pd.Timedelta(hours=hours)
    win = k1h[(k1h["open_time"] > t0) & (k1h["open_time"] <= end)]
    # 要求窗口后有足够数据覆盖（允许 1 根未收盘）
    coverage = k1h[k1h["open_time"] > t0]
    if coverage.empty:
        return None, None, None
    last_bar = k1h["open_time"].max()
    if last_bar < end - pd.Timedelta(hours=2):
        return None, None, None
    if win.empty:
        return None, None, None
    ret = float(win.iloc[-1]["close"] / price - 1)
    max_drop = float(win["low"].min() / price - 1)
    max_rise = float(win["high"].max() / price - 1)
    return ret, max_drop, max_rise


def compute_outcomes(storage: Storage, cfg: Dict[str, Any]) -> int:
    """Recompute scan_outcomes for all recorded scans. Returns number of rows written."""
    try:
        scans = storage.query("SELECT DISTINCT scan_time FROM scan_results ORDER BY scan_time")
    except Exception as e:
        LOG.warning("scan_results unavailable: %s", e)
        return 0
    rows: List[Dict[str, Any]] = []
    for st in scans["scan_time"].tolist():
        try:
            batch = storage.query(
                "SELECT symbol, price, data_last_time FROM scan_results WHERE scan_time = ?", [st])
        except Exception as e:
            LOG.warning("scan batch %s failed: %s", st, e)
            continue
        for r in batch.itertuples():
            try:
                t0 = pd.Timestamp(r.data_last_time)
                price = float(r.price) if r.price is not None else None
            except Exception:
                continue
            if not price:
                continue
            k1h = storage.load_klines(r.symbol, "1h")
            rec = {"scan_time": st, "symbol": r.symbol, "anchor_time": t0, "anchor_price": price}
            any_val = False
            for h in HORIZONS:
                ret, drop, rise = _window_stats(k1h, t0, price, h)
                rec[f"future_return_{h}h"] = ret
                rec[f"future_max_drop_{h}h"] = drop
                rec[f"future_max_rise_{h}h"] = rise
                any_val = any_val or ret is not None
            if any_val:
                rows.append(rec)
    if not rows:
        LOG.info("no outcomes computable yet (data window not advanced far enough)")
        return 0
    df = pd.DataFrame(rows, columns=OUTCOME_COLS)

    def _write():
        con = storage.duck()
        con.register("out_rows", df)
        try:
            con.execute("CREATE OR REPLACE TABLE scan_outcomes AS SELECT * FROM out_rows")
        finally:
            con.unregister("out_rows")

    storage._with_write_retry(_write, "save_scan_outcomes")
    LOG.info("scan_outcomes computed: %s rows", len(df))
    return len(df)
