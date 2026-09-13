"""Binance market-data access layer.

Two interchangeable sources (data_source.mode in config/scanner.yaml):

- ``BinanceFapiClient``  — Binance USDT-M Futures REST (fapi.binance.com)
- ``BinanceVisionClient`` — Binance public data archive (data.binance.vision),
  data.binance.vision 公共归档（S3 风格列表 + 按文件下载 zip）

``MarketDataSource`` 是统一门面：auto 模式下先探测 fapi，可达则优先使用；
不可达（或配置指定）时回退到公共归档。所有 URL / endpoint / 参数集中在本模块，
业务代码不散落任何接口细节；官方接口变更时只需修改这里。
"""
from __future__ import annotations

import csv
import io
import json
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import quote

import pandas as pd

from .utils import (
    HttpClient,
    dt_from_ms,
    get_logger,
    interval_delta,
    to_epoch_ms,
    utcnow,
)

KLINE_COLS = [
    "open_time", "open", "high", "low", "close", "volume",
    "close_time", "quote_volume", "count",
    "taker_buy_volume", "taker_buy_quote_volume", "ignore",
]

FUNDING_COLS_VISION = ["calc_time", "funding_interval_hours", "last_funding_rate"]
METRICS_COLS_VISION = [
    "create_time", "symbol", "sum_open_interest", "sum_open_interest_value",
    "count_toptrader_long_short_ratio", "sum_toptrader_long_short_ratio",
    "count_long_short_ratio", "sum_taker_long_short_vol_ratio",
]

METRICS_STD_COLS = ["time", "open_interest", "open_interest_value", "taker_ratio"]


def _parse_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _rows_to_df(rows: Sequence[Sequence[str]], cols: List[str]) -> pd.DataFrame:
    """CSV rows -> DataFrame; auto-skips a header row, drops blank rows."""
    clean: List[List[str]] = []
    for r in rows:
        if not r or len(r) < 2:
            continue
        if r[0] in ("open_time", "calc_time", "create_time"):  # header
            continue
        clean.append(list(r))
    return pd.DataFrame(clean, columns=cols[: len(clean[0])] if clean else cols)


def parse_fapi_klines(rows: List[list]) -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=KLINE_COLS)
    if df.empty:
        return _empty_klines()
    for c in ["open", "high", "low", "close", "volume", "quote_volume",
              "taker_buy_volume", "taker_buy_quote_volume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["open_time"] = pd.to_datetime(df["open_time"].astype("int64"), unit="ms", utc=True)
    df["close_time"] = pd.to_datetime(df["close_time"].astype("int64"), unit="ms", utc=True)
    df["count"] = pd.to_numeric(df["count"], errors="coerce").fillna(0).astype("int64")
    return df[KLINE_COLS[:-1]].drop_duplicates("open_time").sort_values("open_time").reset_index(drop=True)


def parse_fapi_funding(rows: List[dict], symbol: str) -> pd.DataFrame:
    if not rows:
        return _empty_funding()
    df = pd.DataFrame(rows)
    df["symbol"] = symbol
    df["funding_time"] = pd.to_datetime(df["fundingTime"].astype("int64"), unit="ms", utc=True)
    df["funding_rate"] = pd.to_numeric(df["fundingRate"], errors="coerce")
    return (df[["symbol", "funding_time", "funding_rate"]]
            .drop_duplicates("funding_time").sort_values("funding_time").reset_index(drop=True))


def parse_fapi_oi(rows: List[dict], symbol: str) -> pd.DataFrame:
    if not rows:
        return _empty_oi()
    df = pd.DataFrame(rows)
    df["symbol"] = symbol
    df["time"] = pd.to_datetime(df["timestamp"].astype("int64"), unit="ms", utc=True)
    df["open_interest"] = pd.to_numeric(df["sumOpenInterest"], errors="coerce")
    df["open_interest_value"] = pd.to_numeric(df["sumOpenInterestValue"], errors="coerce")
    return (df[["symbol", "time", "open_interest", "open_interest_value"]]
            .drop_duplicates("time").sort_values("time").reset_index(drop=True))


def parse_fapi_taker(rows: List[dict], symbol: str) -> pd.DataFrame:
    if not rows:
        return _empty_taker()
    df = pd.DataFrame(rows)
    df["symbol"] = symbol
    df["time"] = pd.to_datetime(df["timestamp"].astype("int64"), unit="ms", utc=True)
    df["taker_ratio"] = pd.to_numeric(df["buySellRatio"], errors="coerce")
    return (df[["symbol", "time", "taker_ratio"]]
            .drop_duplicates("time").sort_values("time").reset_index(drop=True))


def _empty_klines() -> pd.DataFrame:
    return pd.DataFrame(columns=KLINE_COLS[:-1])


def _empty_funding() -> pd.DataFrame:
    return pd.DataFrame(columns=["symbol", "funding_time", "funding_rate"])


def _empty_oi() -> pd.DataFrame:
    return pd.DataFrame(columns=["symbol", "time", "open_interest", "open_interest_value"])


def _empty_taker() -> pd.DataFrame:
    return pd.DataFrame(columns=["symbol", "time", "taker_ratio"])


def _empty_metrics() -> pd.DataFrame:
    return pd.DataFrame(columns=METRICS_STD_COLS)


# ===========================================================================
# fapi (Binance USDT-M Futures REST)
# ===========================================================================
class BinanceFapiClient:
    """Binance USDT-M Futures REST client (fapi).

    endpoints 集中在 EP 字典；如需替换接口，只改这里。
    """

    EP = {
        "ping": "/fapi/v1/ping",
        "exchange_info": "/fapi/v1/exchangeInfo",
        "klines": "/fapi/v1/klines",
        "funding_rate": "/fapi/v1/fundingRate",
        "oi_hist": "/futures/data/openInterestHist",
        "taker_ratio": "/futures/data/takerlongshortRatio",
        "ticker_24hr": "/fapi/v1/ticker/24hr",
    }

    def __init__(self, api_cfg: Dict[str, Any]):
        self.base = api_cfg["fapi_base"].rstrip("/")
        self.http = HttpClient(
            timeout=api_cfg.get("timeout_seconds", 25),
            max_retries=api_cfg.get("max_retries", 5),
            backoff_base=api_cfg.get("backoff_base_seconds", 0.8),
            backoff_max=api_cfg.get("backoff_max_seconds", 20),
            rate_per_minute=api_cfg.get("rate_limit_per_minute", 1500),
            user_agent=api_cfg.get("user_agent", "crypto-top-scanner"),
            logger=get_logger("fapi"),
        )
        self.log = get_logger("fapi")

    def url(self, key: str) -> str:
        return self.base + self.EP[key]

    def ping(self, timeout: float = 5.0) -> bool:
        try:
            r = self.http.session.get(self.url("ping"), timeout=timeout)
            return r.status_code == 200
        except Exception:
            return False

    def exchange_info(self) -> Optional[dict]:
        return self.http.get(self.url("exchange_info"), as_json=True)

    def ticker_24hr(self):
        return self.http.get(self.url("ticker_24hr"), as_json=True)

    def klines(self, symbol: str, interval: str, start_ms: Optional[int] = None,
               end_ms: Optional[int] = None, limit: int = 1500) -> List[list]:
        params = {"symbol": symbol, "interval": interval, "limit": limit}
        if start_ms is not None:
            params["startTime"] = start_ms
        if end_ms is not None:
            params["endTime"] = end_ms
        return self.http.get(self.url("klines"), params=params, as_json=True) or []

    def klines_range(self, symbol: str, interval: str, start: datetime, end: datetime) -> pd.DataFrame:
        start_ms, end_ms = to_epoch_ms(start), to_epoch_ms(end)
        out: List[list] = []
        cursor = start_ms
        guard = 0
        while cursor < end_ms and guard < 500:
            guard += 1
            rows = self.klines(symbol, interval, start_ms=cursor, end_ms=end_ms, limit=1500)
            if not rows:
                break
            out.extend(rows)
            last_open = int(rows[-1][0])
            new_cursor = last_open + int(interval_delta(interval).total_seconds() * 1000)
            if new_cursor <= cursor:
                break
            cursor = new_cursor
            if len(rows) < 1500:
                break
        if not out:
            return _empty_klines()
        return parse_fapi_klines(out)

    def funding_range(self, symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
        out: List[dict] = []
        cursor = to_epoch_ms(start)
        end_ms = to_epoch_ms(end)
        guard = 0
        while cursor < end_ms and guard < 100:
            guard += 1
            rows = self.http.get(
                self.url("funding_rate"),
                params={"symbol": symbol, "startTime": cursor, "endTime": end_ms, "limit": 1000},
                as_json=True,
            ) or []
            if not rows:
                break
            out.extend(rows)
            last = int(rows[-1]["fundingTime"])
            if last <= cursor:
                break
            cursor = last + 1
            if len(rows) < 1000:
                break
        return parse_fapi_funding(out, symbol)

    def open_interest(self, symbol: str, period: str, start: datetime, end: datetime) -> pd.DataFrame:
        rows = self.http.get(
            self.url("oi_hist"),
            params={"symbol": symbol, "period": period,
                    "startTime": to_epoch_ms(start), "endTime": to_epoch_ms(end), "limit": 500},
            as_json=True,
        ) or []
        return parse_fapi_oi(rows, symbol)

    def taker(self, symbol: str, period: str, start: datetime, end: datetime) -> pd.DataFrame:
        rows = self.http.get(
            self.url("taker_ratio"),
            params={"symbol": symbol, "period": period,
                    "startTime": to_epoch_ms(start), "endTime": to_epoch_ms(end), "limit": 500},
            as_json=True,
        ) or []
        return parse_fapi_taker(rows, symbol)

    def metrics_range(self, symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
        """OI + taker ratio merged to a common (hourly) timeline."""
        oi = self.open_interest(symbol, "1h", start, end)
        tk = self.taker(symbol, "1h", start, end)
        if oi.empty and tk.empty:
            return _empty_metrics()
        if oi.empty:
            return tk.rename(columns={"taker_ratio": "taker_ratio"})[["time", "taker_ratio"]]
        oi = oi.rename(columns={"symbol": "symbol"})
        if not tk.empty:
            oi = oi.merge(tk[["time", "taker_ratio"]], on="time", how="left")
        else:
            oi["taker_ratio"] = None
        return oi[["time", "open_interest", "open_interest_value", "taker_ratio"]].reset_index(drop=True)


# ===========================================================================
# vision (public data archive)
# ===========================================================================
class BinanceVisionClient:
    """Binance public data archive client (data.binance.vision).

    - S3 风格列表接口枚举全部合约与文件
    - 历史 K 线 / Funding / Metrics 按 (月 / 日) zip 文件下载并解析
    - 下载结果按文件级缓存，重复请求不会重复下载
    """

    def __init__(self, api_cfg: Dict[str, Any], vision_cfg: Dict[str, Any]):
        self.base = api_cfg["vision_base"].rstrip("/")
        self.s3 = api_cfg["vision_s3_base"].rstrip("/")
        self.lag_days = int(vision_cfg.get("data_lag_days", 2))
        self.workers = int(api_cfg.get("vision_workers", 12))
        self.http = HttpClient(
            timeout=api_cfg.get("timeout_seconds", 25),
            max_retries=api_cfg.get("max_retries", 5),
            backoff_base=api_cfg.get("backoff_base_seconds", 0.8),
            backoff_max=api_cfg.get("backoff_max_seconds", 20),
            rate_per_minute=api_cfg.get("vision_max_requests_per_minute", 1200),
            user_agent=api_cfg.get("user_agent", "crypto-top-scanner"),
            logger=get_logger("vision"),
        )
        self.log = get_logger("vision")
        self._file_cache: Dict[str, Optional[List[List[str]]]] = {}
        self._list_cache: Dict[str, Any] = {}

    # ---------------- URL builders ----------------
    def klines_url(self, symbol: str, interval: str, period: str, granularity: str = "monthly") -> str:
        return f"{self.base}/data/futures/um/{granularity}/klines/{symbol}/{interval}/{symbol}-{interval}-{period}.zip"

    def funding_url(self, symbol: str, period: str, granularity: str = "monthly") -> str:
        return f"{self.base}/data/futures/um/{granularity}/fundingRate/{symbol}/{symbol}-fundingRate-{period}.zip"

    def metrics_url(self, symbol: str, period: str, granularity: str = "daily") -> str:
        return f"{self.base}/data/futures/um/{granularity}/metrics/{symbol}/{symbol}-metrics-{period}.zip"

    # ---------------- listing ----------------
    def list_prefixes(self, prefix: str) -> List[str]:
        key = f"prefixes::{prefix}"
        if key in self._list_cache:
            return self._list_cache[key]
        out: List[str] = []
        marker = None
        for _ in range(500):
            params = {"delimiter": "/", "prefix": prefix, "max-keys": 1000}
            if marker:
                params["marker"] = marker
            data = self.http.get(self.s3, params=params)
            if data is None:
                break
            try:
                import xml.etree.ElementTree as ET

                root = ET.fromstring(data)
                ns = "{http://s3.amazonaws.com/doc/2006-03-01/}"
                out.extend(c.find(f"{ns}Prefix").text for c in root.findall(f"{ns}CommonPrefixes"))
                truncated = root.find(f"{ns}IsTruncated")
                if truncated is None or truncated.text != "true":
                    break
                nm = root.find(f"{ns}NextMarker")
                marker = nm.text if nm is not None and nm.text else None
                if not marker:
                    break
            except Exception as e:  # parse error
                self.log.warning("s3 list parse error for %s: %s", prefix, e)
                break
        self._list_cache[key] = out
        return out

    def list_all_symbols(self) -> List[str]:
        prefixes = self.list_prefixes("data/futures/um/monthly/klines/")
        return sorted({p.rstrip("/").split("/")[-1] for p in prefixes})

    # ---------------- file fetch / parse ----------------
    def _fetch_zip_rows(self, url: str) -> Optional[List[List[str]]]:
        if url in self._file_cache:
            return self._file_cache[url]
        raw = self.http.get(url)
        if raw is None:  # 404 or failure
            self._file_cache[url] = None
            return None
        try:
            zf = zipfile.ZipFile(io.BytesIO(raw))
            name = zf.namelist()[0]
            text = zf.read(name).decode("utf-8", errors="replace")
            rows = list(csv.reader(io.StringIO(text)))
            self._file_cache[url] = rows
            return rows
        except Exception as e:
            self.log.warning("zip parse failed %s: %s", url, e)
            self._file_cache[url] = None
            return None

    # ---------------- klines ----------------
    @staticmethod
    def _month_end(y: int, m: int) -> date:
        if m == 12:
            return date(y + 1, 1, 1) - timedelta(days=1)
        return date(y, m + 1, 1) - timedelta(days=1)

    def _plan_kline_files(self, symbol: str, interval: str, start: datetime,
                          end: datetime) -> List[Tuple[str, str]]:
        """Return list of (granularity, period) to try, covering [start, end]."""
        plans: List[Tuple[str, str]] = []
        today = utcnow().date()
        cur = date(start.year, start.month, 1)
        last_month = date(end.year, end.month, 1)
        while cur <= last_month:
            ym = f"{cur.year:04d}-{cur.month:02d}"
            m_end = self._month_end(cur.year, cur.month)
            is_current_month = (cur.year == today.year and cur.month == today.month)
            if not is_current_month:
                plans.append(("monthly", ym))
            else:
                # 当月：按天，最后可用日 = min(today - lag, 月末, end)
                last_day = min(today - timedelta(days=self.lag_days), m_end, end.date())
                if cur.year == start.year and cur.month == start.month:
                    day = max(date(cur.year, cur.month, 1), start.date())
                else:
                    day = date(cur.year, cur.month, 1)
                while day <= last_day:
                    plans.append(("daily", day.strftime("%Y-%m-%d")))
                    day += timedelta(days=1)
            # next month
            cur = date(cur.year + 1, 1, 1) if cur.month == 12 else date(cur.year, cur.month + 1, 1)
        return plans

    def klines(self, symbol: str, interval: str, start: datetime, end: datetime) -> pd.DataFrame:
        plans = self._plan_kline_files(symbol, interval, start, end)
        frames: List[pd.DataFrame] = []
        for gran, period in plans:
            url = self.klines_url(symbol, interval, period, gran)
            rows = self._fetch_zip_rows(url)
            if rows is None and gran == "monthly":
                # 月包缺失：可能该月未生成（新月）或该合约本月按天存储；按天兜底
                y, m = int(period[:4]), int(period[5:7])
                first = date(y, m, 1)
                if first < start.date() and (y, m) == (start.year, start.month):
                    first = start.date()
                last_day = min(utcnow().date() - timedelta(days=self.lag_days),
                               end.date(), self._month_end(y, m))
                d = first
                while d <= last_day:
                    u2 = self.klines_url(symbol, interval, d.strftime("%Y-%m-%d"), "daily")
                    r2 = self._fetch_zip_rows(u2)
                    if r2:
                        df2 = self._parse_vision_klines(r2)
                        if not df2.empty:
                            frames.append(df2)
                    d += timedelta(days=1)
                continue
            if not rows:
                continue
            df = self._parse_vision_klines(rows)
            if not df.empty:
                frames.append(df)
        if not frames:
            return _empty_klines()
        out = pd.concat(frames, ignore_index=True).drop_duplicates("open_time").sort_values("open_time")
        mask = (out["open_time"] >= start) & (out["open_time"] <= end)
        return out.loc[mask].reset_index(drop=True)

    @staticmethod
    def _parse_vision_klines(rows: List[List[str]]) -> pd.DataFrame:
        df = _rows_to_df(rows, KLINE_COLS)
        if df.empty:
            return _empty_klines()
        for c in ["open", "high", "low", "close", "volume", "quote_volume",
                  "taker_buy_volume", "taker_buy_quote_volume"]:
            df[c] = pd.to_numeric(df[c], errors="coerce")
        t = pd.to_numeric(df["open_time"], errors="coerce").astype("Int64")
        # 归档数据可能为 ms 或 us
        mx = t.max()
        unit = "us" if mx is not None and mx > 10**14 else "ms"
        df["open_time"] = pd.to_datetime(t.astype("int64"), unit=unit, utc=True)
        ct = pd.to_numeric(df["close_time"], errors="coerce").astype("Int64")
        df["close_time"] = pd.to_datetime(ct.astype("int64"), unit=unit, utc=True)
        df["count"] = pd.to_numeric(df["count"], errors="coerce").fillna(0).astype("int64")
        return df[KLINE_COLS[:-1]]

    # ---------------- funding ----------------
    def funding(self, symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
        cur = date(start.year, start.month, 1)
        last_month = date(end.year, end.month, 1)
        out: List[pd.DataFrame] = []
        while cur <= last_month:
            ym = f"{cur.year:04d}-{cur.month:02d}"
            rows = self._fetch_zip_rows(self.funding_url(symbol, ym))
            if rows is None:
                # 当月可能还没有月包：尝试按天（多数不可用则跳过）
                pass
            if rows:
                out.append(self._parse_vision_funding(rows, symbol))
            cur = date(cur.year + 1, 1, 1) if cur.month == 12 else date(cur.year, cur.month + 1, 1)
        if not out:
            return _empty_funding()
        df = pd.concat(out, ignore_index=True).drop_duplicates("funding_time").sort_values("funding_time")
        mask = (df["funding_time"] >= start) & (df["funding_time"] <= end)
        return df.loc[mask].reset_index(drop=True)

    @staticmethod
    def _parse_vision_funding(rows: List[List[str]], symbol: str) -> pd.DataFrame:
        df = _rows_to_df(rows, FUNDING_COLS_VISION)
        if df.empty:
            return _empty_funding()
        t = pd.to_numeric(df["calc_time"], errors="coerce").astype("Int64")
        mx = t.max()
        unit = "us" if mx is not None and mx > 10**14 else "ms"
        df["funding_time"] = pd.to_datetime(t.astype("int64"), unit=unit, utc=True)
        df["funding_rate"] = pd.to_numeric(df["last_funding_rate"], errors="coerce")
        df["symbol"] = symbol
        return df[["symbol", "funding_time", "funding_rate"]]

    # ---------------- metrics (OI / taker) ----------------
    def metrics(self, symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
        d = start.date()
        last_day = min(utcnow().date() - timedelta(days=self.lag_days), end.date())
        out: List[pd.DataFrame] = []
        while d <= last_day:
            rows = self._fetch_zip_rows(self.metrics_url(symbol, d.strftime("%Y-%m-%d")))
            if rows:
                out.append(self._parse_vision_metrics(rows))
            d += timedelta(days=1)
        if not out:
            return _empty_metrics()
        df = pd.concat(out, ignore_index=True).drop_duplicates("time").sort_values("time")
        mask = (df["time"] >= start) & (df["time"] <= end)
        return df.loc[mask].reset_index(drop=True)

    @staticmethod
    def _parse_vision_metrics(rows: List[List[str]]) -> pd.DataFrame:
        df = _rows_to_df(rows, METRICS_COLS_VISION)
        if df.empty:
            return _empty_metrics()
        df["time"] = pd.to_datetime(df["create_time"], utc=True, errors="coerce")
        df["open_interest"] = pd.to_numeric(df["sum_open_interest"], errors="coerce")
        df["open_interest_value"] = pd.to_numeric(df["sum_open_interest_value"], errors="coerce")
        df["taker_ratio"] = pd.to_numeric(df["sum_taker_long_short_vol_ratio"], errors="coerce")
        return df[["time", "open_interest", "open_interest_value", "taker_ratio"]].dropna(subset=["time"])


# ===========================================================================
# facade
# ===========================================================================
class MarketDataSource:
    """Unified market data facade (fapi preferred, vision fallback)."""

    def __init__(self, cfg: Dict[str, Any]):
        self.cfg = cfg
        self.api_cfg = cfg["api"]
        self.log = get_logger("source")
        self.fapi = BinanceFapiClient(self.api_cfg)
        self.vision = BinanceVisionClient(self.api_cfg, cfg.get("data_source", {}))
        self._mode: Optional[str] = None

    # ---------------- mode ----------------
    @property
    def mode(self) -> str:
        if self._mode is None:
            self._mode = self.resolve_mode()
        return self._mode

    def resolve_mode(self) -> str:
        want = str(self.cfg.get("data_source", {}).get("mode", "auto")).lower()
        if want == "fapi":
            if not self.fapi.ping(self.cfg.get("data_source", {}).get("fapi_probe_timeout_seconds", 5)):
                self.log.warning("fapi unreachable; continuing anyway (mode=fapi forced)")
            return "fapi"
        if want == "vision":
            return "vision"
        # auto
        if self.fapi.ping(self.cfg.get("data_source", {}).get("fapi_probe_timeout_seconds", 5)):
            self.log.info("data source: fapi (binance futures REST reachable)")
            return "fapi"
        self.log.warning("fapi unreachable -> falling back to data.binance.vision archive")
        return "vision"

    # ---------------- universe ----------------
    def universe(self) -> pd.DataFrame:
        ucfg = self.cfg.get("universe", {})
        quote = ucfg.get("quote_asset", "USDT")
        excludes = tuple(ucfg.get("exclude_suffixes", []))
        if self.mode == "fapi":
            info = self.fapi.exchange_info() or {}
            rows = []
            for s in info.get("symbols", []):
                if s.get("quoteAsset") != quote:
                    continue
                if str(s.get("contractType", "")).upper() != "PERPETUAL":
                    continue
                if s.get("status") != "TRADING":
                    continue
                sym = s["symbol"]
                if excludes and any(sym.endswith(e) for e in excludes):
                    continue
                rows.append({
                    "symbol": sym,
                    "base_asset": s.get("baseAsset"),
                    "quote_asset": s.get("quoteAsset"),
                    "contract_type": s.get("contractType"),
                    "status": s.get("status"),
                    "onboard_date": pd.to_datetime(s.get("onboardDate"), unit="ms", utc=True)
                    if s.get("onboardDate") else pd.NaT,
                })
            df = pd.DataFrame(rows)
        else:
            syms = self.vision.list_all_symbols()
            rows = []
            for sym in syms:
                if not sym.endswith(quote):
                    continue
                if excludes and any(sym.endswith(e) for e in excludes):
                    continue
                rows.append({
                    "symbol": sym,
                    "base_asset": sym[: -len(quote)],
                    "quote_asset": quote,
                    "contract_type": "PERPETUAL",
                    # 归档模式无法读取 exchangeInfo，状态按在架推断
                    "status": "TRADING*",
                    "onboard_date": pd.NaT,
                })
            df = pd.DataFrame(rows)
        limit = int(ucfg.get("limit_symbols", 0) or 0)
        if limit > 0:
            df = df.head(limit)
        return df.reset_index(drop=True)

    # ---------------- market data ----------------
    def klines(self, symbol: str, interval: str, start: datetime, end: datetime) -> pd.DataFrame:
        if self.mode == "fapi":
            return self.fapi.klines_range(symbol, interval, start, end)
        return self.vision.klines(symbol, interval, start, end)

    def funding(self, symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
        if self.mode == "fapi":
            return self.fapi.funding_range(symbol, start, end)
        return self.vision.funding(symbol, start, end)

    def metrics(self, symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
        if self.mode == "fapi":
            return self.fapi.metrics_range(symbol, start, end)
        return self.vision.metrics(symbol, start, end)

    def ticker_24hr(self) -> Optional[pd.DataFrame]:
        """24h ticker snapshot — fapi only; None in vision mode."""
        if self.mode != "fapi":
            return None
        rows = self.fapi.ticker_24hr()
        if not rows:
            return None
        df = pd.DataFrame(rows)
        return df
