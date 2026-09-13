#!/usr/bin/env python3
"""增量更新数据（等价于 python main.py update）。

只请求本地缺失的部分；重复运行不会产生重复数据。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.binance_client import MarketDataSource
from src.storage import Storage
from src.updater import Updater
from src.utils import load_config, setup_logging


def main() -> int:
    cfg = load_config()
    setup_logging(cfg)
    storage = Storage(cfg)
    source = MarketDataSource(cfg)
    updater = Updater(cfg, storage, source)
    uni = storage.load_universe()
    if uni.empty:
        uni = updater.update_universe()
    res = updater.update_all(uni["symbol"].tolist() if not uni.empty else None)
    storage.refresh_views()
    print(f"update done: {res['symbols']} symbols, {len(res['failures'])} failures, "
          f"{sum(res['rows'].values())} rows, {res['seconds']}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
