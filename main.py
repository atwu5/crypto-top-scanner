#!/usr/bin/env python3
"""Crypto Top Reversal Scanner — CLI entry.

用法:
    python main.py scan                # 全市场扫描（自动增量更新数据）
    python main.py scan --no-update    # 只用本地数据扫描
    python main.py scan --top 80       # 输出前 80 行
    python main.py scan --symbols LSKUSDT,UAIUSDT
    python main.py update              # 只做增量数据更新
"""
from __future__ import annotations

import argparse
import sys

from src.utils import get_logger, load_config, setup_logging


def cmd_scan(args) -> int:
    from src.scanner import Scanner

    cfg = load_config(args.config)
    log = setup_logging(cfg)
    symbols = [s.strip().upper() for s in args.symbols.split(",")] if args.symbols else None
    sc = Scanner(cfg)
    res = sc.run(update=not args.no_update, symbols=symbols, top=args.top, workers=args.workers)
    df = res.get("df")
    if df is None or df.empty:
        print("No results. 请先运行:  python scripts/init_data.py")
        return 1
    ready = df[df["status"].isin(["SHORT_READY", "SHORT_TREND", "REVERSAL_CONFIRMING"])]
    if len(ready):
        print(f"\n候选信号 (SHORT_READY / SHORT_TREND / REVERSAL_CONFIRMING): {len(ready)} 个")
        for r in ready.head(15).itertuples():
            print(f"  - {r.symbol:14s} status={r.status:20s} reversal={getattr(r,'reversal_score',None)} overheat={getattr(r,'overheat_score',None)}")
    return 0


def cmd_update(args) -> int:
    from src.storage import Storage
    from src.binance_client import MarketDataSource
    from src.updater import Updater

    cfg = load_config(args.config)
    setup_logging(cfg)
    storage = Storage(cfg)
    source = MarketDataSource(cfg)
    updater = Updater(cfg, storage, source)
    uni = storage.load_universe()
    if uni.empty:
        uni = updater.update_universe()
    res = updater.update_all(uni["symbol"].tolist() if not uni.empty else None)
    print(f"update done: {res['symbols']} symbols, {len(res['failures'])} failures, "
          f"{sum(res['rows'].values())} rows, {res['seconds']}s")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Crypto Top Reversal Scanner")
    p.add_argument("--config", default=None, help="config/scanner.yaml 路径")
    sub = p.add_subparsers(dest="cmd")

    ps = sub.add_parser("scan", help="全市场扫描")
    ps.add_argument("--no-update", action="store_true", help="跳过数据增量更新")
    ps.add_argument("--top", type=int, default=60, help="输出行数")
    ps.add_argument("--symbols", default=None, help="只扫描指定 symbol（逗号分隔）")
    ps.add_argument("--workers", type=int, default=8, help="并发线程数")
    ps.set_defaults(func=cmd_scan)

    pu = sub.add_parser("update", help="仅增量更新数据")
    pu.set_defaults(func=cmd_update)

    args = p.parse_args()
    if not getattr(args, "cmd", None):
        args = p.parse_args(["scan"])
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
