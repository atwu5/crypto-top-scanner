"""Case analysis: config/cases.csv 样本横向对比（success vs false_top）。

python scripts/analyze_cases.py
- 每个样本提取 event_date ± 72 小时窗口的特征
- 输出 data/processed/case_analysis.csv（缺失数据保持 NULL，绝不造假）
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

from .features import _pump_and_rebound
from .indicators import add_indicators, pct_return
from .storage import Storage
from .structure import analyze_structure
from .utils import get_logger

LOG = get_logger("cases")

CASE_COLUMNS = [
    "symbol", "label", "event_date", "note",
    "return_24h", "return_72h", "volume_ratio", "volume_ratio_24h",
    "rank_before", "rank_after", "rank_change",
    "oi_change_24h", "funding",
    "first_macd_negative_time", "ema20_break", "ema20_break_time",
    "swing_low_break", "lower_high", "lower_low",
    "rebound_ratio", "new_high_after_weakness",
    "max_drawdown_4h", "max_drawdown_12h", "max_drawdown_24h", "max_drawdown_48h",
    "max_adverse_move_4h", "max_adverse_move_12h", "max_adverse_move_24h", "max_adverse_move_48h",
]


def load_cases(cfg: Dict[str, Any]) -> pd.DataFrame:
    p = Path(cfg["_project_root"]) / "config" / "cases.csv"
    df = pd.read_csv(p)
    df["event_date"] = pd.to_datetime(df["event_date"], utc=True)
    return df


def _close_before(df: pd.DataFrame, t: pd.Timestamp) -> Optional[float]:
    sub = df[df["open_time"] <= t]
    if sub.empty:
        return None
    return float(sub.iloc[-1]["close"])


def _min_low_after(df: pd.DataFrame, t: pd.Timestamp, hours: int) -> Optional[float]:
    sub = df[(df["open_time"] > t) & (df["open_time"] <= t + pd.Timedelta(hours=hours))]
    if sub.empty:
        return None
    return float(sub["low"].min())


def _max_high_after(df: pd.DataFrame, t: pd.Timestamp, hours: int) -> Optional[float]:
    sub = df[(df["open_time"] > t) & (df["open_time"] <= t + pd.Timedelta(hours=hours))]
    if sub.empty:
        return None
    return float(sub["high"].max())


def analyze_one(storage: Storage, symbol: str, event_date: pd.Timestamp,
                label: str, cfg: Dict[str, Any]) -> Dict[str, Any]:
    row: Dict[str, Any] = {c: None for c in CASE_COLUMNS}
    row["symbol"], row["label"] = symbol, label
    row["event_date"] = event_date.strftime("%Y-%m-%d")

    k4 = storage.load_klines(symbol, "4h")
    k1 = storage.load_klines(symbol, "1h")
    if k4.empty and k1.empty:
        row["note"] = "no local data"
        return row
    if k1.empty:
        k1 = k4
    ref = k1 if len(k1) else k4

    t0 = event_date
    coverage_start = ref["open_time"].min()
    coverage_end = ref["open_time"].max()
    if t0 > coverage_end + pd.Timedelta(hours=4) or t0 < coverage_start:
        row["note"] = f"event outside data coverage [{coverage_start} ~ {coverage_end}]"

    # ---- as-of features (≤ event date) ----
    close_at = _close_before(ref, t0)
    row["return_24h"] = _ret_before(ref, t0, 24)
    row["return_72h"] = _ret_before(ref, t0, 72)

    if not k4.empty:
        k4i = add_indicators(k4)
        sub4 = k4i[k4i["open_time"] <= t0]
        if len(sub4) > 25:
            qv = sub4["quote_volume"]
            cur = float(qv.tail(1).sum())
            base = float(qv.iloc[-21:-1].median()) if len(qv) > 21 else None
            row["volume_ratio"] = (cur / base) if base else None
        qv_all = k4i[k4i["open_time"] <= t0]["quote_volume"]
        if len(qv_all) >= 44:
            rolls = qv_all.rolling(6).sum().dropna()
            base = float(rolls.iloc[-21:-1].median())
            row["volume_ratio_24h"] = float(rolls.iloc[-1] / base) if base else None
    else:
        k4i = k4

    oi = storage.load_oi(symbol)
    if not oi.empty:
        cur = oi[oi["time"] <= t0]
        before = oi[oi["time"] <= t0 - pd.Timedelta(hours=24)]
        if len(cur) and len(before):
            c0 = float(cur.iloc[-1]["open_interest_value"])
            b0 = float(before.iloc[-1]["open_interest_value"])
            row["oi_change_24h"] = (c0 / b0 - 1) if b0 else None

    funding = storage.load_funding(symbol)
    if not funding.empty:
        fr = funding[funding["funding_time"] <= t0]
        if len(fr):
            row["funding"] = float(fr.iloc[-1]["funding_rate"])

    # ---- pump / MACD / EMA around the event window ----
    win_lo, win_hi = t0 - pd.Timedelta(hours=72), t0 + pd.Timedelta(hours=72)
    w4 = k4i[(k4i["open_time"] >= win_lo) & (k4i["open_time"] <= win_hi)] if not k4i.empty else k4
    if not w4.empty and "macd_hist" in w4.columns:
        iH = int(w4["high"].to_numpy().argmax())
        H_time = w4.iloc[iH]["open_time"]
        H = float(w4.iloc[iH]["high"])
        hist = w4["macd_hist"]
        neg = hist < 0
        cross = neg & (~neg.shift(1, fill_value=False))
        cross_times = w4.loc[cross, "open_time"]
        after_peak = cross_times[cross_times > H_time]
        if len(after_peak):
            row["first_macd_negative_time"] = str(after_peak.iloc[0])
        # ema break after pump
        ema = w4["ema20"]
        below = w4["close"] < ema
        cross_b = below & (~below.shift(1, fill_value=False))
        bt = w4.loc[cross_b, "open_time"]
        bt = bt[bt > H_time]
        if len(bt):
            row["ema20_break"] = True
            row["ema20_break_time"] = str(bt.iloc[0])
        elif len(w4):
            row["ema20_break"] = bool(w4.iloc[-1]["close"] < w4.iloc[-1]["ema20"])
        # new high after weakness
        if row["first_macd_negative_time"]:
            fnt = pd.Timestamp(row["first_macd_negative_time"])
            after = w4[w4["open_time"] >= fnt]
            row["new_high_after_weakness"] = bool(len(after) and float(after["high"].max()) >= H)
        # rebound
        pump = _pump_and_rebound(w4.reset_index(drop=True), cfg)
        row["rebound_ratio"] = pump.get("rebound_ratio")
    # structure over window
    st = analyze_structure(w4 if not w4.empty else k4, cfg) if not (k4.empty) else {}
    row["lower_high"] = st.get("lower_high_confirmed")
    row["lower_low"] = st.get("lower_low_confirmed")
    row["swing_low_break"] = st.get("swing_low_break")

    # ---- forward outcomes from event ----
    if close_at:
        for h in (4, 12, 24, 48):
            lo = _min_low_after(ref, t0, h)
            hi = _max_high_after(ref, t0, h)
            row[f"max_drawdown_{h}h"] = (lo / close_at - 1) if lo is not None else None
            row[f"max_adverse_move_{h}h"] = (hi / close_at - 1) if hi is not None else None
    return row


def _ret_before(df: pd.DataFrame, t: pd.Timestamp, hours: int) -> Optional[float]:
    cur = _close_before(df, t)
    prev = _close_before(df, t - pd.Timedelta(hours=hours))
    if cur is None or prev in (None, 0):
        return None
    return float(cur / prev - 1)


def analyze_cases(cfg: Dict[str, Any], print_report: bool = True) -> pd.DataFrame:
    storage = Storage(cfg)
    cases = load_cases(cfg)
    rows = []
    for r in cases.itertuples():
        LOG.info("analyzing %s (%s) @ %s", r.symbol, r.label, r.event_date.date())
        try:
            rows.append(analyze_one(storage, r.symbol, r.event_date, r.label, cfg))
        except Exception as e:  # one case must not kill the batch
            LOG.warning("case %s failed: %s", r.symbol, e)
            row = {c: None for c in CASE_COLUMNS}
            row.update({"symbol": r.symbol, "label": r.label,
                        "event_date": r.event_date.strftime("%Y-%m-%d"), "note": f"error: {e}"})
            rows.append(row)

    out = pd.DataFrame(rows, columns=CASE_COLUMNS)
    out_path = storage.processed / "case_analysis.csv"
    out.to_csv(out_path, index=False)
    try:
        storage.save_case_analysis(out)
    except Exception as e:
        LOG.warning("save case_analysis to duckdb failed: %s", e)
    LOG.info("case analysis saved: %s", out_path)

    if print_report:
        _print_comparison(out)
    return out


def _print_comparison(df: pd.DataFrame) -> None:
    from tabulate import tabulate

    print()
    print("=" * 100)
    print("CASE ANALYSIS — success vs false_top (event_date ±72h)")
    print("=" * 100)
    show_cols = ["symbol", "label", "event_date", "return_24h", "return_72h", "volume_ratio",
                 "rebound_ratio", "first_macd_negative_time", "max_drawdown_24h",
                 "max_adverse_move_24h", "note"]
    show = df[[c for c in show_cols if c in df.columns]].copy()
    for c in ("return_24h", "return_72h", "rebound_ratio", "max_drawdown_24h", "max_adverse_move_24h"):
        if c in show.columns:
            show[c] = show[c].map(lambda v: f"{v*100:.1f}%" if pd.notna(v) else "NULL")
    if "volume_ratio" in show.columns:
        show["volume_ratio"] = show["volume_ratio"].map(lambda v: f"{v:.1f}x" if pd.notna(v) else "NULL")
    print(tabulate(show, headers="keys", tablefmt="psql", showindex=False))
    print()

    numeric = ["return_24h", "return_72h", "volume_ratio", "rebound_ratio",
               "max_drawdown_24h", "max_adverse_move_24h"]
    agg = df.groupby("label")[numeric].median(numeric_only=True)
    print("Median comparison (success vs false_top):")
    print(tabulate(agg, headers="keys", tablefmt="psql"))
    print()
    print("提示：以上仅为现有样本的初步对比（样本量极小），用于持续积累和回测，"
          "不构成任何已验证的交易策略。")
