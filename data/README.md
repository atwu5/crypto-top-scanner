# data/ 目录说明

本目录是工具运行积累的本地数据（默认 `git` 提交时：原始 parquet 走 LFS）。

## 目录结构

```text
data/
├── raw/
│   ├── klines/{symbol}/{interval}/YYYY-MM.parquet   # K线（按 symbol/interval/月 分区）
│   ├── funding/{symbol}/{symbol}.parquet            # Funding Rate（含 funding_time、funding_rate）
│   ├── open_interest/{symbol}/{symbol}.parquet      # 持仓量 OI（time, open_interest, open_interest_value）
│   ├── taker/{symbol}/{symbol}.parquet              # 主动买卖比（time, taker_ratio）
│   └── market_cap/snapshots/YYYY-MM.parquet         # 市值排名快照（每次扫描追加一条全市场快照）
├── processed/
│   ├── universe.parquet         # 扫描范围（合约列表，含抓取时间）
│   ├── symbol_map.csv           # Binance symbol ↔ CoinGecko 映射（自动 + 人工 overrides）
│   ├── unresolved_symbols.csv   # 未能自动映射的 symbol
│   ├── case_analysis.csv/.parquet  # 样本（success / false_top）分析结果
│   ├── scan_meta.json           # 最近一次扫描元信息（时间、状态统计、数据截止）
│   └── scans/scan_*.parquet     # 每次扫描的完整结果快照（含全部特征、评分、状态、解释 JSON）
└── scanner.duckdb               # DuckDB 查询层（视图中转，不入库文件本身通常不提交）
```

## 数据口径

- 时间：全部 UTC；
- 数据源：Binance USDT-M Futures —— 直连优先（fapi），不可达时使用 `data.binance.vision` 公共归档（滞后 1~2 天）；
- 去重：所有写入按 `symbol + interval + timestamp` 去重，重复运行不产生重复数据；
- 市值排名：CoinGecko 快照，无法访问时相关字段为 NULL（绝不伪造成历史值）。

## 重新生成 / 更新

```bash
python scripts/init_data.py    # 首次全量
python main.py scan            # 日常增量 + 扫描
python scripts/analyze_cases.py
```
