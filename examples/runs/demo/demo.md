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
- Honest benchmark hash: `488c375daa143582f22a591cf09f2a5d165238d43f00b75335c205b02b84cacd`
- Naive control PBO: `n/a (requires >=4 trials)`
- Honest benchmark PBO: `n/a (requires >=4 trials)`
- Manifest run id: `6386e6227a7c329e9c14adda442e14272962d919378e3389a102fad47ad7f530`

## Naive Control

| Policy | Avg markout bps | Avg lift bps | Quote rate | Deflated Sharpe probability |
| --- | ---: | ---: | ---: | --- |
| leaky | 0.75 | 0.75 | 0.5 | n/a (requires >=3 trials) |
| maker_side_filter | 0 | 0 | 0.5 | n/a (requires >=3 trials) |
| ofi_quote | 0 | 0 | 1 | n/a (requires >=3 trials) |

## Hindsight Clean Benchmark

| Policy | Avg markout bps | Avg lift bps | Quote rate | Deflated Sharpe probability |
| --- | ---: | ---: | ---: | --- |
| maker_side_filter | 1 | -1.5 | 0.5 | n/a (requires >=3 trials) |
| ofi_quote | 2.5 | 0 | 1 | n/a (requires >=3 trials) |

## Artifacts

- `demo_json`: `demo.json`
- `demo_markdown`: `demo.md`
- `manifest`: `manifest.json`
- `naive_control_csv`: `naive-control-leaderboard.csv`
- `hindsight_clean_csv`: `hindsight-clean-leaderboard.csv`
