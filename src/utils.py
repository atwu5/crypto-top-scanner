"""Shared utilities: config loading, logging, HTTP client with retry/rate-limit.

集中管理：配置读取（yaml + .env 覆盖）、日志、HTTP 请求（超时/重试/指数退避/限频）。
"""
from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

import requests
import yaml

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOGGER_NAME = "ctscan"
_LOGGER: Optional[logging.Logger] = None


# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------
def load_config(path: Optional[str] = None) -> Dict[str, Any]:
    """Load config/scanner.yaml and apply .env overrides."""
    if load_dotenv is not None:
        load_dotenv(PROJECT_ROOT / ".env")

    cfg_path = Path(path) if path else PROJECT_ROOT / "config" / "scanner.yaml"
    with open(cfg_path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    # env overrides
    env = os.environ
    ds = cfg.setdefault("data_source", {})
    if env.get("CTSCAN_DATA_SOURCE"):
        ds["mode"] = env["CTSCAN_DATA_SOURCE"]
    api = cfg.setdefault("api", {})
    if env.get("FAPI_BASE"):
        api["fapi_base"] = env["FAPI_BASE"].rstrip("/")
    if env.get("VISION_BASE"):
        api["vision_base"] = env["VISION_BASE"].rstrip("/")
    if env.get("VISION_S3_BASE"):
        api["vision_s3_base"] = env["VISION_S3_BASE"].rstrip("/")
    if env.get("COINGECKO_BASE"):
        api["coingecko_base"] = env["COINGECKO_BASE"].rstrip("/")
    cfg["_project_root"] = str(PROJECT_ROOT)
    return cfg


def project_path(*parts: str) -> Path:
    return PROJECT_ROOT.joinpath(*parts)


# --------------------------------------------------------------------------
# logging
# --------------------------------------------------------------------------
def setup_logging(cfg: Optional[Dict[str, Any]] = None, force: bool = False) -> logging.Logger:
    """Configure the shared logger: file (logs/app.log) + console."""
    global _LOGGER
    if _LOGGER is not None and not force:
        return _LOGGER

    cfg = cfg or {}
    log_cfg = cfg.get("logging", {})
    log_file = log_cfg.get("file", "logs/app.log")
    level = getattr(logging, str(log_cfg.get("level", "INFO")).upper(), logging.INFO)

    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(level)
    logger.handlers.clear()
    logger.propagate = False

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )

    fpath = PROJECT_ROOT / log_file
    fpath.parent.mkdir(parents=True, exist_ok=True)
    fh = logging.FileHandler(fpath, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    _LOGGER = logger
    return logger


def get_logger(name: str = "") -> logging.Logger:
    if _LOGGER is None:
        setup_logging()
    return logging.getLogger(f"{LOGGER_NAME}.{name}" if name else LOGGER_NAME)


# --------------------------------------------------------------------------
# time helpers (all data stored in UTC)
# --------------------------------------------------------------------------
def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def dt_from_ms(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)


def to_epoch_ms(dt: datetime) -> int:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


INTERVAL_DELTA = {
    "1m": timedelta(minutes=1),
    "3m": timedelta(minutes=3),
    "5m": timedelta(minutes=5),
    "15m": timedelta(minutes=15),
    "30m": timedelta(minutes=30),
    "1h": timedelta(hours=1),
    "2h": timedelta(hours=2),
    "4h": timedelta(hours=4),
    "6h": timedelta(hours=6),
    "8h": timedelta(hours=8),
    "12h": timedelta(hours=12),
    "1d": timedelta(days=1),
}


def interval_delta(interval: str) -> timedelta:
    return INTERVAL_DELTA.get(interval, timedelta(hours=4))


# --------------------------------------------------------------------------
# http with retry / backoff / rate limit
# --------------------------------------------------------------------------
class RateLimiter:
    """Thread-safe sliding-window rate limiter (requests per minute)."""

    def __init__(self, per_minute: int):
        self.per_minute = max(1, int(per_minute))
        self._lock = threading.Lock()
        self._events: list = []

    def acquire(self) -> None:
        while True:
            now = time.monotonic()
            with self._lock:
                cutoff = now - 60.0
                self._events = [t for t in self._events if t > cutoff]
                if len(self._events) < self.per_minute:
                    self._events.append(now)
                    return
                wait = 60.0 - (now - self._events[0])
            time.sleep(max(0.05, min(wait, 5.0)))


class HttpClient:
    """HTTP wrapper with timeout / retry / exponential backoff / rate limit."""

    def __init__(
        self,
        timeout: float = 25.0,
        max_retries: int = 5,
        backoff_base: float = 0.8,
        backoff_max: float = 20.0,
        rate_per_minute: int = 1200,
        user_agent: str = "crypto-top-scanner/0.1",
        logger: Optional[logging.Logger] = None,
    ):
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.backoff_max = backoff_max
        self.limiter = RateLimiter(rate_per_minute)
        self.log = logger or get_logger("http")
        self.session = requests.Session()
        adapter = requests.adapters.HTTPAdapter(pool_connections=64, pool_maxsize=64)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)
        self.session.headers.update({"User-Agent": user_agent})

    def get(
        self,
        url: str,
        params: Optional[dict] = None,
        timeout: Optional[float] = None,
        as_json: bool = False,
        ok_statuses: Iterable[int] = (200,),
        raise_on_error: bool = False,
    ):
        """GET with retries. Returns response body (bytes/text/json) or None on failure."""
        attempts = 0
        last_err: Optional[str] = None
        while attempts <= self.max_retries:
            attempts += 1
            self.limiter.acquire()
            try:
                r = self.session.get(url, params=params, timeout=timeout or self.timeout)
            except requests.RequestException as e:
                last_err = f"request error: {e}"
                self._sleep_backoff(attempts)
                continue

            if r.status_code in ok_statuses:
                if as_json:
                    try:
                        return r.json()
                    except ValueError:
                        last_err = "invalid json"
                        return None
                return r.content

            if r.status_code == 404:
                return None

            if r.status_code in (429, 418):  # rate limited / banned
                retry_after = r.headers.get("Retry-After")
                wait = float(retry_after) if retry_after and retry_after.isdigit() else None
                self.log.warning("rate limited %s on %s (attempt %s)", r.status_code, url, attempts)
                if wait:
                    time.sleep(min(wait, 60))
                else:
                    self._sleep_backoff(attempts, mul=2.0)
                continue

            if r.status_code >= 500:
                last_err = f"http {r.status_code}"
                self._sleep_backoff(attempts)
                continue

            # other 4xx
            last_err = f"http {r.status_code}: {r.text[:200] if r.text else ''}"
            if raise_on_error:
                raise RuntimeError(f"GET {url} -> {last_err}")
            return None

        self.log.error("GET failed after %s attempts: %s (%s)", attempts - 1, url, last_err)
        if raise_on_error:
            raise RuntimeError(f"GET {url} failed: {last_err}")
        return None

    def _sleep_backoff(self, attempt: int, mul: float = 1.0) -> None:
        delay = min(self.backoff_max, self.backoff_base * (2 ** (attempt - 1)) * mul)
        time.sleep(delay)


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------
def ensure_dir(p: Path) -> Path:
    p.mkdir(parents=True, exist_ok=True)
    return p


def safe_div(a, b):
    try:
        if b in (0, None):
            return None
        return a / b
    except Exception:
        return None


def fmt_pct(x, digits: int = 2) -> str:
    if x is None:
        return "-"
    return f"{x * 100:.{digits}f}%"


def fmt_num(x, digits: int = 4) -> str:
    if x is None:
        return "-"
    if isinstance(x, str):
        return x
    if abs(x) >= 1000:
        return f"{x:,.0f}"
    if abs(x) >= 1:
        return f"{x:.{min(digits, 3)}f}"
    return f"{x:.{digits + 2}f}"


def floor_dt(dt: datetime, interval: str) -> datetime:
    """Floor a datetime to the interval boundary (UTC)."""
    d = interval_delta(interval)
    total = int(d.total_seconds())
    ts = int(dt.timestamp())
    return datetime.fromtimestamp(ts - ts % total, tz=timezone.utc)
