"""Local storage layer: Parquet partitions + DuckDB query layer.

- K 线按  data/raw/klines/{symbol}/{interval}/YYYY-MM.parquet  月分区存储
- Funding / OI / Taker 按 symbol 单文件存储
- 市值快照按  data/raw/market_cap/snapshots/YYYY-MM.parquet
- 写入前按 (symbol, interval, timestamp) 去重，重复执行不产生重复数据
- DuckDB 建立统一查询视图（klines_15m/1h/4h, funding, open_interest, taker, market_cap）
"""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import duckdb
import pandas as pd

from .utils import ensure_dir, get_logger, utcnow

LOG = get_logger("storage")


class Storage:
    def __init__(self, cfg: Dict[str, Any], read_only: bool = False):
        root = Path(cfg.get("_project_root", "."))
        st = cfg.get("storage", {})
        self.root = root
        self.read_only = read_only
        self.raw = ensure_dir(root / st.get("raw_dir", "data/raw"))
        self.processed = ensure_dir(root / st.get("processed_dir", "data/processed"))
        self.duckdb_path = root / st.get("duckdb_path", "data/scanner.duckdb")
        ensure_dir(self.duckdb_path.parent)
        ensure_dir(self.processed / "scans")
        self.compression = st.get("parquet_compression", "zstd")
        self._lock = threading.Lock()
        self._last_time_cache: Dict[str, Optional[pd.Timestamp]] = {}
        self._con: Optional[duckdb.DuckDBPyConnection] = None

    # ==================================================================
    # paths
    # ==================================================================
    def kline_dir(self, symbol: str, interval: str) -> Path:
        return ensure_dir(self.raw / "klines" / symbol / interval)

    def kline_file(self, symbol: str, interval: str, ym: str) -> Path:
        return self.kline_dir(symbol, interval) / f"{ym}.parquet"

    def funding_file(self, symbol: str) -> Path:
        return ensure_dir(self.raw / "funding" / symbol)

    def oi_file(self, symbol: str) -> Path:
        return ensure_dir(self.raw / "open_interest" / symbol)

    def taker_file(self, symbol: str) -> Path:
        return ensure_dir(self.raw / "taker" / symbol)

    def marketcap_file(self, ym: str) -> Path:
        return ensure_dir(self.raw / "market_cap" / "snapshots") / f"{ym}.parquet"

    # ==================================================================
    # write helpers (dedup + atomic)
    # ==================================================================
    def _write_parquet(self, path: Path, df: pd.DataFrame) -> None:
        tmp = path.with_suffix(path.suffix + ".tmp")
        df.to_parquet(tmp, engine="pyarrow", compression=self.compression, index=False)
        os.replace(tmp, path)

    def _merge_write(self, path: Path, new_df: pd.DataFrame, time_col: str) -> int:
        """Read existing + concat + dedup(time_col) + atomic write. Returns rows added."""
        if new_df is None or new_df.empty:
            return 0
        new_df = new_df.dropna(subset=[time_col]).sort_values(time_col)
        before = 0
        if path.exists():
            old = pd.read_parquet(path)
            before = len(old)
            merged = pd.concat([old, new_df], ignore_index=True)
        else:
            merged = new_df
        merged = merged.drop_duplicates(subset=[time_col], keep="last").sort_values(time_col)
        merged = merged.reset_index(drop=True)
        self._write_parquet(path, merged)
        return max(0, len(merged) - before)

    def save_klines(self, symbol: str, interval: str, df: pd.DataFrame, source_key: str = "") -> int:
        if df is None or df.empty:
            return 0
        added = 0
        df = df.copy()
        df["symbol"] = symbol
        months = df["open_time"].dt.strftime("%Y-%m")
        for ym, part in df.groupby(months):
            added += self._merge_write(self.kline_file(symbol, interval, str(ym)), part, "open_time")
        with self._lock:
            self._last_time_cache.pop(f"{source_key}|{symbol}|{interval}", None)
        return added

    def save_funding(self, symbol: str, df: pd.DataFrame) -> int:
        if df is None or df.empty:
            return 0
        df = df.copy()
        df["symbol"] = symbol
        return self._merge_write(self.funding_file(symbol) / f"{symbol}.parquet", df, "funding_time")

    def save_oi(self, symbol: str, df: pd.DataFrame) -> int:
        if df is None or df.empty:
            return 0
        df = df.copy()
        df["symbol"] = symbol
        return self._merge_write(self.oi_file(symbol) / f"{symbol}.parquet", df, "time")

    def save_taker(self, symbol: str, df: pd.DataFrame) -> int:
        if df is None or df.empty:
            return 0
        cols = [c for c in ["time", "taker_ratio"] if c in df.columns]
        if len(cols) < 2:
            return 0
        out = df[cols].dropna(subset=["time"]).copy()
        out["symbol"] = symbol
        return self._merge_write(self.taker_file(symbol) / f"{symbol}.parquet", out, "time")

    def save_marketcap_snapshot(self, df: pd.DataFrame) -> Optional[Path]:
        if df is None or df.empty:
            return None
        df = df.copy()
        ym = pd.to_datetime(df["timestamp"]).dt.strftime("%Y-%m").max()
        path = self.marketcap_file(str(ym))
        self._merge_write(path, df, "timestamp")
        return path

    # ==================================================================
    # read helpers
    # ==================================================================
    def _files_in_range(self, symbol: str, interval: str, start: Optional[datetime],
                        end: Optional[datetime]) -> List[Path]:
        d = self.raw / "klines" / symbol / interval
        if not d.exists():
            return []
        files = sorted(d.glob("*.parquet"))
        if start is not None:
            m0 = start.strftime("%Y-%m")
            files = [f for f in files if f.stem >= m0]
        if end is not None:
            m1 = end.strftime("%Y-%m")
            files = [f for f in files if f.stem <= m1]
        return files

    def load_klines(self, symbol: str, interval: str,
                    start: Optional[datetime] = None, end: Optional[datetime] = None) -> pd.DataFrame:
        files = self._files_in_range(symbol, interval, start, end)
        if not files:
            return pd.DataFrame()
        df = pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)
        df = df.drop_duplicates("open_time").sort_values("open_time")
        if start is not None:
            df = df[df["open_time"] >= pd.Timestamp(start)]
        if end is not None:
            df = df[df["open_time"] <= pd.Timestamp(end)]
        return df.reset_index(drop=True)

    def last_kline_time(self, symbol: str, interval: str, source_key: str = "") -> Optional[pd.Timestamp]:
        key = f"{source_key}|{symbol}|{interval}"
        with self._lock:
            if key in self._last_time_cache:
                return self._last_time_cache[key]
        files = sorted((self.raw / "klines" / symbol / interval).glob("*.parquet"))
        res: Optional[pd.Timestamp] = None
        if files:
            try:
                last = pd.read_parquet(files[-1], columns=["open_time"])["open_time"].max()
                res = pd.Timestamp(last)
            except Exception as e:
                LOG.warning("last_kline_time read failed %s %s: %s", symbol, interval, e)
        with self._lock:
            self._last_time_cache[key] = res
        return res

    def last_time_generic(self, path: Path, col: str) -> Optional[pd.Timestamp]:
        if not path.exists():
            return None
        try:
            return pd.Timestamp(pd.read_parquet(path, columns=[col])[col].max())
        except Exception:
            return None

    def load_funding(self, symbol: str) -> pd.DataFrame:
        p = self.funding_file(symbol) / f"{symbol}.parquet"
        return pd.read_parquet(p) if p.exists() else pd.DataFrame()

    def load_oi(self, symbol: str) -> pd.DataFrame:
        p = self.oi_file(symbol) / f"{symbol}.parquet"
        return pd.read_parquet(p) if p.exists() else pd.DataFrame()

    def load_taker(self, symbol: str) -> pd.DataFrame:
        p = self.taker_file(symbol) / f"{symbol}.parquet"
        return pd.read_parquet(p) if p.exists() else pd.DataFrame()

    def load_marketcap(self, ym_from: Optional[str] = None) -> pd.DataFrame:
        d = self.raw / "market_cap" / "snapshots"
        if not d.exists():
            return pd.DataFrame()
        files = sorted(d.glob("*.parquet"))
        if ym_from:
            files = [f for f in files if f.stem >= ym_from]
        if not files:
            return pd.DataFrame()
        return pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)

    # ==================================================================
    # universe / processed files
    # ==================================================================
    def save_universe(self, df: pd.DataFrame) -> Path:
        p = self.processed / "universe.parquet"
        df = df.copy()
        df["fetched_at"] = pd.Timestamp(utcnow())
        df.to_parquet(p, index=False)
        return p

    def load_universe(self) -> pd.DataFrame:
        p = self.processed / "universe.parquet"
        return pd.read_parquet(p) if p.exists() else pd.DataFrame()

    def save_processed_csv(self, name: str, df: pd.DataFrame) -> Path:
        p = self.processed / name
        df.to_csv(p, index=False)
        return p

    # ==================================================================
    # scan results
    # ==================================================================
    def duck(self) -> duckdb.DuckDBPyConnection:
        if self._con is None:
            if self.read_only and not self.duckdb_path.exists():
                raise RuntimeError(f"duckdb not found: {self.duckdb_path}")
            if self.read_only:
                self._con = duckdb.connect(str(self.duckdb_path), read_only=True)
            else:
                self._con = self._connect_writer()
        return self._con

    def _connect_writer(self) -> duckdb.DuckDBPyConnection:
        """Read-write connect with retry (另一个进程可能短暂持锁)."""
        last_err = None
        for attempt in range(8):
            try:
                return duckdb.connect(str(self.duckdb_path))
            except Exception as e:  # lock conflict
                last_err = e
                time.sleep(1.5 * (attempt + 1))
        raise RuntimeError(f"cannot open duckdb for write: {last_err}")

    def _with_write_retry(self, fn, what: str):
        last_err = None
        for attempt in range(5):
            try:
                return fn()
            except Exception as e:
                msg = str(e)
                if "lock" in msg.lower() or "Conflicting" in msg:
                    last_err = e
                    try:
                        if self._con is not None:
                            self._con.close()
                    except Exception:
                        pass
                    self._con = None
                    time.sleep(2.0 * (attempt + 1))
                    continue
                raise
        LOG.warning("%s failed after retries (duckdb locked): %s", what, last_err)
        return None

    def save_scan_results(self, df: pd.DataFrame, scan_time: pd.Timestamp) -> None:
        df = df.copy()
        df.insert(0, "scan_time", scan_time)
        # parquet snapshot (历史快照始终保留)
        snap = self.processed / "scans" / f"scan_{scan_time.strftime('%Y%m%d_%H%M%S')}.parquet"
        df.to_parquet(snap, index=False, compression=self.compression)
        # duckdb table (append; schema evolution -> recreate)
        def _write():
            con = self.duck()
            try:
                con.register("new_rows", df)
                for attempt in (1, 2):
                    try:
                        con.execute("CREATE TABLE IF NOT EXISTS scan_results AS SELECT * FROM new_rows WHERE 1=0")
                        con.execute("DELETE FROM scan_results WHERE scan_time = ?", [scan_time.to_pydatetime()])
                        con.execute("INSERT INTO scan_results SELECT * FROM new_rows")
                        break
                    except Exception as e:
                        if attempt == 2:
                            raise
                        LOG.warning("scan_results schema mismatch (%s); recreating table", str(e)[:160])
                        con.execute("DROP TABLE IF EXISTS scan_results")
            finally:
                try:
                    con.unregister("new_rows")
                except Exception:
                    pass

        self._with_write_retry(_write, "save_scan_results")

    def load_latest_scan(self) -> pd.DataFrame:
        # 1) duckdb（只读模式：每次开短连接，避免阻塞写入方）
        try:
            if self.read_only:
                if self.duckdb_path.exists():
                    con = duckdb.connect(str(self.duckdb_path), read_only=True)
                    try:
                        t = con.execute("SELECT max(scan_time) FROM scan_results").fetchone()[0]
                        if t is not None:
                            return con.execute("SELECT * FROM scan_results WHERE scan_time = ?", [t]).fetchdf()
                    finally:
                        con.close()
            else:
                con = self.duck()
                t = con.execute("SELECT max(scan_time) FROM scan_results").fetchone()[0]
                if t is not None:
                    return con.execute("SELECT * FROM scan_results WHERE scan_time = ?", [t]).fetchdf()
        except Exception as e:
            LOG.warning("load_latest_scan from duckdb failed (%s); falling back to parquet", str(e)[:140])
        # 2) parquet snapshot fallback
        snaps = sorted((self.processed / "scans").glob("scan_*.parquet"))
        if snaps:
            try:
                df = pd.read_parquet(snaps[-1])
                return df
            except Exception as e:
                LOG.warning("parquet fallback failed: %s", e)
        return pd.DataFrame()

    def save_case_analysis(self, df: pd.DataFrame) -> None:
        df.to_parquet(self.processed / "case_analysis.parquet", index=False)

        def _write():
            con = self.duck()
            con.register("case_rows", df)
            try:
                con.execute("CREATE OR REPLACE TABLE case_analysis AS SELECT * FROM case_rows")
            finally:
                con.unregister("case_rows")

        self._with_write_retry(_write, "save_case_analysis")

    def refresh_views(self) -> None:
        """(Re)create convenience views over all parquet data."""
        self._with_write_retry(self._refresh_views_impl, "refresh_views")

    def _refresh_views_impl(self) -> None:
        con = self.duck()
        raw = str(self.raw)

        empty_klines_sql = (
            "SELECT NULL::VARCHAR AS symbol, NULL::TIMESTAMP AS open_time, "
            "NULL::DOUBLE AS open, NULL::DOUBLE AS high, NULL::DOUBLE AS low, "
            "NULL::DOUBLE AS close, NULL::DOUBLE AS volume, NULL::TIMESTAMP AS close_time, "
            "NULL::DOUBLE AS quote_volume, NULL::BIGINT AS count, "
            "NULL::DOUBLE AS taker_buy_volume, NULL::DOUBLE AS taker_buy_quote_volume WHERE 1=0"
        )

        def view_klines(name: str, interval: str) -> None:
            glob = f"{raw}/klines/*/{interval}/*.parquet"
            has = any((self.raw / "klines").glob(f"*/{interval}/*.parquet"))
            if not has:
                con.execute(f"CREATE OR REPLACE VIEW {name} AS {empty_klines_sql}")
                return
            con.execute(
                f"""CREATE OR REPLACE VIEW {name} AS
                    SELECT
                      regexp_extract(filename, 'klines/([^/]+)/{interval}/', 1) AS symbol,
                      open_time, open, high, low, close, volume, close_time, quote_volume,
                      count, taker_buy_volume, taker_buy_quote_volume
                    FROM read_parquet('{glob}', union_by_name=true, filename=true)
                """
            )

        for itv in ("15m", "1h", "4h"):
            view_klines(f"klines_{itv}", itv)

        # funding / oi / taker
        for name, sub in (("funding", "funding"), ("open_interest", "open_interest"), ("taker", "taker")):
            has = any((self.raw / sub).glob("*/*.parquet"))
            if not has:
                con.execute(f"CREATE OR REPLACE VIEW {name} AS SELECT NULL AS symbol WHERE 1=0")
                continue
            con.execute(
                f"CREATE OR REPLACE VIEW {name} AS "
                f"SELECT * FROM read_parquet('{raw}/{sub}/*/*.parquet', union_by_name=true)"
            )

        has_mc = any((self.raw / "market_cap" / "snapshots").glob("*.parquet"))
        if has_mc:
            con.execute(
                f"CREATE OR REPLACE VIEW market_cap AS "
                f"SELECT * FROM read_parquet('{raw}/market_cap/snapshots/*.parquet', union_by_name=true)"
            )
        LOG.info("duckdb views refreshed at %s", self.duckdb_path)

    def query(self, sql: str, params: Optional[list] = None) -> pd.DataFrame:
        return self.duck().execute(sql, params or []).fetchdf()

    def close(self) -> None:
        try:
            if self._con is not None:
                self._con.close()
        finally:
            self._con = None
