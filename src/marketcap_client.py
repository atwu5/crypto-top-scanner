"""Market cap / rank data via CoinGecko (优先) — 历史排名只依赖本地累计快照。

重要原则（需求明确要求）：
- 历史日期的市值排名如果无法可靠获取 → 记录 NULL，绝不用当前排名冒充；
- 无法自动映射的 symbol 记入 data/processed/unresolved_symbols.csv，可人工在
  config/symbol_overrides.csv 里补充映射。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from .utils import HttpClient, get_logger, utcnow

LOG = get_logger("marketcap")


class MarketCapClient:
    def __init__(self, cfg: Dict[str, Any]):
        self.cfg = cfg
        api = cfg.get("api", {})
        cg = cfg.get("coingecko", {})
        self.base = api.get("coingecko_base", "https://api.coingecko.com/api/v3").rstrip("/")
        self.vs = cg.get("vs_currency", "usd")
        self.per_page = int(cg.get("per_page", 250))
        self.max_pages = int(cg.get("max_pages", 4))
        self.overrides_file = cfg.get("_project_root", ".") + "/" + cg.get("overrides_file", "config/symbol_overrides.csv")
        self.http = HttpClient(timeout=20, max_retries=3, backoff_base=1.0, backoff_max=15,
                               rate_per_minute=20, user_agent=api.get("user_agent", "crypto-top-scanner"),
                               logger=get_logger("coingecko"))

    # ------------------------------------------------------------------
    def fetch_snapshot(self) -> Optional[pd.DataFrame]:
        """Fetch top-N coins snapshot from CoinGecko. Returns None on failure."""
        rows: List[dict] = []
        for page in range(1, self.max_pages + 1):
            data = self.http.get(
                f"{self.base}/coins/markets",
                params={
                    "vs_currency": self.vs,
                    "order": "market_cap_desc",
                    "per_page": self.per_page,
                    "page": page,
                    "price_change_percentage": "24h",
                },
                as_json=True,
            )
            if not data:
                if page == 1:
                    LOG.warning("CoinGecko unavailable — 本轮不保存市值快照（不伪造历史排名）")
                    return None
                break
            rows.extend(data)
            if len(data) < self.per_page:
                break
        if not rows:
            return None
        ts = pd.Timestamp(utcnow())
        df = pd.DataFrame(rows)
        out = pd.DataFrame({
            "timestamp": ts,
            "coin_id": df.get("id"),
            "symbol": df.get("symbol", pd.Series(dtype=str)).astype(str).str.upper(),
            "name": df.get("name"),
            "market_cap": pd.to_numeric(df.get("market_cap"), errors="coerce"),
            "market_cap_rank": pd.to_numeric(df.get("market_cap_rank"), errors="coerce"),
            "current_price": pd.to_numeric(df.get("current_price"), errors="coerce"),
            "change_24h": pd.to_numeric(df.get("price_change_percentage_24h"), errors="coerce") / 100.0,
        })
        LOG.info("market cap snapshot fetched: %s coins", len(out))
        return out

    # ------------------------------------------------------------------
    def load_overrides(self) -> Dict[str, str]:
        try:
            df = pd.read_csv(self.overrides_file)
            df = df.dropna(subset=["symbol", "coingecko_id"])
            return {str(r.symbol).strip().upper(): str(r.coingecko_id).strip() for r in df.itertuples()}
        except Exception:
            return {}

    @staticmethod
    def _candidates(base: str) -> List[str]:
        base = base.upper()
        cands = [base]
        for pref in ("1000000", "1000", "1M"):
            if base.startswith(pref) and len(base) > len(pref):
                cands.append(base[len(pref):])
        return cands

    def build_symbol_map(self, markets: pd.DataFrame, binance_symbols: List[str]) -> Tuple[pd.DataFrame, pd.DataFrame]:
        """Map binance symbols -> coingecko coin_id (auto + overrides)."""
        overrides = self.load_overrides()
        if markets is None or markets.empty:
            markets = pd.DataFrame(columns=["symbol", "coin_id", "market_cap_rank"])
        by_symbol = {}
        for r in markets.dropna(subset=["symbol"]).itertuples():
            s = str(r.symbol).upper()
            cur = by_symbol.get(s)
            if cur is None or (pd.notna(r.market_cap_rank) and (cur["rank"] is None or r.market_cap_rank < cur["rank"])):
                by_symbol[s] = {"coin_id": r.coin_id, "rank": r.market_cap_rank}

        resolved, unresolved = [], []
        for sym in binance_symbols:
            if sym in overrides:
                cid = overrides[sym]
                rank = by_symbol.get(cid.upper(), {}).get("rank")
                resolved.append({"symbol": sym, "coin_id": cid, "market_cap_rank": rank, "match": "manual"})
                continue
            base = sym[:-4] if sym.endswith("USDT") else sym
            hit = None
            for cand in self._candidates(base):
                if cand in by_symbol:
                    hit = by_symbol[cand]
                    break
            if hit:
                resolved.append({"symbol": sym, "coin_id": hit["coin_id"],
                                 "market_cap_rank": hit["rank"], "match": "auto"})
            else:
                unresolved.append({"symbol": sym, "reason": "no coingecko match"})
        return pd.DataFrame(resolved), pd.DataFrame(unresolved)

    # ------------------------------------------------------------------
    @staticmethod
    def build_rank_index(snapshots: pd.DataFrame) -> Dict[str, List[Tuple[pd.Timestamp, int]]]:
        """symbol -> sorted [(timestamp, rank)] from accumulated local snapshots."""
        idx: Dict[str, List[Tuple[pd.Timestamp, int]]] = {}
        if snapshots is None or snapshots.empty:
            return idx
        df = snapshots.dropna(subset=["market_cap_rank"])
        for r in df.itertuples():
            key = str(r.symbol).upper()
            idx.setdefault(key, []).append((pd.Timestamp(r.timestamp), int(r.market_cap_rank)))
        for k in idx:
            idx[k].sort(key=lambda x: x[0])
        return idx

    @staticmethod
    def get_rank_ctx(rank_index: Dict[str, List[Tuple[pd.Timestamp, int]]],
                     symbol: str, cfg: Dict[str, Any],
                     now: Optional[pd.Timestamp] = None) -> Dict[str, Any]:
        """Rank features for a symbol. Missing history => None (绝不伪造)."""
        ctx = {"rank_now": None, "rank_24h": None, "rank_72h": None, "rank_7d": None,
               "rank_change_abs": None, "rank_improvement_pct": None,
               "rank_breakout": None, "rank_near_target": None, "rank_as_of": None}
        series = rank_index.get(symbol.upper())
        if not series:
            return ctx
        now = now or pd.Timestamp(utcnow())
        ranks = pd.DataFrame(series, columns=["ts", "rank"]).sort_values("ts")
        now_rank = int(ranks.iloc[-1]["rank"])
        ctx["rank_now"] = now_rank
        ctx["rank_as_of"] = str(ranks.iloc[-1]["ts"])

        def rank_at(hours: float):
            target = now - pd.Timedelta(hours=hours)
            sub = ranks[ranks["ts"] <= target]
            if sub.empty:
                return None
            # 要求快照确实早于目标时间（避免拿当前值冒充）
            return int(sub.iloc[-1]["rank"])

        r24 = rank_at(24)
        r72 = rank_at(72)
        r7d = rank_at(168)
        ctx["rank_24h"], ctx["rank_72h"], ctx["rank_7d"] = r24, r72, r7d
        if r72 is not None:
            ctx["rank_change_abs"] = r72 - now_rank  # 正数 = 排名提升
            if r72 > 0:
                ctx["rank_improvement_pct"] = float((r72 - now_rank) / r72)
                rk = cfg.get("rank", {})
                ctx["rank_breakout"] = bool(
                    ctx["rank_improvement_pct"] >= rk.get("improvement_pct", 0.5)
                    and now_rank <= rk.get("target_rank", 100)
                )
                ctx["rank_near_target"] = bool(now_rank <= rk.get("near_target_rank", 150))
        return ctx
