# Hindsight Demo

The backtester that catches you lying to yourself.

## Sample Data

- Label: synthetic
- Symbol: SOL-PERP
- Date: 20260101
- Rows: 8
- Horizon: 10s

## Result

- Naive control winner: `leaky`
- Naive control warning: deliberately unsafe control - do not use
- Hindsight audit: `blocked` `leaky`
- Audit error: `policy leaky failed leakage probe`
- Honest benchmark hash: `8878b1f5b0a5fadff26e1f8e0d7a0a8354cf73ad1d0f695bbf0dabde7e6feda2`
- Manifest run id: `1c8e302d5225a94aaf1d2ae99a836e3cf5f488bedc915b9f57d94cd4cfdcbcb7`

## Naive Control

| Policy | Avg markout bps | Avg lift bps | Quote rate |
| --- | ---: | ---: | ---: |
| leaky | 0.75 | 0.75 | 0.5 |
| maker_side_filter | 0 | 0 | 0.5 |
| ofi_quote | 0 | 0 | 1 |

## Hindsight Clean Benchmark

| Policy | Avg markout bps | Avg lift bps | Quote rate |
| --- | ---: | ---: | ---: |
| maker_side_filter | 1 | -1.5 | 0.5 |
| ofi_quote | 2.5 | 0 | 1 |

## Artifacts

- `demo_json`: `demo.json`
- `demo_markdown`: `demo.md`
- `manifest`: `manifest.json`
- `naive_control_csv`: `naive-control-leaderboard.csv`
- `hindsight_clean_csv`: `hindsight-clean-leaderboard.csv`
