#!/usr/bin/env python3
"""从 data_pack/ 还原为 data/raw 逐文件布局。

用法:  python scripts/restore_data_pack.py
（克隆仓库后运行一次，即可得到与直接下载相同的本地数据布局）
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.utils import get_logger, setup_logging

LOG = get_logger("restore")


def _write_partition(df: pd.DataFrame, out_dir: Path, symbol: str) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    months = df["open_time"].dt.strftime("%Y-%m")
    for ym, part in df.groupby(months):
        part = part.drop_duplicates("open_time").sort_values("open_time")
        part.to_parquet(out_dir / f"{ym}.parquet", compression="zstd", index=False)


def main() -> int:
    setup_logging()
    root = Path(__file__).resolve().parent.parent
    pack = root / "data_pack"
    if not pack.exists():
        print("data_pack/ not found")
        return 1

    # ---- klines ----
    total = 0
    for f in sorted((pack / "klines").glob("part*.parquet")):
        df = pd.read_parquet(f)
        for (sym, itv), part in df.groupby(["symbol", "interval"]):
            out_dir = root / "data/raw/klines" / sym / itv
            _write_partition(part, out_dir, sym)
            total += len(part)
        LOG.info("restored %s", f.name)

    # ---- funding / open_interest / taker ----
    for sub, fname, tcol in (("funding", "funding", "funding_time"),
                             ("open_interest", "open_interest", "time"),
                             ("taker", "taker", "time")):
        pf = pack / sub / f"{fname}.parquet"
        if not pf.exists():
            continue
        df = pd.read_parquet(pf)
        for sym, part in df.groupby("symbol"):
            out_dir = root / "data/raw" / sub / sym
            out_dir.mkdir(parents=True, exist_ok=True)
            part = part.drop_duplicates(subset=[tcol]).sort_values(tcol)
            part.to_parquet(out_dir / f"{sym}.parquet", compression="zstd", index=False)
        LOG.info("restored %s (%s rows)", sub, len(df))

    # ---- processed ----
    proc = root / "data/processed"
    scans = pack / "processed" / "scans.parquet"
    if scans.exists():
        df = pd.read_parquet(scans)
        (proc / "scans").mkdir(parents=True, exist_ok=True)
        if "scan_time" in df.columns:
            for t, part in df.groupby("scan_time"):
                ts = pd.Timestamp(t).strftime("%Y%m%d_%H%M%S")
                part.to_parquet(proc / "scans" / f"scan_{ts}.parquet", index=False)
        LOG.info("restored scans")
    for name in ("case_analysis.parquet", "universe.parquet"):
        src = pack / "processed" / name
        if src.exists():
            proc.mkdir(parents=True, exist_ok=True)
            pd.read_parquet(src).to_parquet(proc / name, index=False)
    for name in ("symbol_map.csv", "unresolved_symbols.csv"):
        src = pack / "processed" / name
        if src.exists():
            (proc).mkdir(parents=True, exist_ok=True)
            (proc / name).write_bytes(src.read_bytes())

    print("restore done ✔  现在可以: python main.py scan  /  streamlit run app.py")
    print(f"（klines rows restored: {total}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
