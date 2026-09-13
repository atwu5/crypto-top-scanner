#!/usr/bin/env python3
"""把逐文件 parquet 打包为合并数据包（data_pack/）。

用途：在「大文件直传受限」的网络环境下，用少量较大文件替代 8700+ 小文件，
配合 `scripts/push_tree_via_api.py` 通过 GitHub REST API 推送。

用法:
    python scripts/pack_data.py            # 生成 data_pack/
    （之后 git add data_pack && git push 即可，或走 API 推送脚本）

还原:  python scripts/restore_data_pack.py
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.utils import get_logger, setup_logging

LOG = get_logger("pack")

CHUNK_MB = 10.0
COMPRESSION = "zstd"

KLINE_COLS = ["symbol", "interval", "open_time", "open", "high", "low", "close", "volume",
              "close_time", "quote_volume", "count", "taker_buy_volume", "taker_buy_quote_volume"]


def _mb(path: Path) -> float:
    return path.stat().st_size / 1024 / 1024 if path.exists() else 0.0


def pack_klines(root: Path, out: Path) -> list:
    files = []
    kdir = root / "data/raw/klines"
    if not kdir.exists():
        return files
    syms = sorted([p for p in kdir.iterdir() if p.is_dir()])
    # 每个 symbol 的全部 interval 一起打包（均衡分块）
    parts, cur, cur_bytes = [], [], 0
    def flush():
        nonlocal cur, cur_bytes
        if not cur:
            return
        df = pd.concat(cur, ignore_index=True)
        idx = len(parts) + 1
        f = out / "klines" / f"part{idx:02d}.parquet"
        f.parent.mkdir(parents=True, exist_ok=True)
        df.to_parquet(f, compression=COMPRESSION, index=False)
        parts.append(f)
        LOG.info("klines %s: %s rows, %.1f MB", f.name, len(df), _mb(f))
        cur, cur_bytes = [], 0
    for sp in syms:
        frames = []
        for itv_dir in sorted(sp.iterdir()):
            if not itv_dir.is_dir():
                continue
            interval = itv_dir.name
            for pf in sorted(itv_dir.glob("*.parquet")):
                df = pd.read_parquet(pf)
                df["symbol"] = sp.name
                df["interval"] = interval
                frames.append(df)
        if not frames:
            continue
        df = pd.concat(frames, ignore_index=True)
        est_bytes = df.memory_usage(deep=True).sum()
        cur.append(df)
        cur_bytes += est_bytes
        if cur_bytes > CHUNK_MB * 1024 * 1024 * 1.4:  # 估算系数（parquet 压缩）
            flush()
    flush()
    # 记录实际大小以迭代分块（若单块过大按大小重分）
    return parts


def pack_table(root: Path, sub: str, fname: str, out: Path, name: str) -> list:
    files = []
    d = root / "data/raw" / sub
    if not d.exists():
        return files
    frames = []
    for sym_dir in sorted([p for p in d.iterdir() if p.is_dir()]):
        pf = sym_dir / f"{sym_dir.name}.parquet"
        if pf.exists():
            frames.append(pd.read_parquet(pf))
    if not frames:
        return files
    df = pd.concat(frames, ignore_index=True)
    f = out / sub / f"{fname}.parquet"
    f.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(f, compression=COMPRESSION, index=False)
    LOG.info("%s: %s rows, %.1f MB", name, len(df), _mb(f))
    files.append(f)
    return files


def pack_processed(root: Path, out: Path) -> list:
    files = []
    proc = root / "data/processed"
    # scans -> 单文件
    scans = sorted((proc / "scans").glob("scan_*.parquet")) if (proc / "scans").exists() else []
    if scans:
        frames = []
        for f in scans:
            df = pd.read_parquet(f)
            if "scan_time" not in df.columns:
                df.insert(0, "scan_time", f.stem.replace("scan_", ""))
            frames.append(df)
        allscans = pd.concat(frames, ignore_index=True)
        f = out / "processed" / "scans.parquet"
        f.parent.mkdir(parents=True, exist_ok=True)
        allscans.to_parquet(f, compression=COMPRESSION, index=False)
        LOG.info("scans: %s rows, %.1f MB", len(allscans), _mb(f))
        files.append(f)
    for name in ("case_analysis.parquet", "universe.parquet"):
        src = proc / name
        if src.exists():
            f = out / "processed" / name
            f.parent.mkdir(parents=True, exist_ok=True)
            pd.read_parquet(src).to_parquet(f, compression=COMPRESSION, index=False)
            files.append(f)
    for name in ("symbol_map.csv", "unresolved_symbols.csv"):
        src = proc / name
        if src.exists():
            f = out / "processed" / name
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_bytes(src.read_bytes())
            files.append(f)
    return files


def main() -> int:
    setup_logging()
    root = Path(__file__).resolve().parent.parent
    out = root / "data_pack"
    if out.exists():
        import shutil
        shutil.rmtree(out)
    out.mkdir(parents=True)

    all_files = []
    all_files += pack_klines(root, out)
    all_files += pack_table(root, "funding", "funding", out, "funding")
    all_files += pack_table(root, "open_interest", "open_interest", out, "open_interest")
    all_files += pack_table(root, "taker", "taker", out, "taker")
    all_files += pack_processed(root, out)

    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "layout_version": 1,
        "note": "合并数据包；运行 python scripts/restore_data_pack.py 还原为 data/raw 逐文件布局",
        "files": [{"path": str(f.relative_to(out)), "bytes": f.stat().st_size} for f in all_files],
    }
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    total = sum(f.stat().st_size for f in all_files)
    LOG.info("data_pack done: %s files, %.1f MB total", len(all_files), total / 1024 / 1024)
    print(f"data_pack: {len(all_files)} files, {total/1024/1024:.1f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
