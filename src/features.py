"""Per-symbol feature computation (the "what is happening" layer).

输入：本地 K 线 (15m/1h/4h) + Funding + OI/持仓 + 市值排名上下文
输出：一个扁平的 feature dict（含 None-safe 处理；缺数据 = None，不伪造）
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

import numpy as np
import pandas as pd

from .indicators import add_indicators, pct_return, rolling_ratio, slope_pct
from .structure import analyze_structure, build_signal_timeline
from .utils import get_logger

LOG = get_logger("features")


def _last(df: pd.DataFrame, col: str):
    if df is None or df.empty or col not in df.columns:
        return None
    s = df[col].dropna()
    return float(s.iloc[-1]) if len(s) else None


def _value_hours_ago(df: pd.DataFrame, time_col: str, value_col: str,
                     anchor: pd.Timestamp, hours: float) -> Optional[float]:
    """Last value at or before (anchor - hours). None if coverage insufficient."""
    if df is None or df.empty:
        return None
    sub = df[df[time_col] <= anchor - pd.Timedelta(hours=hours)]
    if sub.empty:
        return None
    # 要求覆盖足够（最早数据早于目标时间 2 小时以上）
    earliest = df[time_col].min()
    if earliest > anchor - pd.Timedelta(hours=hours + 2):
        return None
    v = sub[value_col].dropna()
    return float(v.iloc[-1]) if len(v) else None


def _change(df: pd.DataFrame, time_col: str, value_col: str, anchor, hours: float) -> Optional[float]:
    cur = _last(df, value_col)
    ago = _value_hours_ago(df, time_col, value_col, anchor, hours)
    if cur is None or ago in (None, 0):
        return None
    return float(cur / ago - 1)


def _norm_cols(df: Optional[pd.DataFrame]) -> pd.DataFrame:
    if df is None:
        return pd.DataFrame()
    return df.sort_values("open_time").reset_index(drop=True) if "open_time" in df.columns else df


def compute_features(symbol: str, data: Dict[str, pd.DataFrame],
                     rank_ctx: Dict[str, Any], cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Compute the full feature row for one symbol."""
    k1h = _norm_cols(data.get("1h"))
    k4h = _norm_cols(data.get("4h"))
    k15 = _norm_cols(data.get("15m"))
    funding = data.get("funding")
    oi = data.get("oi")
    taker = data.get("taker")

    f: Dict[str, Any] = {"symbol": symbol}
    if k4h.empty and k1h.empty:
        raise ValueError("no kline data")

    # ---------- price / returns ----------
    price = _last(k4h, "close") or _last(k1h, "close")
    f["price"] = price

    ret_src = k1h if len(k1h) > 200 else k4h
    per_bar = "1h" if ret_src is k1h else "4h"
    scale = 1 if per_bar == "1h" else 4  # convert hours -> bars
    close = ret_src["close"] if not ret_src.empty else pd.Series(dtype=float)

    def ret(hours: int):
        return pct_return(close, hours * scale) if len(close) else None

    f["return_1h"] = ret(1)
    f["return_4h"] = ret(4)
    f["return_12h"] = ret(12)
    f["return_24h"] = ret(24)
    f["return_72h"] = ret(72)
    f["return_7d"] = ret(168)
    f["momentum_1h"] = f["return_1h"]
    f["momentum_4h"] = f["return_4h"]
    f["momentum_12h"] = f["return_12h"]
    f["momentum_24h"] = f["return_24h"]

    if not ret_src.empty:
        hi = ret_src["high"]
        f["drawdown_from_72h_high"] = pct_from_window(close, hi, 72 * scale)
        f["drawdown_from_7d_high"] = pct_from_window(close, hi, 168 * scale)
    else:
        f["drawdown_from_72h_high"] = None
        f["drawdown_from_7d_high"] = None

    # ---------- momentum deceleration ----------
    f["momentum_deceleration_score"] = _decel_score(f, cfg)

    # ---------- volume ----------
    if not ret_src.empty and "quote_volume" in ret_src.columns:
        qv = ret_src["quote_volume"]
        f["volume_1h"] = float(qv.tail(1 * scale).sum()) if len(qv) >= scale else None
        f["volume_4h"] = float(qv.tail(4 * scale).sum()) if len(qv) >= 4 * scale else None
        f["volume_24h"] = float(qv.tail(24 * scale).sum()) if len(qv) >= 24 * scale else None
        vcfg = cfg.get("volume", {})
        cycles = int(vcfg.get("baseline_cycles", 20))
        f["volume_ratio_4h"] = rolling_ratio(qv, 4 * scale, cycles, "mean")
        f["volume_ratio_4h_median"] = rolling_ratio(qv, 4 * scale, cycles, "median")
        f["volume_ratio_24h"] = rolling_ratio(qv, 24 * scale, cycles, "mean")
        f["volume_ratio_24h_median"] = rolling_ratio(qv, 24 * scale, cycles, "median")
    else:
        for k in ("volume_1h", "volume_4h", "volume_24h", "volume_ratio_4h",
                  "volume_ratio_4h_median", "volume_ratio_24h", "volume_ratio_24h_median"):
            f[k] = None

    # volume peak on 4h
    peak_days = int(cfg.get("volume", {}).get("spike_peak_days", 7))
    if not k4h.empty:
        win = k4h.tail(peak_days * 6)
        if len(win) > 3:
            i_v = int(win["quote_volume"].to_numpy().argmax())
            f["volume_peak_time"] = str(win.iloc[i_v]["open_time"])
            peak_vol = float(win.iloc[i_v]["quote_volume"])
            last_vol = float(win.iloc[-1]["quote_volume"])
            f["volume_since_peak_change"] = (last_vol / peak_vol - 1) if peak_vol else None
            # price/volume divergence: price high formed after volume peak with (much) lower volume
            i_h = int(win["close"].to_numpy().argmax())
            vol_at_h = float(win.iloc[i_h]["quote_volume"])
            div_ratio = float(cfg.get("volume", {}).get("divergence_vol_ratio", 0.6))
            near_high = f["price"] is not None and win["close"].max() > 0 and f["price"] >= 0.90 * float(win["close"].max())
            f["price_volume_divergence"] = bool(i_h > i_v and peak_vol > 0 and vol_at_h < div_ratio * peak_vol and near_high)
            vals = win["quote_volume"].tail(3).tolist()
            f["volume_declining_3bars"] = bool(len(vals) == 3 and vals[0] > vals[1] > vals[2])
        else:
            f["volume_peak_time"] = None
            f["volume_since_peak_change"] = None
            f["price_volume_divergence"] = False
            f["volume_declining_3bars"] = False
    else:
        f["volume_peak_time"] = None
        f["volume_since_peak_change"] = None
        f["price_volume_divergence"] = False
        f["volume_declining_3bars"] = False

    # ---------- OI ----------
    anchor = None
    if oi is not None and not oi.empty and "time" in oi.columns:
        anchor = pd.Timestamp(oi["time"].max())
    if anchor is not None:
        f["oi_change_1h"] = _change(oi, "time", "open_interest_value", anchor, 1)
        f["oi_change_4h"] = _change(oi, "time", "open_interest_value", anchor, 4)
        f["oi_change_24h"] = _change(oi, "time", "open_interest_value", anchor, 24)
        f["oi_change_72h"] = _change(oi, "time", "open_interest_value", anchor, 72)
        f["oi_last"] = _last(oi, "open_interest_value")
        f["oi_as_of"] = str(anchor)
    else:
        f["oi_change_1h"] = f["oi_change_4h"] = f["oi_change_24h"] = f["oi_change_72h"] = None
        f["oi_last"] = None
        f["oi_as_of"] = None

    price_down_th = float(cfg.get("oi", {}).get("price_down_threshold", -0.05))
    r24 = f.get("return_24h")
    oi24 = f.get("oi_change_24h")
    f["price_down_oi_down"] = bool(r24 is not None and oi24 is not None and r24 < price_down_th and oi24 < 0)
    f["price_down_oi_up"] = bool(r24 is not None and oi24 is not None and r24 < price_down_th and oi24 > 0)

    # taker ratio summary
    if taker is not None and not taker.empty and "taker_ratio" in taker.columns:
        f["taker_ratio_last"] = _last(taker, "taker_ratio")
        tr = taker["taker_ratio"].dropna().tail(24 * 12)  # 5-min 数据 -> 24h
        f["taker_ratio_avg"] = float(tr.mean()) if len(tr) else None
    else:
        f["taker_ratio_last"] = None
        f["taker_ratio_avg"] = None

    # ---------- funding ----------
    if funding is not None and not funding.empty and "funding_rate" in funding.columns:
        fr = funding["funding_rate"].dropna()
        if len(fr):
            cur = float(fr.iloc[-1])
            f["funding_current"] = cur
            f["funding_time"] = str(funding["funding_time"].iloc[-1])
            f["funding_percentile"] = float((fr <= cur).mean())
            std = float(fr.std())
            f["funding_zscore"] = float((cur - fr.mean()) / std) if std > 0 else None
        else:
            f["funding_current"] = f["funding_time"] = f["funding_percentile"] = f["funding_zscore"] = None
    else:
        f["funding_current"] = f["funding_time"] = f["funding_percentile"] = f["funding_zscore"] = None

    # ---------- rank (nullable) ----------
    for k in ("rank_now", "rank_24h", "rank_72h", "rank_7d", "rank_change_abs",
              "rank_improvement_pct", "rank_breakout", "rank_near_target", "rank_as_of"):
        f[k] = rank_ctx.get(k) if rank_ctx else None

    # ---------- 4h indicators ----------
    if not k4h.empty:
        k4i = add_indicators(k4h)
        last = k4i.iloc[-1]
        f["ema20"] = float(last["ema20"]) if pd.notna(last["ema20"]) else None
        f["ema60"] = float(last["ema60"]) if pd.notna(last["ema60"]) else None
        f["close_below_ema20"] = bool(f["price"] is not None and f["ema20"] is not None and f["price"] < f["ema20"])
        f["ema20_slope"] = slope_pct(k4i["ema20"], 3)
        f["ema60_slope"] = slope_pct(k4i["ema60"], 3)
        f["macd"] = float(last["macd"]) if pd.notna(last["macd"]) else None
        f["macd_signal"] = float(last["macd_signal"]) if pd.notna(last["macd_signal"]) else None
        f["macd_hist"] = float(last["macd_hist"]) if pd.notna(last["macd_hist"]) else None
        f["rsi14"] = float(last["rsi14"]) if pd.notna(last["rsi14"]) else None
        hist = k4i["macd_hist"]
        neg = hist < 0
        cross = neg & (~neg.shift(1, fill_value=False))
        neg_times = k4i.loc[cross, "open_time"]
        f["macd_first_negative_time"] = str(neg_times.iloc[-1]) if len(neg_times) else None
        f["macd_negative_bars"] = int((~neg).iloc[::-1].cumsum().eq(0).sum()) if f["macd_hist"] is not None and f["macd_hist"] < 0 else 0
        # ema20 reclaim: 最近 6 根内价格从下方回到上方，且 ema20 上升
        recent = k4i.tail(6)
        reclaimed = False
        ema20 = k4i["ema20"]
        for i in range(max(1, len(k4i) - 6), len(k4i)):
            if k4i["close"].iloc[i] > ema20.iloc[i] and k4i["close"].iloc[i - 1] <= ema20.iloc[i - 1]:
                reclaimed = True
        f["ema20_reclaimed_recent"] = bool(reclaimed and (f["ema20_slope"] or 0) > 0)
        f["price_new_high_recent"] = _new_high_recent(k4i, cfg)
        k4i_full = k4i
    else:
        for k in ("ema20", "ema60", "close_below_ema20", "ema20_slope", "ema60_slope",
                  "macd", "macd_signal", "macd_hist", "rsi14", "macd_first_negative_time",
                  "macd_negative_bars", "ema20_reclaimed_recent", "price_new_high_recent"):
            f[k] = None
        k4i_full = k4h

    # ---------- structure ----------
    st = analyze_structure(k4i_full if not k4i_full.empty else k4h, cfg)
    f["structure"] = {k: v for k, v in st.items() if k != "pivots"}
    f["pivots"] = st.get("pivots", [])
    for k in ("lower_high_confirmed", "lower_low_confirmed", "higher_high_confirmed",
              "higher_low_confirmed", "swing_low_break", "second_breakdown",
              "market_structure_broken", "swing_low_break_time", "second_breakdown_time",
              "last_swing_high", "last_swing_low"):
        f[k] = st.get(k)

    # ---------- rebound / pump ----------
    pump = _pump_and_rebound(k4i_full, cfg)
    f["pump"] = pump
    f["rebound_ratio"] = pump.get("rebound_ratio")
    f["rebound_failed"] = pump.get("rebound_failed")
    f["rebound_new_high"] = pump.get("rebound_new_high")
    f["rebound_volume_decline"] = pump.get("rebound_volume_decline")
    f["rebound_pending"] = pump.get("rebound_pending")
    f["pump_leg_move_pct"] = pump.get("leg_move_pct")
    f["pump_high"] = pump.get("H")
    f["pump_high_time"] = pump.get("H_time")

    # ---------- pump context gate（近期暴涨背景） ----------
    _pump_context(f, k4i_full, cfg)

    # ---------- signal timeline ----------
    f["timeline"] = build_signal_timeline(k4i_full, st, pump)

    # ---------- data freshness ----------
    last_times = []
    for df in (k15, k1h, k4h):
        if df is not None and not df.empty:
            last_times.append(df["open_time"].max())
    f["data_last_time"] = str(max(last_times)) if last_times else None
    f["k4h_last"] = str(k4h["open_time"].max()) if not k4h.empty else None
    return f


def _pump_context(f: Dict[str, Any], k4: pd.DataFrame, cfg: Dict[str, Any]) -> None:
    """近期暴涨背景（was_extended）+ 转弱时效性（weakness_recent）。"""
    pg = cfg.get("pump_gate", {})
    lookback_bars = int(float(pg.get("lookback_days", 14)) * 6)  # 4h bars
    f["max_return_24h_14d"] = None
    f["max_return_72h_14d"] = None
    f["range_30d"] = None
    f["drawdown_from_30d_high"] = None
    f["was_extended"] = None
    f["days_since_breakdown"] = None
    f["days_since_macd_negative"] = None
    f["weakness_recent"] = None
    if k4 is None or k4.empty:
        return
    c = k4["close"]
    r24 = (c / c.shift(6) - 1)
    r72 = (c / c.shift(18) - 1)
    n24 = max(1, min(lookback_bars, len(c) - 7))
    n72 = max(1, min(lookback_bars, len(c) - 19))
    v24 = r24.tail(n24).max()
    v72 = r72.tail(n72).max()
    f["max_return_24h_14d"] = None if pd.isna(v24) else float(v24)
    f["max_return_72h_14d"] = None if pd.isna(v72) else float(v72)
    w30 = k4.tail(180)
    if len(w30) > 10:
        rng = float(w30["high"].max() / w30["low"].min() - 1) if w30["low"].min() > 0 else None
        f["range_30d"] = rng
        hi30 = float(w30["high"].max())
        if hi30 > 0 and f.get("price"):
            f["drawdown_from_30d_high"] = float(f["price"] / hi30 - 1)

    mr24 = f.get("max_return_24h_14d") or 0
    mr72 = f.get("max_return_72h_14d") or 0
    f["was_extended"] = bool(mr24 >= float(pg.get("max_return_24h_14d", 0.30))
                             or mr72 >= float(pg.get("max_return_72h_14d", 0.35)))

    last_time = pd.Timestamp(k4["open_time"].max())

    def days_since(ts: Optional[str]):
        if not ts:
            return None
        try:
            return float((last_time - pd.Timestamp(ts)).total_seconds() / 86400.0)
        except Exception:
            return None

    f["days_since_breakdown"] = days_since(f.get("swing_low_break_time"))
    f["days_since_macd_negative"] = days_since(f.get("macd_first_negative_time"))
    win_days = float(pg.get("weakness_recent_days", 14))
    wr = False
    if f["days_since_breakdown"] is not None and f["days_since_breakdown"] <= win_days:
        wr = True
    if f["days_since_macd_negative"] is not None and f["days_since_macd_negative"] <= min(win_days, 10):
        wr = True
    f["weakness_recent"] = bool(wr)


def pct_from_window(close: pd.Series, high: pd.Series, bars: int) -> Optional[float]:
    if len(close) < 2:
        return None
    n = min(bars, len(close))
    hi = high.tail(n).max()
    if hi in (0, None) or pd.isna(hi):
        return None
    return float(close.iloc[-1] / hi - 1)


def _decel_score(f: Dict[str, Any], cfg: Dict[str, Any]) -> Optional[float]:
    r = {1: f.get("return_1h"), 4: f.get("return_4h"), 12: f.get("return_12h"), 24: f.get("return_24h")}
    if r[24] is None or r[24] < 0.1:
        return None
    conds = cfg.get("momentum", {}).get("decel_conditions", [[1, 24, 0.0], [4, 12, 0.5], [12, 24, 0.6], [4, 24, 0.2]])
    weights = cfg.get("momentum", {}).get("decel_weights", [0.35, 0.30, 0.25, 0.10])
    score = 0.0
    wsum = 0.0
    for (a, b, ratio), w in zip(conds, weights):
        ra, rb = r.get(int(a)), r.get(int(b))
        wsum += w
        if rb is None or rb <= 0.05 or ra is None:
            continue
        if ra < rb * float(ratio):
            score += w
    return round(100 * score / wsum, 1) if wsum else None


def _new_high_recent(k4i: pd.DataFrame, cfg: Dict[str, Any]) -> bool:
    hours = int(cfg.get("blockers", {}).get("new_high_lookback_hours", 24))
    bars = max(1, hours // 4)
    if len(k4i) < bars + 1:
        return False
    recent_high = k4i["high"].tail(bars).max()
    return bool(k4i["close"].iloc[-1] >= recent_high * 0.999)


def _pump_and_rebound(k4i: pd.DataFrame, cfg: Dict[str, Any]) -> Dict[str, Any]:
    rc = cfg.get("rebound", {})
    res: Dict[str, Any] = {
        "H": None, "H_time": None, "L": None, "L_time": None, "R": None, "R_time": None,
        "rebound_ratio": None, "rebound_failed": None, "rebound_new_high": None,
        "rebound_volume_decline": None, "rebound_pending": None,
        "leg_move_pct": None, "qualified": False, "vol_ratio_rebound": None,
    }
    lookback_bars = int(rc.get("pump_lookback_hours", 336) // 4)
    if k4i is None or k4i.empty or len(k4i) < 10:
        return res
    win = k4i.tail(lookback_bars).reset_index(drop=True)
    iH = int(win["high"].to_numpy().argmax())
    H = float(win.iloc[iH]["high"])
    res["H"], res["H_time"] = H, str(win.iloc[iH]["open_time"])
    if iH >= len(win) - 2:
        # 高点太新：还没有回落 → 无反弹信息
        return res
    after_h = win.iloc[iH + 1:]
    iL_rel = int(after_h["low"].to_numpy().argmin())
    L = float(after_h.iloc[iL_rel]["low"])
    L_time = after_h.iloc[iL_rel]["open_time"]
    iL = iH + 1 + iL_rel
    res["L"], res["L_time"] = L, str(L_time)
    if L <= 0:
        return res
    leg = H / L - 1
    res["leg_move_pct"] = float(leg)
    res["qualified"] = bool(leg >= float(rc.get("min_leg_move_pct", 0.40)))
    after_l = win.iloc[iL + 1:] if iL + 1 < len(win) else win.iloc[0:0]
    if after_l.empty:
        return res
    iR_rel = int(after_l["high"].to_numpy().argmax())
    R = float(after_l.iloc[iR_rel]["high"])
    res["R"], res["R_time"] = R, str(after_l.iloc[iR_rel]["open_time"])
    res["rebound_pending"] = bool(len(after_l) < 3)
    if H > L:
        res["rebound_ratio"] = float((R - L) / (H - L))
    res["rebound_new_high"] = bool(R >= H)
    ratio = res["rebound_ratio"]
    res["rebound_failed"] = bool(ratio is not None and R < H and ratio <= float(rc.get("failed_rebound_ratio", 0.50)))
    # rebound volume vs pump-top volume
    try:
        pump_slice = win.iloc[max(0, iH - 6): iH + 1]
        reb_slice = after_l
        vol_pump = float(pump_slice["quote_volume"].mean())
        vol_reb = float(reb_slice["quote_volume"].mean())
        if vol_pump > 0:
            res["vol_ratio_rebound"] = float(vol_reb / vol_pump)
            res["rebound_volume_decline"] = bool(vol_reb < 0.6 * vol_pump)
    except Exception:
        pass
    return res
