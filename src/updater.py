"""Incremental data updater.

核心要求：每次运行只下载缺失的数据，绝不重新全量拉取。
- 读取本地最后时间 -> 只请求之后的数据
- 写入按 symbol+interval+timestamp 去重（storage 层保证）
- 单个币失败不影响整体（最后汇总失败列表）
"""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

import pandas as pd

from .binance_client import MarketDataSource
from .storage import Storage
from .utils import get_logger, interval_delta, utcnow

LOG = get_logger("updater")


def _lookback_start(now: datetime, spec: Dict[str, int]) -> datetime:
    if "days" in spec:
        return now - timedelta(days=int(spec["days"]))
    if "months" in spec:
        m = int(spec["months"])
        y = now.year
        mo = now.month - m
        while mo <= 0:
            mo += 12
            y -= 1
        return now.replace(year=y, month=mo, day=1, hour=0, minute=0, second=0, microsecond=0)
    return now - timedelta(days=30)


class Updater:
    def __init__(self, cfg: Dict[str, Any], storage: Storage, source: MarketDataSource):
        self.cfg = cfg
        self.storage = storage
        self.source = source
        self.failures: List[Tuple[str, str]] = []
        self._fail_lock = threading.Lock()

    # ------------------------------------------------------------------
    def update_universe(self) -> pd.DataFrame:
        df = self.source.universe()
        if not df.empty:
            self.storage.save_universe(df)
            LOG.info("universe refreshed: %s symbols (%s mode)", len(df), self.source.mode)
        else:
            LOG.warning("universe fetch returned empty")
        return df

    # ------------------------------------------------------------------
    def _update_klines(self, symbol: str, interval: str, now: datetime,
                       stats: Dict[str, int]) -> None:
        last = self.storage.last_kline_time(symbol, interval)
        if last is None:
            start = _lookback_start(now, self.cfg["lookback"]["klines"].get(interval, {"days": 30}))
        else:
            # 从最后一根 K 线的下一根开始（含重叠，由去重兜底）
            start = last.to_pydatetime() + interval_delta(interval)
            if start >= now:
                return
        df = self.source.klines(symbol, interval, start, now)
        if df is not None and not df.empty:
            added = self.storage.save_klines(symbol, interval, df)
            stats[f"klines_{interval}_rows"] = stats.get(f"klines_{interval}_rows", 0) + added

    def _update_funding(self, symbol: str, now: datetime, stats: Dict[str, int]) -> None:
        f = self.storage.load_funding(symbol)
        if f.empty:
            start = _lookback_start(now, self.cfg["lookback"].get("funding", {"months": 4}))
        else:
            last = pd.Timestamp(f["funding_time"].max())
            start = last.to_pydatetime() - timedelta(hours=8)  # 含重叠防遗漏
            if start >= now:
                return
        df = self.source.funding(symbol, start, now)
        if df is not None and not df.empty:
            added = self.storage.save_funding(symbol, df)
            stats["funding_rows"] = stats.get("funding_rows", 0) + added

    def _update_metrics(self, symbol: str, now: datetime, stats: Dict[str, int]) -> None:
        oi = self.storage.load_oi(symbol)
        if oi.empty:
            start = _lookback_start(now, self.cfg["lookback"].get("metrics", {"days": 8}))
        else:
            last = pd.Timestamp(oi["time"].max())
            start = last.to_pydatetime() - timedelta(hours=2)
            if start >= now:
                return
        df = self.source.metrics(symbol, start, now)
        if df is None or df.empty:
            return
        if "open_interest" in df.columns:
            added = self.storage.save_oi(symbol, df[["time", "open_interest", "open_interest_value"]])
            stats["oi_rows"] = stats.get("oi_rows", 0) + added
        if "taker_ratio" in df.columns:
            added = self.storage.save_taker(symbol, df[["time", "taker_ratio"]])
            stats["taker_rows"] = stats.get("taker_rows", 0) + added

    # ------------------------------------------------------------------
    def update_symbol(self, symbol: str, now: Optional[datetime] = None) -> Dict[str, int]:
        now = now or utcnow()
        stats: Dict[str, int] = {}
        errors: List[str] = []
        try:
            for interval in self.cfg.get("intervals", ["15m", "1h", "4h"]):
                try:
                    self._update_klines(symbol, interval, now, stats)
                except Exception as e:
                    errors.append(f"klines {interval}: {e}")
            try:
                self._update_funding(symbol, now, stats)
            except Exception as e:
                errors.append(f"funding: {e}")
            try:
                self._update_metrics(symbol, now, stats)
            except Exception as e:
                errors.append(f"metrics: {e}")
        except Exception as e:
            errors.append(f"fatal: {e}")
        if errors:
            with self._fail_lock:
                self.failures.append((symbol, "; ".join(errors)))
            LOG.warning("symbol %s updated with errors: %s", symbol, "; ".join(errors))
        return stats

    # ------------------------------------------------------------------
    def update_all(self, symbols: Optional[List[str]] = None,
                   max_workers: Optional[int] = None,
                   progress_every: int = 50) -> Dict[str, Any]:
        t0 = time.time()
        uni = self.storage.load_universe()
        if symbols is None:
            symbols = uni["symbol"].tolist() if not uni.empty else []
        if not symbols:
            LOG.warning("no symbols to update")
            return {"symbols": 0, "failures": [], "rows": {}, "seconds": 0.0}

        workers = int(max_workers or self.cfg["api"].get("vision_workers", 8))
        workers = max(1, min(workers, 24))
        total_rows: Dict[str, int] = {}
        done = 0
        LOG.info("updating %s symbols with %s workers (mode=%s)...", len(symbols), workers, self.source.mode)

        def work(sym: str) -> Dict[str, int]:
            return self.update_symbol(sym)

        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(work, s): s for s in symbols}
            for fut in as_completed(futs):
                done += 1
                try:
                    st = fut.result()
                except Exception as e:  # pragma: no cover
                    st = {}
                    with self._fail_lock:
                        self.failures.append((futs[fut], str(e)))
                for k, v in (st or {}).items():
                    total_rows[k] = total_rows.get(k, 0) + v
                if done % progress_every == 0:
                    LOG.info("update progress: %s/%s symbols, %s rows", done, len(symbols),
                             sum(total_rows.values()))

        result = {
            "symbols": len(symbols),
            "failures": list(self.failures),
            "rows": total_rows,
            "seconds": round(time.time() - t0, 1),
        }
        LOG.info("update done: %s symbols, %s failures, %s rows, %.1fs",
                 result["symbols"], len(result["failures"]), sum(total_rows.values()), result["seconds"])
        return result
