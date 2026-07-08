# Local Hyperliquid Flagship Phase 0

Data label: [local real Hyperliquid lake]

This artifact was produced from local Hyperliquid parquet files. Raw market data is not committed; this directory stores hashes, row counts, and benchmark outputs only.

## Coverage

- Symbols: `BTC-PERP, ETH-PERP, SOL-PERP`
- Dates: `30`
- Markout partitions: `90`
- Trade partitions: `90`
- Top-of-book partitions: `90`
- Funding partitions: `90`

## Acceptance

- Passed: `True`
- Policies: `8`
- Folds: `8`
- Fold matrix cells: `64`
- Numeric PBO: `True`
- Numeric DSR policies: `7`

Failures:

- none

## Artifacts

- [coverage.json](coverage.json)
- [flagship.json](flagship.json)
- [flagship-leaderboard.csv](flagship-leaderboard.csv)
- [leakage.json](leakage.json)
- [falsification.json](falsification.json)
- [report.json](report.json)
- [tearsheet.html](tearsheet.html)
