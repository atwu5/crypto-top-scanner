"""Technical indicators (pure pandas)."""
from __future__ import annotations

import numpy as np
import pandas as pd


def ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    out = 100 - (100 / (1 + rs))
    return out.fillna(50.0)


def macd(series: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    ema_fast = ema(series, fast)
    ema_slow = ema(series, slow)
    macd_line = ema_fast - ema_slow
    sig = ema(macd_line, signal)
    hist = macd_line - sig
    return pd.DataFrame({"macd": macd_line, "macd_signal": sig, "macd_hist": hist})


def add_indicators(df: pd.DataFrame, ema_fast: int = 20, ema_slow: int = 60, rsi_period: int = 14) -> pd.DataFrame:
    """Attach EMA / RSI / MACD columns to a kline DataFrame (sorted by open_time)."""
    out = df.copy()
    out["ema20"] = ema(out["close"], ema_fast)
    out["ema60"] = ema(out["close"], ema_slow)
    out["rsi14"] = rsi(out["close"], rsi_period)
    m = macd(out["close"])
    out["macd"] = m["macd"]
    out["macd_signal"] = m["macd_signal"]
    out["macd_hist"] = m["macd_hist"]
    return out


def pct_return(series: pd.Series, bars: int) -> float:
    """Return over the last ``bars`` periods (fraction, e.g. 0.25 = +25%)."""
    if len(series) <= bars or bars <= 0:
        return None
    prev = series.iloc[-1 - bars]
    if prev in (0, None) or pd.isna(prev):
        return None
    return float(series.iloc[-1] / prev - 1)


def slope_pct(series: pd.Series, bars: int = 3) -> float:
    """Mean per-bar % slope over last ``bars`` periods."""
    if len(series) <= bars:
        return None
    a = series.iloc[-1] / series.iloc[-1 - bars] - 1
    return float(a / bars)


def drawdown_from_high(series: pd.Series, bars: int, use: str = "close") -> float:
    if len(series) == 0:
        return None
    window = series.iloc[-bars:] if bars > 0 else series
    hi = window.max()
    if hi in (0, None) or pd.isna(hi):
        return None
    return float(series.iloc[-1] / hi - 1)


def rolling_ratio(series: pd.Series, window: int, baseline_cycles: int = 20,
                  agg: str = "mean") -> float:
    """Current ``window``-sum vs baseline of previous ``baseline_cycles`` sums.

    Used for volume ratios: e.g. current 4h volume vs avg/median of prior 20 4h volumes.
    """
    if len(series) < window + baseline_cycles * 1:
        return None
    rolls = series.rolling(window).sum().dropna()
    if len(rolls) < baseline_cycles + 1:
        return None
    current = rolls.iloc[-1]
    baseline = rolls.iloc[-(baseline_cycles + 1):-1]
    base = baseline.median() if agg == "median" else baseline.mean()
    if base in (0, None) or pd.isna(base) or base == 0:
        return None
    return float(current / base)
