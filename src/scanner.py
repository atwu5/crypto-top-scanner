"""Scan orchestrator — STEP 1..11 of the product spec.

python main.py scan
  1) 读取当前全部 USDT 永续
  2) 读取本地数据库
  3) 检查数据缺口
  4) 增量下载缺失行情
  5) 获取市场排名快照（失败容忍）
  6) 计算全部指标
  7) Overheat Score
  8) Reversal Score
  9) 状态机
 10) 输出候选列表（CLI 表格 + parquet）
 11) 保存本次扫描结果（DuckDB + parquet）
"""
from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import pandas as pd

from .binance_client import MarketDataSource
from .features import compute_features
from .marketcap_client import MarketCapClient
from .scoring import (STATUS_ORDER, build_explain, compute_blockers, derive_status,
                      overheat_score, reversal_score)
from .storage import Storage
from .updater import Updater
from .utils import get_logger, utcnow

LOG = get_logger("scanner")

JSON_FIELDS = ["structure", "pump", "pivots", "timeline", "explain",
               "overheat", "reversal", "blockers"]


def _jsonify(obj: Any) -> str:
    try:
        return json.dumps(obj, default=str, ensure_ascii=False)
    except Exception:
        return "{}"


class Scanner:
    def __init__(self, cfg: Dict[str, Any]):
        self.cfg = cfg
        self.storage = Storage(cfg)
        self.source = MarketDataSource(cfg)
        self.updater = Updater(cfg, self.storage, self.source)
        self.marketcap = MarketCapClient(cfg)
        self.log = get_logger("scanner")

    # ------------------------------------------------------------------
    def run(self, update: bool = True, symbols: Optional[List[str]] = None,
            top: int = 50, workers: int = 8) -> Dict[str, Any]:
        t0 = time.time()
        self.log.info("=" * 78)
        self.log.info("SCAN START (source mode will be auto-detected; update=%s)", update)

        # STEP 1/2/3/4 — universe + incremental update
        uni = self.storage.load_universe()
        if update or uni.empty:
            uni = self.updater.update_universe()
        all_syms = uni["symbol"].tolist() if not uni.empty else []
        if symbols:
            all_syms = [s for s in all_syms if s in set(symbols)] or list(symbols)
        update_res: Dict[str, Any] = {}
        if update and all_syms:
            update_res = self.updater.update_all(all_syms)
        self.storage.refresh_views()

        # STEP 5 — market cap snapshot（失败容忍，绝不伪造历史排名）
        snap = None
        try:
            snap = self.marketcap.fetch_snapshot()
        except Exception as e:
            self.log.warning("market cap fetch failed: %s", e)
        if snap is not None and not snap.empty:
            self.storage.save_marketcap_snapshot(snap)
        snaps_all = self.storage.load_marketcap()
        symmap, unresolved = self.marketcap.build_symbol_map(
            snap if snap is not None else snaps_all, all_syms or [])
        if not symmap.empty:
            self.storage.save_processed_csv("symbol_map.csv", symmap)
        if not unresolved.empty:
            self.storage.save_processed_csv("unresolved_symbols.csv", unresolved)
            self.log.info("unresolved marketcap mapping: %s symbols", len(unresolved))
        rank_index = self.marketcap.build_rank_index(snaps_all)

        # STEP 6..9 — compute features / scores / status for every symbol
        scan_time = pd.Timestamp(utcnow())
        rows: List[Dict[str, Any]] = []
        failed: List[Dict[str, str]] = []
        scan_syms = all_syms or self._symbols_with_local_data()

        def work(sym: str):
            return self._process_symbol(sym, rank_index)

        with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
            futs = {ex.submit(work, s): s for s in scan_syms}
            done = 0
            for fut in as_completed(futs):
                sym = futs[fut]
                done += 1
                try:
                    row = fut.result()
                    if row is not None:
                        rows.append(row)
                except Exception as e:
                    failed.append({"symbol": sym, "error": str(e)[:300]})
                    self.log.warning("symbol %s failed: %s", sym, e)
                if done % 100 == 0:
                    self.log.info("scan progress: %s/%s", done, len(scan_syms))

        if not rows:
            self.log.error("no symbols scanned — check data availability")
            return {"df": pd.DataFrame(), "scan_time": scan_time,
                    "failed": failed, "update": update_res}

        df = pd.DataFrame(rows)
        df = self._sort(df)
        df["scan_time"] = scan_time

        # STEP 10/11 — save + print
        self.storage.save_scan_results(df.drop(columns=["scan_time"]), scan_time)
        self._save_meta(scan_time, df, failed, update_res)
        self._print_summary(df, failed, update_res, scan_time, top)

        self.log.info("SCAN DONE in %.1fs — %s symbols, %s failed",
                      time.time() - t0, len(df), len(failed))
        return {"df": df, "scan_time": scan_time, "failed": failed, "update": update_res}

    # ------------------------------------------------------------------
    def _symbols_with_local_data(self) -> List[str]:
        base = self.storage.raw / "klines"
        if not base.exists():
            return []
        return sorted(p.name for p in base.iterdir() if p.is_dir())

    def _process_symbol(self, sym: str, rank_index) -> Optional[Dict[str, Any]]:
        st = self.storage
        k15 = st.load_klines(sym, "15m")
        k1h = st.load_klines(sym, "1h")
        k4h = st.load_klines(sym, "4h")
        if k4h.empty and k1h.empty:
            return None
        # 退市 / 陈旧合约过滤
        ucfg = self.cfg.get("universe", {})
        staleness_days = float(ucfg.get("max_staleness_days", 10))
        min_bars = int(ucfg.get("min_bars_4h", 50))
        ref = k4h if not k4h.empty else k1h
        last_t = pd.Timestamp(ref["open_time"].max())
        cutoff = pd.Timestamp(utcnow()) - pd.Timedelta(days=staleness_days)
        if last_t < cutoff:
            return None
        if not k4h.empty and len(k4h) < min_bars:
            return None
        data = {
            "15m": k15, "1h": k1h, "4h": k4h,
            "funding": st.load_funding(sym),
            "oi": st.load_oi(sym),
            "taker": st.load_taker(sym),
        }
        rank_ctx = MarketCapClient.get_rank_ctx(rank_index, sym, self.cfg, now=pd.Timestamp(utcnow()))
        f = compute_features(sym, data, rank_ctx, self.cfg)
        oh = overheat_score(f, self.cfg)
        rv = reversal_score(f, self.cfg)
        blockers = compute_blockers(f, self.cfg)
        status = derive_status(f, oh, rv, blockers, self.cfg)
        explain = build_explain(f, oh, rv, blockers)

        row: Dict[str, Any] = {}
        for key, val in f.items():
            if key in JSON_FIELDS or key in ("pivots",):
                row[key + "_json" if key != "pivots" else "pivots_json"] = _jsonify(val)
            elif isinstance(val, (dict, list)):
                row[key + "_json"] = _jsonify(val)
            else:
                row[key] = val
        row["overheat_score"] = oh["score"]
        row["reversal_score"] = rv["score"]
        row["overheat_components_json"] = _jsonify(oh)
        row["reversal_components_json"] = _jsonify(rv)
        row["blockers_json"] = _jsonify(blockers)
        row["block_short"] = status["block_short"]
        row["block_reasons_json"] = _jsonify(status["block_reasons"])
        row["status"] = status["status"]
        row["status_reasons_json"] = _jsonify(status["status_reasons"])
        row["explain_json"] = _jsonify(explain)
        row["updated_at"] = pd.Timestamp(utcnow())
        return row

    # ------------------------------------------------------------------
    @staticmethod
    def _sort(df: pd.DataFrame) -> pd.DataFrame:
        order = {s: i for i, s in enumerate(STATUS_ORDER)}
        df = df.copy()
        df["_so"] = df["status"].map(order).fillna(99)
        df = df.sort_values(["reversal_score", "overheat_score"],
                            ascending=[False, False], na_position="last")
        df = df.sort_values("_so", kind="stable").drop(columns=["_so"])
        return df.reset_index(drop=True)

    def _save_meta(self, scan_time, df: pd.DataFrame, failed, update_res) -> None:
        counts = df["status"].value_counts().to_dict()
        data_last = None
        if "data_last_time" in df.columns:
            series = pd.to_datetime(df["data_last_time"], errors="coerce", utc=True).dropna()
            if len(series):
                data_last = str(series.max())
        meta = {
            "scan_time": str(scan_time),
            "source_mode": self.source.mode,
            "symbols": int(len(df)),
            "status_counts": {k: int(v) for k, v in counts.items()},
            "data_through": data_last,
            "failed_symbols": len(failed),
            "update": {k: v for k, v in (update_res or {}).items() if k != "failures"},
        }
        (self.storage.processed / "scan_meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    # ------------------------------------------------------------------
    def _print_summary(self, df: pd.DataFrame, failed, update_res, scan_time, top: int = 60) -> None:
        from tabulate import tabulate

        counts = df["status"].value_counts()
        self.log.info("-" * 78)
        line = " | ".join(f"{s}: {int(counts.get(s, 0))}"
                          for s in ("SHORT_READY", "SHORT_TREND", "REVERSAL_CONFIRMING",
                                    "TOP_WATCH", "FIRST_WEAKNESS", "EXTREME"))
        self.log.info("STATUS COUNTS  %s", line)
        if not df.empty and "data_last_time" in df.columns:
            dl = pd.to_datetime(df["data_last_time"], errors="coerce", utc=True).max()
            self.log.info("DATA THROUGH    %s (source: %s)", dl, self.source.mode)

        show = df.copy()
        show["price"] = show["price"].map(lambda v: _fmt_price(v))
        for c in ("return_24h", "return_72h", "oi_change_24h", "funding_current",
                  "rebound_ratio", "rank_improvement_pct"):
            if c in show.columns:
                show[c] = show[c].map(lambda v: f"{v*100:.1f}%" if pd.notna(v) else "-")
        for c in ("volume_ratio_4h",):
            if c in show.columns:
                show[c] = show[c].map(lambda v: f"{v:.1f}x" if pd.notna(v) else "-")
        show["overheat_score"] = show["overheat_score"].map(lambda v: f"{v:.0f}" if pd.notna(v) else "-")
        show["reversal_score"] = show["reversal_score"].map(lambda v: f"{v:.0f}" if pd.notna(v) else "-")
        show["rank_change_abs"] = show["rank_change_abs"].map(
            lambda v: f"+{int(v)}" if pd.notna(v) and v > 0 else (f"{int(v)}" if pd.notna(v) else "-"))
        show["macd_disp"] = df.apply(lambda r: "NEG" if (r.get("macd_hist") or 0) < 0 else "POS", axis=1)
        show["structure_disp"] = df.apply(_structure_disp, axis=1)

        cols = ["symbol", "price", "return_24h", "return_72h", "volume_ratio_4h",
                "rank_now", "rank_change_abs", "oi_change_24h", "funding_current",
                "macd_disp", "structure_disp", "rebound_ratio", "overheat_score",
                "reversal_score", "status"]
        cols = [c for c in cols if c in show.columns]
        table = show[cols].head(max(10, int(top)))
        print()
        print(table.to_string(index=False, justify="left"))
        print()
        if failed or (update_res and update_res.get("failures")):
            fails = failed or [{"symbol": s, "error": e} for s, e in update_res.get("failures", [])]
            self.log.warning("FAILED SYMBOLS (%s): %s", len(fails),
                             ", ".join(f["symbol"] for f in fails[:12]))
        else:
            self.log.info("no failed symbols")


def _fmt_price(v) -> str:
    if pd.isna(v):
        return "-"
    v = float(v)
    if v >= 1000:
        return f"{v:,.0f}"
    if v >= 1:
        return f"{v:.3f}"
    if v >= 0.01:
        return f"{v:.5f}"
    return f"{v:.8f}"


def _structure_disp(row) -> str:
    lh = bool(row.get("lower_high_confirmed"))
    br = bool(row.get("swing_low_break"))
    hh = bool(row.get("higher_high_confirmed"))
    hl = bool(row.get("higher_low_confirmed"))
    if lh and br:
        return "LH+BREAK"
    if lh:
        return "LH"
    if br:
        return "BREAK"
    if hh and hl:
        return "HH+HL"
    return "-"
