# Local Hyperliquid SOL Benchmark - 2026-05-27

Data label: [local real Hyperliquid lake]

This artifact was produced from the local MarketImmune Hyperliquid lake on this
machine. The source parquet is not committed or redistributed by Hindsight.

## Source

- Source file: `C:\MarketImmune\data\hyperliquid\gold\hyperliquid\markout\SOL\SOL-markout-20260527.parquet`
- Source file SHA-256: `ED26C4BB947D79CD813D66024763D2BDCFCCE2D0A3B55E43D811DC8E77F94013`
- Source rows available: `191740`
- Rows evaluated: first `5000`
- Symbol: `SOL-PERP`
- Date: `20260527`
- Markout horizon: `10s`

## Command

Run from the Hindsight repo root:

```powershell
python -m hindsight.cli benchmark --lake-root C:\MarketImmune\data\hyperliquid --output-dir docs\benchmarks\2026-07-02-local-hyperliquid-sol-20260527 --symbol SOL-PERP --date 20260527 --limit 5000 --horizon 10s --label-horizon-seconds 10 --n-folds 5 --train-window 3000 --test-window 400 --purge-seconds 10 --embargo-seconds 10
```

## Output

- Leaderboard: [hindsight-leaderboard.csv](hindsight-leaderboard.csv)
- Leaderboard SHA-256: `AAED6B67F6FC584728B96D6330F8801DC8FF8F1F4FCCC875873DA8362D44006A`
- Benchmark hash: `99943c2daf9e564b066bb4e1750fab2fec1ce8f27918658a0ce7ac63225dfce6`
- Hindsight engine-code commit used: `83f22d1b69286a815ab5ba8dc5345f26697347f9`

## Caveats

- This benchmark is reproducible only on machines with the same local lake file.
- The artifact commits benchmark output and provenance, not raw market data.
- The policies are simple engine smoke policies: `ofi_quote` quotes every row, and
  `maker_side_filter` quotes only positive maker-side rows.
- These numbers are not a trading claim. They prove that the standalone Hindsight
  benchmark path can run over the local real Hyperliquid markout lake with
  purged and embargoed walk-forward folds.
