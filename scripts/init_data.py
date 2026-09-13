#!/usr/bin/env python3
"""初始化数据：拉取全量 Universe + 按 lookback 范围下载历史数据。

用法:
    python scripts/init_data.py            # 全量初始化（首次运行）
    python scripts/init_data.py --limit 50 # 只处理前 50 个 symbol（调试）

之后日常只用 python main.py scan（自动增量）。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.binance_client import MarketDataSource
from src.storage import Storage
from src.updater import Updater
from src.utils import load_config, setup_logging


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 个 symbol")
    ap.add_argument("--symbols", default=None, help="逗号分隔的 symbol 列表")
    ap.add_argument("--workers", type=int, default=0)
    args = ap.parse_args()

    cfg = load_config()
    log = setup_logging(cfg)

    storage = Storage(cfg)
    source = MarketDataSource(cfg)
    updater = Updater(cfg, storage, source)

    log.info("== INIT DATA ==")
    log.info("data source mode: %s", source.mode)
    uni = updater.update_universe()
    syms = uni["symbol"].tolist() if not uni.empty else []
    if args.symbols:
        syms = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    if args.limit:
        syms = syms[: args.limit]
    log.info("symbols to init: %s", len(syms))

    res = updater.update_all(syms, max_workers=args.workers or None, progress_every=25)

    try:
        storage.refresh_views()
    except Exception as e:
        print(f"[warn] refresh_views failed ({e}); 扫描时会自动重建视图")
    print()
    print("=" * 70)
    print(f"INIT DONE: {res['symbols']} symbols, {len(res['failures'])} failures, "
          f"{sum(res['rows'].values())} rows, {res['seconds']}s")
    for k, v in sorted(res["rows"].items()):
        print(f"   {k:28s} +{v}")
    if res["failures"]:
        print("failures (first 10):")
        for s, e in res["failures"][:10]:
            print(f"   {s}: {e[:140]}")
    print("=" * 70)
    print("下一步:  python main.py scan")
    return 0


if __name__ == "__main__":
    sys.exit(main())
