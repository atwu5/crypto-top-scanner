#!/usr/bin/env python3
"""样本分析：读取 config/cases.csv，输出 data/processed/case_analysis.csv。

用法:  python scripts/analyze_cases.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.cases import analyze_cases
from src.utils import load_config, setup_logging


def main() -> int:
    cfg = load_config()
    setup_logging(cfg)
    df = analyze_cases(cfg)
    print(f"case_analysis.csv written: {len(df)} cases")
    return 0


if __name__ == "__main__":
    sys.exit(main())
