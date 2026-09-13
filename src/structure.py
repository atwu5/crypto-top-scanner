"""Price structure analysis: swing pivots, HH/HL/LH/LL, breaks, signal timeline.

MVP 使用简单 Pivot 算法（左右各 N 根 K 线）识别 Swing High / Low，
并据此判定市场结构（Higher High / Higher Low / Lower High / Lower Low）。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import pandas as pd


def find_pivots(df: pd.DataFrame, left: int = 2, right: int = 2) -> pd.DataFrame:
    """Find swing pivots. Returns DataFrame[idx, time, price, kind('H'|'L')]."""
    highs: List[Dict[str, Any]] = []
    lows: List[Dict[str, Any]] = []
    if len(df) < left + right + 1:
        return pd.DataFrame(columns=["idx", "time", "price", "kind"])
    hv = df["high"].to_numpy()
    lv = df["low"].to_numpy()
    tv = df["open_time"].tolist()
    n = len(df)
    for i in range(left, n - right):
        win_h = hv[i - left: i + right + 1]
        if hv[i] == win_h.max() and (win_h == hv[i]).sum() == 1:
            highs.append({"idx": i, "time": tv[i], "price": float(hv[i]), "kind": "H"})
        win_l = lv[i - left: i + right + 1]
        if lv[i] == win_l.min() and (win_l == lv[i]).sum() == 1:
            lows.append({"idx": i, "time": tv[i], "price": float(lv[i]), "kind": "L"})
    out = pd.DataFrame(highs + lows)
    if out.empty:
        return pd.DataFrame(columns=["idx", "time", "price", "kind"])
    return out.sort_values("idx").reset_index(drop=True)


def _consecutive(pivots: pd.DataFrame, kind: str) -> List[Dict[str, Any]]:
    rows = pivots[pivots["kind"] == kind].to_dict("records") if not pivots.empty else []
    return rows


def analyze_structure(df: pd.DataFrame, cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Structure features on the (4h) kline DataFrame.

    Returns dict with swing info + booleans used by scoring & UI.
    """
    sk = cfg.get("structure", {})
    left, right = int(sk.get("pivot_left", 2)), int(sk.get("pivot_right", 2))
    lookback = int(sk.get("swing_lookback_bars", 120))
    sub = df.tail(lookback).reset_index(drop=True)
    pivots = find_pivots(sub, left, right)

    res: Dict[str, Any] = {
        "pivots": pivots.to_dict("records") if not pivots.empty else [],
        "last_swing_high": None,
        "last_swing_low": None,
        "prev_swing_high": None,
        "prev_swing_low": None,
        "lower_high_confirmed": False,
        "lower_low_confirmed": False,
        "higher_high_confirmed": False,
        "higher_low_confirmed": False,
        "swing_low_break": False,
        "swing_low_break_time": None,
        "second_breakdown": False,
        "second_breakdown_time": None,
        "market_structure_broken": False,
    }
    if pivots.empty:
        return res

    highs = _consecutive(pivots, "H")
    lows = _consecutive(pivots, "L")
    if len(highs) >= 1:
        res["last_swing_high"] = {"time": str(highs[-1]["time"]), "price": highs[-1]["price"]}
    if len(highs) >= 2:
        res["prev_swing_high"] = {"time": str(highs[-2]["time"]), "price": highs[-2]["price"]}
        res["lower_high_confirmed"] = highs[-1]["price"] < highs[-2]["price"]
        res["higher_high_confirmed"] = highs[-1]["price"] > highs[-2]["price"]
    if len(lows) >= 1:
        res["last_swing_low"] = {"time": str(lows[-1]["time"]), "price": lows[-1]["price"]}
    if len(lows) >= 2:
        res["prev_swing_low"] = {"time": str(lows[-2]["time"]), "price": lows[-2]["price"]}
        res["lower_low_confirmed"] = lows[-1]["price"] < lows[-2]["price"]
        res["higher_low_confirmed"] = lows[-1]["price"] > lows[-2]["price"]

    # ---- find breaks of swing lows (close below a swing low after it formed) ----
    closes = sub["close"]
    times = sub["open_time"]
    broken: List[Dict[str, Any]] = []
    for low in reversed(lows[-6:]):
        after = sub[sub["open_time"] > low["time"]]
        if after.empty:
            continue
        below = after[after["close"] < low["price"]]
        if not below.empty:
            first_break_time = below.iloc[0]["open_time"]
            broken.append({"low_time": low["time"], "low": low["price"], "break_time": first_break_time})
    if broken:
        # most recent broken low
        broken_sorted = sorted(broken, key=lambda x: x["break_time"])
        res["swing_low_break"] = True
        res["swing_low_break_time"] = str(broken_sorted[-1]["break_time"])
        if len(broken_sorted) >= 2:
            res["second_breakdown"] = True
            res["second_breakdown_time"] = str(broken_sorted[-1]["break_time"])

    res["market_structure_broken"] = bool(res["lower_high_confirmed"] and res["swing_low_break"])
    return res


def build_signal_timeline(df: pd.DataFrame, structure: Dict[str, Any],
                          pump: Optional[Dict[str, Any]] = None) -> List[Dict[str, str]]:
    """Best-effort event timeline for charts: FIRST WEAKNESS / LOWER HIGH / BREAKDOWN / SHORT READY.

    导出为 [{time, label}] 列表，供前端在 K 线图上打点。
    """
    events: List[Dict[str, str]] = []
    if df is None or df.empty:
        return events

    def add(t, label):
        if t is None:
            return
        events.append({"time": str(t), "label": label})

    # FIRST WEAKNESS: 第一根 MACD histogram 由非负转负的 K 线（暴涨段之后）
    if "macd_hist" in df.columns:
        h = df["macd_hist"]
        neg = h < 0
        cross = neg & (~neg.shift(1, fill_value=False))
        pump_time = None
        if pump and pump.get("H_time"):
            pump_time = pd.Timestamp(pump["H_time"])
        for idx in df.index[cross][:5]:
            t = df.loc[idx, "open_time"]
            if pump_time is not None and t < pump_time - pd.Timedelta(hours=12):
                continue
            add(t, "FIRST WEAKNESS")
            break

    # LOWER HIGH
    lh = structure.get("last_swing_high")
    if structure.get("lower_high_confirmed") and lh:
        add(lh["time"], "LOWER HIGH")

    # BREAKDOWN
    if structure.get("swing_low_break") and structure.get("swing_low_break_time"):
        add(structure["swing_low_break_time"], "BREAKDOWN")

    # SHORT READY ≈ 结构破坏 + 反抽失败之后再次下破（近似：第二次破位时间）
    if structure.get("second_breakdown") and structure.get("second_breakdown_time"):
        add(structure["second_breakdown_time"], "SHORT READY")
    elif structure.get("market_structure_broken") and structure.get("swing_low_break_time"):
        # 无二次破位时，用 LH+破位完成的时间近似标注
        lh_t = pd.Timestamp(lh["time"]) if lh else None
        bk_t = pd.Timestamp(structure["swing_low_break_time"])
        t = max(filter(None, [lh_t, bk_t])) if (lh_t or bk_t) else None
        add(t, "SHORT READY" if t is not None else None)

    # REBOUND HIGH marker
    if pump and pump.get("R_time"):
        add(pump["R_time"], "REBOUND HIGH")
    if pump and pump.get("H_time"):
        add(pump["H_time"], "PUMP HIGH")

    return events
