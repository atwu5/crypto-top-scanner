"""Crypto Top Reversal Scanner — Streamlit 本地网页。

运行:  streamlit run app.py
- 首页: 各状态数量 + 全市场扫描主表格（搜索 / 状态 / 涨幅 / Overheat / Reversal / 市值排名筛选）
- 单币详情: 评分 + 状态 + 信号解释（为什么 TOP WATCH / 为什么不是 SHORT READY）
  + K线/EMA/Swing标记、Volume、MACD、OI、Funding、市值排名 六联图（共享时间轴）
"""
from __future__ import annotations

import json
from datetime import timedelta

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots

from src.indicators import add_indicators
from src.storage import Storage
from src.utils import load_config

st.set_page_config(page_title="Crypto Top Reversal Scanner", page_icon="🔭", layout="wide")

CFG = load_config()
STORAGE = Storage(CFG, read_only=True)

STATUS_COLORS = {
    "SHORT_READY": "#d62728",
    "SHORT_TREND": "#8c1d18",
    "REVERSAL_CONFIRMING": "#ff7f0e",
    "TOP_WATCH": "#e6b800",
    "FIRST_WEAKNESS": "#f0a030",
    "EXTREME": "#9467bd",
    "HOT": "#1f77b4",
    "INVALIDATED": "#7f7f7f",
    "NORMAL": "#bbbbbb",
}
STATUS_EMOJI = {
    "SHORT_READY": "🔴", "SHORT_TREND": "🟥", "REVERSAL_CONFIRMING": "🟠",
    "TOP_WATCH": "🟡", "FIRST_WEAKNESS": "🟡", "EXTREME": "🟣", "HOT": "🔵",
    "INVALIDATED": "⚪", "NORMAL": "⚫",
}


# --------------------------------------------------------------------------
# data access (cached)
# --------------------------------------------------------------------------
def _bump() -> int:
    return st.session_state.get("gen", 0)


@st.cache_data(ttl=90, show_spinner=False)
def load_scan(gen: int):
    df = STORAGE.load_latest_scan()
    return df


@st.cache_data(ttl=90, show_spinner=False)
def load_meta(gen: int):
    p = STORAGE.processed / "scan_meta.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


@st.cache_data(ttl=300, show_spinner=False)
def load_klines(symbol: str, interval: str, gen: int):
    return STORAGE.load_klines(symbol, interval)


@st.cache_data(ttl=300, show_spinner=False)
def load_oi(symbol: str, gen: int):
    return STORAGE.load_oi(symbol)


@st.cache_data(ttl=300, show_spinner=False)
def load_funding(symbol: str, gen: int):
    return STORAGE.load_funding(symbol)


@st.cache_data(ttl=300, show_spinner=False)
def load_rank_series(symbol: str, gen: int):
    smap_p = STORAGE.processed / "symbol_map.csv"
    if not smap_p.exists():
        return pd.DataFrame()
    smap = pd.read_csv(smap_p)
    hit = smap[smap["symbol"] == symbol]
    if hit.empty or pd.isna(hit.iloc[0].get("coin_id")):
        return pd.DataFrame()
    coin = hit.iloc[0]["coin_id"]
    snaps = STORAGE.load_marketcap()
    if snaps.empty or "coin_id" not in snaps.columns:
        return pd.DataFrame()
    sub = snaps[snaps["coin_id"] == coin][["timestamp", "market_cap_rank"]].dropna()
    return sub.sort_values("timestamp")


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _parse_json(s, default):
    if s is None or (isinstance(s, float) and pd.isna(s)):
        return default
    try:
        return json.loads(s)
    except Exception:
        return default


def _fmt_pct(v, digits=1):
    if v is None or pd.isna(v):
        return "-"
    return f"{v * 100:.{digits}f}%"


def _dash(v):
    return "-" if v is None or (isinstance(v, float) and pd.isna(v)) else v


SHORT_STATUS = {
    "SHORT_READY": "🔴 READY",
    "SHORT_TREND": "🟥 TREND",
    "REVERSAL_CONFIRMING": "🟠 CONFIRM",
    "TOP_WATCH": "🟡 WATCH",
    "FIRST_WEAKNESS": "🟡 WEAK",
    "EXTREME": "🟣 EXTREME",
    "HOT": "🔵 HOT",
    "INVALIDATED": "⚪ INVAL",
    "NORMAL": "⚫ NORM",
}


def _badge(status: str) -> str:
    c = STATUS_COLORS.get(status, "#888")
    return (f"<span style='background:{c};color:white;padding:4px 12px;border-radius:6px;"
            f"font-weight:700;font-size:18px'>{STATUS_EMOJI.get(status,'')} {status}</span>")


# --------------------------------------------------------------------------
# page: list
# --------------------------------------------------------------------------
def render_list():
    scan = load_scan(_bump())
    meta = load_meta(_bump())

    st.markdown("## 🔭 Crypto Top Reversal Scanner")
    st.caption(
        "暴涨币顶部反转扫描器 — 找出来、可解释、数据持续沉淀。"
        f"数据源: `{meta.get('source_mode', '?')}` ｜ 扫描时间: {meta.get('scan_time', '-')} ｜ "
        f"数据截至: {meta.get('data_through', '-')}（UTC）"
    )

    if scan.empty:
        st.warning("还没有扫描结果。请先运行：\n\n```bash\npython scripts/init_data.py\npython main.py scan\n```")
        return

    # ---- status counters ----
    counts = scan["status"].value_counts()
    cols = st.columns(6)
    for c, s in zip(cols, ["SHORT_READY", "REVERSAL_CONFIRMING", "TOP_WATCH", "FIRST_WEAKNESS", "EXTREME", "HOT"]):
        c.metric(s.replace("_", " "), int(counts.get(s, 0)))
    st.caption(f"共扫描 {len(scan)} 个合约 ｜ NORMAL {int(counts.get('NORMAL', 0))} ｜ "
               f"INVALIDATED {int(counts.get('INVALIDATED', 0))} ｜ SHORT_TREND {int(counts.get('SHORT_TREND', 0))}")

    # ---- filters ----
    with st.expander("筛选器", expanded=False):
        f1, f2, f3, f4 = st.columns([1.2, 1.6, 1, 1])
        q = f1.text_input("搜索币种", "").strip().upper()
        status_sel = f2.multiselect("状态筛选", sorted(scan["status"].unique()),
                                    default=[])
        min24 = f3.slider("24H 涨幅 ≥", -100, 500, -100, 5, format="%d%%")
        minoh = f4.slider("Overheat ≥", 0, 100, 0, 5)
        g1, g2, g3 = st.columns([1, 1, 1])
        minrv = g1.slider("Reversal ≥", 0, 100, 0, 5)
        rank_max = g2.number_input("当前市值排名 ≤（0=不限）", min_value=0, value=0, step=50)
        hide_normal = g3.checkbox("仅显示信号状态", value=True,
                                  help="隐藏 NORMAL / INVALIDATED / HOT 以下的币")

    view = scan.copy()
    if q:
        view = view[view["symbol"].str.contains(q, case=False, na=False)]
    if status_sel:
        view = view[view["status"].isin(status_sel)]
    elif hide_normal:
        view = view[~view["status"].isin(["NORMAL", "INVALIDATED"])]
    if min24 > -100:
        view = view[(view["return_24h"].fillna(-9) * 100 >= min24)]
    if minoh > 0:
        view = view[view["overheat_score"].fillna(-1) >= minoh]
    if minrv > 0:
        view = view[view["reversal_score"].fillna(-1) >= minrv]
    if rank_max > 0:
        view = view[view["rank_now"].fillna(10**9) <= rank_max]

    # ---- table ----
    disp = pd.DataFrame({
        "币种": view["symbol"],
        "价格": view["price"],
        "1H%": (view["return_1h"] * 100).round(2),
        "4H%": (view["return_4h"] * 100).round(2),
        "24H%": (view["return_24h"] * 100).round(2),
        "72H%": (view["return_72h"] * 100).round(2),
        "成交量倍数(4H)": view["volume_ratio_4h"].round(2),
        "市值排名": view["rank_now"].map(lambda v: "-" if pd.isna(v) else int(v)),
        "72H排名变化": view["rank_change_abs"].map(
            lambda v: "-" if pd.isna(v) else (f"+{int(v)}" if v > 0 else f"{int(v)}")),
        "OI 24H%": view["oi_change_24h"].map(lambda v: "-" if pd.isna(v) else round(v * 100, 1)),
        "Funding": view["funding_current"].map(lambda v: "-" if pd.isna(v) else round(v, 5)),
        "MACD": view["macd_hist"].map(lambda v: "负" if pd.notna(v) and v < 0 else ("正" if pd.notna(v) else "-")),
        "市场结构": view.apply(_structure_disp, axis=1),
        "反抽比例": view["rebound_ratio"].map(lambda v: "-" if pd.isna(v) else round(v * 100, 1)),
        "Overheat": view["overheat_score"],
        "Reversal": view["reversal_score"],
        "状态": view["status"].map(lambda s: SHORT_STATUS.get(s, s)),
        "更新时间": pd.to_datetime(view["data_last_time"], errors="coerce", utc=True).dt.strftime("%m-%d %H:%M"),
        "_symbol": view["symbol"],
    })
    st.caption(f"显示 {len(disp)} / {len(scan)} 个合约（点击行查看单币详情）")
    if disp["市值排名"].eq("-").all():
        st.info("市值排名暂无数据 —— 本环境无法访问 CoinGecko；快照会随持续运行自动积累（不会用当前值冒充历史）。")

    event = st.dataframe(
        disp.drop(columns=["_symbol"]),
        use_container_width=True,
        height=620,
        on_select="rerun",
        selection_mode="single-row",
        hide_index=True,
        column_config={
            "币种": st.column_config.TextColumn(width="small"),
            "状态": st.column_config.TextColumn(width="small"),
            "市场结构": st.column_config.TextColumn(width="small"),
            "MACD": st.column_config.TextColumn(width="small"),
            "价格": st.column_config.NumberColumn(width="small", format="%.6g"),
            "市值排名": st.column_config.TextColumn(width="small"),
            "72H排名变化": st.column_config.TextColumn(width="small"),
        },
    )
    rows = None
    if event is not None:
        sel = getattr(event, "selection", None)
        if sel is None and isinstance(event, dict):
            sel = event.get("selection")
        if sel is not None:
            rows = sel.get("rows") if isinstance(sel, dict) else getattr(sel, "rows", None)
    if rows:
        sym = disp.iloc[rows[0]]["_symbol"]
        st.session_state["sel_symbol"] = sym
        st.session_state["view"] = "detail"
        st.rerun()

    st.divider()
    st.caption("⚠️ 所有阈值均为 Hypothesis，需持续用案例验证；本工具不构成投资建议，且 **不做任何下单**。")


def _structure_disp(row) -> str:
    def b(v):
        return bool(v) if pd.notna(v) else False
    lh, br = b(row.get("lower_high_confirmed")), b(row.get("swing_low_break"))
    hh, hl = b(row.get("higher_high_confirmed")), b(row.get("higher_low_confirmed"))
    if lh and br:
        return "LH + 破位"
    if lh:
        return "LH"
    if br:
        return "破位"
    if hh and hl:
        return "HH + HL"
    return "-"


# --------------------------------------------------------------------------
# page: detail
# --------------------------------------------------------------------------
def render_detail(symbol: str):
    scan = load_scan(_bump())
    row = scan[scan["symbol"] == symbol]
    if row.empty:
        st.error(f"{symbol} 不在最近一次扫描结果中")
        return
    r = row.iloc[0]

    if st.button("← 返回列表"):
        st.session_state["view"] = "list"
        st.session_state["qp_applied"] = None
        try:
            st.query_params.clear()
        except Exception:
            pass
        st.rerun()

    status = r["status"]
    c1, c2, c3 = st.columns([2, 1, 1])
    c1.markdown(f"## {symbol}  " + _badge(status), unsafe_allow_html=True)
    c2.metric("Overheat", f"{r['overheat_score']:.0f}" if pd.notna(r["overheat_score"]) else "-")
    c3.metric("Reversal", f"{r['reversal_score']:.0f}" if pd.notna(r["reversal_score"]) else "-")

    m = st.columns(7)
    m[0].metric("价格", f"{r['price']:,.6g}" if pd.notna(r["price"]) else "-")
    m[1].metric("24H", _fmt_pct(r["return_24h"]))
    m[2].metric("72H", _fmt_pct(r["return_72h"]))
    m[3].metric("成交量×", f"{r['volume_ratio_4h']:.1f}x" if pd.notna(r["volume_ratio_4h"]) else "-")
    m[4].metric("OI 24H", _fmt_pct(r["oi_change_24h"]))
    m[5].metric("Funding", _fmt_pct(r["funding_current"], 3))
    rank_now = r["rank_now"]
    if pd.notna(rank_now):
        m[6].metric("市值排名", f"{int(rank_now)}", delta=(
            f"{int(r['rank_change_abs'])} (72h)" if pd.notna(r["rank_change_abs"]) else None))
    else:
        m[6].metric("市值排名", "N/A")

    if pd.isna(rank_now):
        st.info("历史市值排名暂无数据 —— 本地快照会随持续运行自动积累；缺失时所有排名相关指标记为 NULL，不会用当前值冒充。")

    # ---- blockers / explain ----
    block_short = bool(r.get("block_short")) if pd.notna(r.get("block_short")) else False
    block_reasons = _parse_json(r.get("block_reasons_json"), [])
    if block_short:
        st.error("🚫 BLOCK SHORT\n\n" + "\n".join(f"- {b}" for b in block_reasons))

    explain = _parse_json(r.get("explain_json"), {"reached": [], "missing": []})
    status_reasons = _parse_json(r.get("status_reasons_json"), [])
    if status_reasons:
        st.markdown("**当前状态原因：**")
        for s in status_reasons:
            st.markdown(f"- {s}")

    e1, e2 = st.columns(2)
    with e1:
        st.markdown("**已满足信号 ✓**")
        reached = [x for x in explain.get("reached", [])]
        shown = 0
        for x in reached:
            if x.get("ok"):
                st.markdown(f"- ✓ {x['label']}" + (f"：{x['detail']}" if x.get("detail") else ""))
                shown += 1
        if not shown:
            st.markdown("- （暂无）")
    with e2:
        st.markdown("**尚未满足 / 阻止项 ✗**")
        miss = [x for x in explain.get("missing", []) if not x.get("ok")]
        if miss:
            for x in miss:
                st.markdown(f"- ✗ {x['label']}" + (f"：{x['detail']}" if x.get("detail") else ""))
        else:
            st.markdown("- （无）")

    if explain.get("missing") and not miss:
        st.success("所有 SHORT READY 关键条件均已满足，且无拦截项。")

    # ---- charts ----
    st.markdown("### 图表（共享时间轴，UTC）")
    range_opt = st.radio("显示范围", ["近15天", "近30天", "近60天"], index=1, horizontal=True)
    bars = {"近15天": 90, "近30天": 180, "近60天": 360}[range_opt]
    fig = build_charts(symbol, r, bars)
    if fig is not None:
        st.plotly_chart(fig, use_container_width=True)

    # ---- raw features ----
    with st.expander("全部特征值（原始）"):
        keys = ["price", "return_1h", "return_4h", "return_12h", "return_24h", "return_72h", "return_7d",
                "momentum_deceleration_score", "drawdown_from_72h_high", "drawdown_from_7d_high",
                "volume_1h", "volume_4h", "volume_24h", "volume_ratio_4h", "volume_ratio_4h_median",
                "volume_ratio_24h", "volume_ratio_24h_median", "volume_since_peak_change",
                "price_volume_divergence", "oi_change_1h", "oi_change_4h", "oi_change_24h",
                "funding_current", "funding_percentile", "funding_zscore",
                "rank_now", "rank_24h", "rank_72h", "rank_7d", "rank_change_abs", "rank_improvement_pct",
                "ema20", "ema60", "ema20_slope", "ema60_slope", "close_below_ema20",
                "macd", "macd_signal", "macd_hist", "macd_negative_bars", "rsi14",
                "lower_high_confirmed", "lower_low_confirmed", "swing_low_break", "second_breakdown",
                "market_structure_broken", "rebound_ratio", "rebound_failed", "rebound_new_high",
                "pump_high", "data_last_time"]
        vals = [(k, r.get(k)) for k in keys if k in r.index]
        st.dataframe(pd.DataFrame(vals, columns=["feature", "value"]), hide_index=True, height=380)


def build_charts(symbol: str, row, bars: int):
    k4 = load_klines(symbol, "4h", _bump())
    if k4.empty:
        st.info("本地没有该币的 4H 数据")
        return None
    k4i = add_indicators(k4).tail(bars).reset_index(drop=True)
    oi = load_oi(symbol, _bump())
    funding = load_funding(symbol, _bump())
    ranks = load_rank_series(symbol, _bump())

    fig = make_subplots(
        rows=6, cols=1, shared_xaxes=True, vertical_spacing=0.035,
        row_heights=[0.36, 0.10, 0.15, 0.15, 0.12, 0.12],
        subplot_titles=("K线 + EMA20/60 + 结构标记", "Volume", "MACD (4H)", "Open Interest", "Funding", "Market Cap Rank"),
    )
    x = k4i["open_time"]

    # 1) candles + EMA
    fig.add_trace(go.Candlestick(x=x, open=k4i["open"], high=k4i["high"], low=k4i["low"], close=k4i["close"],
                                 name="K", increasing_line_color="#26a69a", decreasing_line_color="#ef5350",
                                 showlegend=False), row=1, col=1)
    fig.add_trace(go.Scatter(x=x, y=k4i["ema20"], name="EMA20", line=dict(color="#ff9800", width=1.2)), row=1, col=1)
    fig.add_trace(go.Scatter(x=x, y=k4i["ema60"], name="EMA60", line=dict(color="#3f51b5", width=1.2)), row=1, col=1)

    # swing markers
    piv = _parse_json(row.get("pivots_json"), [])
    if piv:
        t_lo, t_hi = x.min(), x.max()
        ph = [(pd.Timestamp(p["time"]), p["price"]) for p in piv if p["kind"] == "H" and t_lo <= pd.Timestamp(p["time"]) <= t_hi]
        pl = [(pd.Timestamp(p["time"]), p["price"]) for p in piv if p["kind"] == "L" and t_lo <= pd.Timestamp(p["time"]) <= t_hi]
        if ph:
            fig.add_trace(go.Scatter(x=[p[0] for p in ph], y=[p[1] for p in ph], mode="markers",
                                     marker=dict(symbol="triangle-down", color="#d62728", size=9),
                                     name="Swing High"), row=1, col=1)
        if pl:
            fig.add_trace(go.Scatter(x=[p[0] for p in pl], y=[p[1] for p in pl], mode="markers",
                                     marker=dict(symbol="triangle-up", color="#2e7d32", size=9),
                                     name="Swing Low"), row=1, col=1)

    # timeline events
    tl = _parse_json(row.get("timeline_json"), [])
    for ev in tl:
        try:
            t = pd.Timestamp(ev["time"])
            label = ev["label"]
            if t < x.min() or t > x.max():
                continue
            nearest = k4i.iloc[(k4i["open_time"] - t).abs().argsort()[:1]]
            yv = float(nearest["high"].iloc[0])
            color = {"FIRST WEAKNESS": "#e6b800", "LOWER HIGH": "#ff7f0e", "BREAKDOWN": "#d62728",
                     "SHORT READY": "#8c1d18", "REBOUND HIGH": "#9c27b0", "PUMP HIGH": "#2e7d32"}.get(label, "#666")
            fig.add_trace(go.Scatter(x=[t], y=[yv * 1.01], mode="markers+text", text=[label],
                                     textposition="top center", textfont=dict(size=10, color=color),
                                     marker=dict(symbol="x", color=color, size=10),
                                     showlegend=False), row=1, col=1)
        except Exception:
            continue

    # 2) volume
    colors = ["#26a69a" if c >= o else "#ef5350" for o, c in zip(k4i["open"], k4i["close"])]
    fig.add_trace(go.Bar(x=x, y=k4i["quote_volume"], marker_color=colors, name="QuoteVolume"), row=2, col=1)

    # 3) MACD
    hcolors = ["#26a69a" if v >= 0 else "#ef5350" for v in k4i["macd_hist"].fillna(0)]
    fig.add_trace(go.Bar(x=x, y=k4i["macd_hist"], marker_color=hcolors, name="Hist"), row=3, col=1)
    fig.add_trace(go.Scatter(x=x, y=k4i["macd"], name="MACD", line=dict(color="#2962ff", width=1.1)), row=3, col=1)
    fig.add_trace(go.Scatter(x=x, y=k4i["macd_signal"], name="Signal", line=dict(color="#ff6d00", width=1.1)), row=3, col=1)

    # 4) OI
    if oi is not None and not oi.empty:
        oi2 = oi.set_index("time").resample("1h").last().reset_index()
        oi2 = oi2[(oi2["time"] >= x.min() - timedelta(hours=4)) & (oi2["time"] <= x.max() + timedelta(hours=4))]
        fig.add_trace(go.Scatter(x=oi2["time"], y=oi2["open_interest_value"], name="OI(USD)",
                                 line=dict(color="#7b1fa2", width=1.3)), row=4, col=1)
    else:
        fig.add_annotation(x=x.iloc[len(x) // 2], y=0.5, yref="y4", text="OI 数据不可用", showarrow=False, row=4, col=1)

    # 5) Funding
    if funding is not None and not funding.empty:
        f2 = funding[(funding["funding_time"] >= x.min() - timedelta(hours=8)) & (funding["funding_time"] <= x.max() + timedelta(hours=8))]
        fig.add_trace(go.Scatter(x=f2["funding_time"], y=f2["funding_rate"] * 100, name="Funding %",
                                 line=dict(color="#00838f", width=1.2, shape="hv")), row=5, col=1)
    else:
        fig.add_annotation(x=x.iloc[len(x) // 2], y=0.5, yref="y5", text="Funding 数据不可用", showarrow=False, row=5, col=1)

    # 6) Rank
    if ranks is not None and len(ranks) >= 1:
        fig.add_trace(go.Scatter(x=ranks["timestamp"], y=ranks["market_cap_rank"], mode="lines+markers",
                                 name="Rank", line=dict(color="#455a64", width=1.2)), row=6, col=1)
        if len(ranks) < 3:
            fig.add_annotation(x=ranks["timestamp"].iloc[-1], y=ranks["market_cap_rank"].iloc[-1],
                               text="历史市值排名暂无数据（持续运行后积累）", showarrow=False, row=6, col=1)
    else:
        fig.add_annotation(x=x.iloc[len(x) // 2], y=0.5, yref="y6",
                           text="历史市值排名暂无数据", showarrow=False, row=6, col=1)

    fig.update_layout(height=1180, hovermode="x unified", showlegend=True,
                      legend=dict(orientation="h", y=1.04, x=0, font=dict(size=11)),
                      margin=dict(l=20, r=20, t=95, b=30))
    fig.update_xaxes(rangeslider_visible=False)
    fig.update_yaxes(title_text="Rank", autorange="reversed", row=6, col=1)
    return fig


# --------------------------------------------------------------------------
def main():
    if "view" not in st.session_state:
        st.session_state["view"] = "list"
    if "gen" not in st.session_state:
        st.session_state["gen"] = 0
    # 深链: ?symbol=XXX 直接打开单币详情
    try:
        qsym = st.query_params.get("symbol")
    except Exception:
        qsym = None
    if qsym and st.session_state.get("qp_applied") != qsym:
        st.session_state["qp_applied"] = qsym
        st.session_state["sel_symbol"] = str(qsym).upper()
        st.session_state["view"] = "detail"

    with st.sidebar:
        st.markdown("### 🔭 Top Reversal Scanner")
        symbols = []
        scan = load_scan(_bump())
        if not scan.empty:
            symbols = scan["symbol"].tolist()
        sel = st.selectbox("单币详情", ["（选择币种）"] + symbols, index=0)
        if sel != "（选择币种）":
            st.session_state["sel_symbol"] = sel
            st.session_state["view"] = "detail"
        if st.button("🔄 刷新数据"):
            st.session_state["gen"] += 1
            st.cache_data.clear()
        st.caption("数据为 UTC 时间；信号解释见详情页。")

    if st.session_state["view"] == "detail" and st.session_state.get("sel_symbol"):
        render_detail(st.session_state["sel_symbol"])
    else:
        render_list()


main()
