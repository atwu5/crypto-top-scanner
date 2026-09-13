#!/usr/bin/env python3
"""为历史扫描结果补充「未来实际走势」字段（spec §29）。

用法:  python scripts/update_outcomes.py
数据窗口不足 4h 的字段保持 NULL。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.outcomes import compute_outcomes
from src.storage import Storage
from src.utils import load_config, setup_logging


def main() -> int:
    cfg = load_config()
    setup_logging(cfg)
    storage = Storage(cfg)
    n = compute_outcomes(storage, cfg)
    print(f"scan_outcomes rows: {n}（窗口不足时字段为 NULL）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
