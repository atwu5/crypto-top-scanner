"""Scoring & state machine.

- Overheat Score (0-100)：是否进入异常暴涨阶段
- Reversal Score (0-100)：顶部反转证据强度（结构 > MACD）
- Short Blockers：阻止 SHORT READY 的硬条件
- 状态机：NORMAL/HOT/EXTREME/TOP_WATCH/FIRST_WEAKNESS/REVERSAL_CONFIRMING/SHORT_READY/SHORT_TREND/INVALIDATED

每个结论都可解释：函数同时返回 原因行（✓/✗）与阻断原因，供 CLI / UI 展示。
MACD 第一次转弱绝不单独触发 SHORT READY（只能进入 TOP WATCH）。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

STATUS_ORDER = [
    "SHORT_TREND", "SHORT_READY", "REVERSAL_CONFIRMING", "TOP_WATCH",
    "FIRST_WEAKNESS", "EXTREME", "HOT", "INVALIDATED", "NORMAL",
]


def _scale(v: Optional[float], watch: float, extreme: float,
           super_: Optional[float] = None) -> Optional[float]:
    """Piecewise scale: 0..0.5 (below watch) .. 0.85 (extreme) .. 1.0 (super)."""
    if v is None:
        return None
    if watch <= 0:
        watch = 1e-9
    if v < watch:
        return max(0.0, 0.5 * (v / watch))
    if v < extreme:
        return 0.5 + 0.35 * (v - watch) / max(1e-9, (extreme - watch))
    top = super_ if super_ is not None else extreme * 1.5
    if v >= top:
        return 1.0
    return 0.85 + 0.15 * (v - extreme) / max(1e-9, (top - extreme))


def _band(s: Optional[float]) -> str:
    if s is None:
        return "na"
    if s >= 0.85:
        return "extreme"
    if s >= 0.5:
        return "watch"
    return "low"


# ===========================================================================
# Overheat
# ===========================================================================
def overheat_score(f: Dict[str, Any], cfg: Dict[str, Any]) -> Dict[str, Any]:
    oh = cfg.get("overheat", {})
    weights = oh.get("weights", {})

    vol_ratio = f.get("volume_ratio_4h")
    if vol_ratio is None:
        vol_ratio = f.get("volume_ratio_4h_median")

    comps: List[Dict[str, Any]] = [
        _comp("return_24h", "24H涨幅", f.get("return_24h"),
              _scale(f.get("return_24h"), oh.get("return_24h_watch", 0.6),
                     oh.get("return_24h_extreme", 1.5), oh.get("return_24h_super_extreme", 2.0)),
              weights.get("return_24h", 0.2), fmt="pct"),
        _comp("return_72h", "72H涨幅", f.get("return_72h"),
              _scale(f.get("return_72h"), oh.get("return_72h_watch", 1.0),
                     oh.get("return_72h_extreme", 2.5)),
              weights.get("return_72h", 0.15), fmt="pct"),
        _comp("volume_ratio", "成交量倍数(4H)", vol_ratio,
              _scale(vol_ratio, oh.get("volume_ratio_watch", 3), oh.get("volume_ratio_extreme", 5)),
              weights.get("volume_ratio", 0.15), fmt="x"),
        _comp("oi_change", "OI 24H变化", f.get("oi_change_24h"),
              _scale(f.get("oi_change_24h"), oh.get("oi_change_24h_watch", 0.3),
                     oh.get("oi_change_24h_extreme", 0.8)),
              weights.get("oi_change", 0.12), fmt="pct"),
        _comp("funding", "Funding 分位", f.get("funding_percentile"),
              _scale(f.get("funding_percentile"), oh.get("funding_percentile_watch", 0.9),
                     oh.get("funding_percentile_extreme", 0.98)),
              weights.get("funding", 0.10), fmt="pct"),
        _comp("rank", "市值排名提升", f.get("rank_improvement_pct"),
              _scale(f.get("rank_improvement_pct"), oh.get("rank_improvement_pct", 0.5), 0.8, 0.95),
              weights.get("rank", 0.10), fmt="pct"),
        _comp("deceleration", "上涨速度衰减", f.get("momentum_deceleration_score"),
              _scale(f.get("momentum_deceleration_score"), oh.get("decel_watch", 40),
                     oh.get("decel_extreme", 70)),
              weights.get("deceleration", 0.10), fmt="num"),
    ]

    num = 0.0
    wsum = 0.0
    missing: List[str] = []
    for c in comps:
        if c["scale"] is None:
            missing.append(c["key"])
            continue
        num += c["weight"] * c["scale"]
        wsum += c["weight"]
    score = round(100 * num / wsum, 1) if wsum > 0 else None
    return {"score": score, "components": comps, "missing": missing}


def _comp(key: str, label: str, value, scale: Optional[float], weight: float,
          fmt: str = "num", note: str = "") -> Dict[str, Any]:
    return {
        "key": key, "label": label, "value": value, "scale": scale,
        "weight": weight, "band": _band(scale), "fmt": fmt, "note": note,
        "points": (round(weight * scale * 100, 1) if scale is not None else None),
    }


# ===========================================================================
# Reversal
# ===========================================================================
def reversal_score(f: Dict[str, Any], cfg: Dict[str, Any]) -> Dict[str, Any]:
    rv = cfg.get("reversal", {})
    w = rv.get("weights", {})
    ratio = f.get("rebound_ratio")
    strong = float(cfg.get("rebound", {}).get("strong_rebound_ratio", 0.7))
    failed = float(cfg.get("rebound", {}).get("failed_rebound_ratio", 0.5))
    qualified = bool(f.get("pump", {}).get("qualified"))

    def check(key, label, cond, weight, detail=""):
        scale = None if cond is None else (1.0 if cond else 0.0)
        return _comp(key, label, cond, scale, weight, fmt="bool", note=detail)

    comps = [
        check("macd_negative", "4H MACD 转负", (f.get("macd_hist") is not None and f.get("macd_hist") < 0),
              w.get("macd_negative", 0.08)),
        check("ema20_break", "跌破 EMA20", f.get("close_below_ema20"), w.get("ema20_break", 0.10)),
        check("swing_low_break", "跌破前一个 Swing Low", f.get("swing_low_break"),
              w.get("swing_low_break", 0.16)),
        check("lower_high", "Lower High 确认", f.get("lower_high_confirmed"), w.get("lower_high", 0.16)),
        check("lower_low", "Lower Low 确认", f.get("lower_low_confirmed"), w.get("lower_low", 0.12)),
        check("rebound_failed", f"反抽失败(≤{int(failed*100)}%)",
              (qualified and ratio is not None and ratio <= failed), w.get("rebound_failed", 0.10)),
        check("rebound_weak", f"反抽较弱(<{int(strong*100)}%)",
              (qualified and ratio is not None and failed < ratio <= strong), w.get("rebound_weak", 0.06)),
        check("rebound_volume_decline", "反抽成交量下降", f.get("rebound_volume_decline"),
              w.get("rebound_volume_decline", 0.06)),
        check("price_down_oi_down", "Price↓ + OI↓", f.get("price_down_oi_down"),
              w.get("price_down_oi_down", 0.08)),
        check("price_down_oi_up", "Price↓ + OI↑", f.get("price_down_oi_up"),
              w.get("price_down_oi_up", 0.04)),
        check("second_breakdown", "再次跌破前低", f.get("second_breakdown"),
              w.get("second_breakdown", 0.04)),
    ]
    num, wsum = 0.0, 0.0
    for c in comps:
        if c["scale"] is None:
            continue
        num += c["weight"] * c["scale"]
        wsum += c["weight"]
    score = round(100 * num / wsum, 1) if wsum > 0 else None
    return {"score": score, "components": comps, "missing": []}


# ===========================================================================
# Blockers
# ===========================================================================
def compute_blockers(f: Dict[str, Any], cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    bc = cfg.get("blockers", {})
    out: List[Dict[str, Any]] = []

    def add(key, label, triggered, detail=""):
        out.append({"key": key, "label": label, "triggered": bool(triggered), "detail": detail})

    new_high = bool(f.get("price_new_high_recent") or f.get("rebound_new_high"))
    add("new_high", "价格重新创近期新高", new_high,
        f"近{bc.get('new_high_lookback_hours', 24)}h 内创出新高" if f.get("price_new_high_recent") else
        ("反抽已突破前高" if f.get("rebound_new_high") else ""))

    ratio = f.get("rebound_ratio")
    strong = float(bc.get("strong_rebound_ratio", cfg.get("rebound", {}).get("strong_rebound_ratio", 0.7)))
    add("strong_rebound", f"反抽恢复比例 > {int(strong*100)}%",
        bool(ratio is not None and ratio > strong),
        f"当前反抽恢复比例 {ratio*100:.0f}%" if ratio is not None else "无反抽数据")

    still_bull = bool(f.get("higher_high_confirmed") and f.get("higher_low_confirmed")
                      and not f.get("lower_high_confirmed"))
    add("bull_structure", "4H结构仍是 Higher High + Higher Low", still_bull, "")

    if bc.get("ema_reclaim_block", True):
        add("ema_reclaim", "价格重新站回 EMA20（EMA20 仍上行）", bool(f.get("ema20_reclaimed_recent")), "")

    if bc.get("require_swing_break", True):
        macd_neg = f.get("macd_hist") is not None and f.get("macd_hist") < 0
        add("no_swing_break", "MACD 转弱但未跌破关键 Swing Low",
            bool(macd_neg and not f.get("swing_low_break")), "")

    return out


def block_summary(blockers: List[Dict[str, Any]]) -> Tuple[bool, List[str]]:
    triggered = [b for b in blockers if b["triggered"]]
    return (len(triggered) > 0), [b["label"] for b in triggered]


# ===========================================================================
# State machine
# ===========================================================================
def derive_status(f: Dict[str, Any], oh: Dict[str, Any], rv: Dict[str, Any],
                  blockers: List[Dict[str, Any]], cfg: Dict[str, Any]) -> Dict[str, Any]:
    st = cfg.get("status", {})
    ohs = oh.get("score")
    rvs = rv.get("score")
    blocked, block_reasons = block_summary(blockers)
    short_ready_bar = float(cfg.get("reversal", {}).get("short_ready_score", 70))
    watch_bar = float(cfg.get("reversal", {}).get("watch_score", 40))
    was_ext = bool(f.get("was_extended"))
    weak_recent = bool(f.get("weakness_recent"))
    status = "NORMAL"
    reasons: List[str] = []

    def _age_hours(timestr: Optional[str]) -> Optional[float]:
        if not timestr:
            return None
        try:
            t = pd.Timestamp(timestr)
            ref = pd.Timestamp(f.get("k4h_last") or timestr)
            return float((ref - t).total_seconds() / 3600.0)
        except Exception:
            return None

    def _dd_ref() -> Optional[float]:
        H = f.get("pump_high")
        price = f.get("price")
        if H and price:
            return float(price / H - 1)
        for key in ("drawdown_from_30d_high", "drawdown_from_7d_high"):
            if f.get(key) is not None:
                return f.get(key)
        return None

    # ---- INVALIDATED（仅对有过暴涨背景的币有意义）----
    invalidated = bool(f.get("rebound_new_high")) or bool(
        f.get("price_new_high_recent") and f.get("macd_first_negative_time") and (f.get("macd_hist") or 0) > 0
    )
    if was_ext and invalidated:
        reasons.append("顶部信号失效：价格重新创出新高")
        if f.get("rebound_new_high"):
            reasons.append("反抽已突破前高（R > H）")
        return _result("INVALIDATED", reasons, blocked, block_reasons)

    # ---- 门控：非「近期暴涨」币不进入信号状态 ----
    if not was_ext:
        reasons.append("无近期暴涨背景（非扫描目标）")
        return _result("NORMAL", reasons, blocked, block_reasons)

    # ---- SHORT READY / SHORT TREND ----
    short_core = bool(f.get("lower_high_confirmed") and f.get("swing_low_break")
                      and rvs is not None and rvs >= short_ready_bar)
    if short_core and weak_recent and not blocked:
        dd = _dd_ref()
        age = _age_hours(f.get("second_breakdown_time") or f.get("swing_low_break_time"))
        trend = bool(
            (dd is not None and dd <= -float(st.get("short_trend_drawdown", 0.25)))
            or (age is not None and age >= 48 and dd is not None and dd <= -0.10)
        )
        reasons.append(f"Lower High + 跌破前低 + Reversal {rvs:.0f} ≥ {short_ready_bar:.0f}")
        if dd is not None:
            reasons.append(f"自高点回撤 {dd*100:.0f}%")
        return _result("SHORT_TREND" if trend else "SHORT_READY", reasons, blocked, block_reasons)

    # ---- REVERSAL CONFIRMING ----
    if rvs is not None and rvs >= watch_bar and (f.get("lower_high_confirmed") or f.get("swing_low_break")) \
            and weak_recent:
        reasons.append(f"Reversal {rvs:.0f} ≥ {watch_bar:.0f}，结构转弱迹象增多")
        if short_core and blocked:
            reasons.append("SHORT 条件达标，但被 Short Blockers 拦截")
        return _result("REVERSAL_CONFIRMING", reasons, blocked, block_reasons)

    # ---- FIRST WEAKNESS / TOP WATCH ----
    first_neg_recent = bool(
        f.get("macd_hist") is not None and f.get("macd_hist") < 0
        and 0 <= (f.get("macd_negative_bars") or 999) <= int(st.get("top_watch_bars", 12))
    )
    weakness_extra = bool(
        f.get("close_below_ema20")
        or (f.get("momentum_deceleration_score") or 0) >= float(st.get("first_weakness_decel", 40))
        or f.get("price_volume_divergence")
    )
    if first_neg_recent or f.get("close_below_ema20") or (f.get("momentum_deceleration_score") or 0) >= 50:
        if first_neg_recent and weakness_extra:
            reasons.append("MACD 转负 + 其他转弱迹象（减速/跌破 EMA20/量价背离）")
            return _result("FIRST_WEAKNESS", reasons, blocked, block_reasons)
        if first_neg_recent:
            reasons.append("4H MACD 第一次转弱（仅进入观察，不构成做空信号）")
            return _result("TOP_WATCH", reasons, blocked, block_reasons)
        if f.get("close_below_ema20"):
            reasons.append("暴涨后首次跌破 EMA20")
            return _result("FIRST_WEAKNESS", reasons, blocked, block_reasons)
        reasons.append("上涨速度明显衰减")
        return _result("TOP_WATCH", reasons, blocked, block_reasons)

    # ---- heat ladder ----
    if ohs is not None and ohs >= float(cfg.get("overheat", {}).get("score_extreme", 70)):
        reasons.append(f"Overheat {ohs:.0f} ≥ 70：异常暴涨中")
        return _result("EXTREME", reasons, blocked, block_reasons)
    if ohs is not None and ohs >= float(cfg.get("overheat", {}).get("score_watch", 50)):
        reasons.append(f"Overheat {ohs:.0f} ≥ 50：进入过热观察")
        return _result("HOT", reasons, blocked, block_reasons)
    reasons.append("近期暴涨后暂未出现转弱迹象")
    return _result("NORMAL", reasons, blocked, block_reasons)


def _result(status: str, reasons: List[str], blocked: bool, block_reasons: List[str]) -> Dict[str, Any]:
    return {
        "status": status,
        "status_reasons": reasons,
        "block_short": blocked,
        "block_reasons": block_reasons,
    }


# ===========================================================================
# Explainability helpers
# ===========================================================================
def build_explain(f: Dict[str, Any], oh: Dict[str, Any], rv: Dict[str, Any],
                  blockers: List[Dict[str, Any]]) -> Dict[str, Any]:
    """UI 展示用：为什么进入当前状态 / 为什么还没 SHORT READY。"""
    pos: List[Dict[str, Any]] = []
    neg: List[Dict[str, Any]] = []

    def add_pos(label, ok, detail=""):
        pos.append({"label": label, "ok": bool(ok), "detail": detail})

    def add_neg(label, ok, detail=""):
        neg.append({"label": label, "ok": bool(ok), "detail": detail})

    ratio = f.get("rebound_ratio")
    add_pos("24H上涨", f.get("return_24h") is not None and f["return_24h"] > 0,
            f"{(f.get('return_24h') or 0)*100:.1f}%")
    add_pos("成交量放大", f.get("volume_ratio_4h") is not None and f["volume_ratio_4h"] >= 3,
            f"{f.get('volume_ratio_4h') or 0:.1f}x")
    if f.get("rank_improvement_pct") is not None:
        add_pos("市值排名快速提升", f["rank_improvement_pct"] >= 0.5,
                f"{f.get('rank_improvement_pct')*100:.0f}%")
    if f.get("funding_percentile") is not None:
        add_pos("Funding 处于高位", f["funding_percentile"] >= 0.9,
                f"历史 {f['funding_percentile']*100:.0f}% 分位")
    add_pos("4H MACD 转负", f.get("macd_hist") is not None and f["macd_hist"] < 0, "")
    add_pos("已跌破 EMA20", bool(f.get("close_below_ema20")), "")

    # short-ready checklist (未达成的逐条列出)
    for key, label, cond, detail in [
        ("lower_high", "尚未形成 Lower High", f.get("lower_high_confirmed"), ""),
        ("swing_break", "尚未跌破关键 Swing Low", f.get("swing_low_break"), ""),
        ("second_break", "尚未二次跌破 Swing Low", f.get("second_breakdown"), ""),
    ]:
        add_neg(label, cond)
    if ratio is not None:
        add_neg("反抽恢复比例过强（>70%）", not (ratio > 0.7), f"当前 {ratio*100:.0f}%")
    for b in blockers:
        if b["triggered"]:
            add_neg(f"BLOCK: {b['label']}", False, b.get("detail", ""))

    return {"reached": pos, "missing": neg}
