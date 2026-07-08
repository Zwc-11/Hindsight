# Benchmarks

This directory is reserved for command-produced benchmark artifacts.

Rules:

- Do not hand-type benchmark numbers.
- Every artifact must name the command that produced it.
- Every public metric must link to the artifact that contains it.
- Synthetic demo artifacts belong under `examples/runs/`; real or licensed
  benchmark artifacts belong here.

Artifacts:

- [2026-07-07 local Hyperliquid flagship Phase 0](2026-07-07-flagship)
  covers BTC/ETH/SOL over 30 local lake dates with recorded trades, top-of-book
  partitions, asset-context funding series, eight clean policy trials, and eight
  purged folds. Raw market data is not committed.
- [2026-07-02 local Hyperliquid SOL benchmark](2026-07-02-local-hyperliquid-sol-20260527)
  uses a local real Hyperliquid lake file and commits provenance plus benchmark
  output only. Raw market data is not committed.
- [2026-07-02 local Hyperliquid SOL CatBoost validation](2026-07-02-local-hyperliquid-sol-catboost-20260527-20260601)
  replays the promoted MarketImmune CatBoost markout model over full local SOL
  holdout and panel partitions, with reconciliation against the MarketImmune
  holdout report.

## Phase 0 flagship workflow

Start with a coverage audit. It writes source paths, row counts, and hashes only:

```powershell
python -m hindsight.cli flagship `
  --lake-root C:\MarketImmune\data\hyperliquid `
  --output-dir docs\benchmarks\phase0-flagship `
  --symbols SOL-PERP,ETH-PERP,BTC-PERP `
  --dates 20260527..20260625 `
  --coverage-only
```

Once coverage is ready, run the benchmark:

```powershell
python -m hindsight.cli flagship `
  --lake-root C:\MarketImmune\data\hyperliquid `
  --output-dir docs\benchmarks\2026-07-07-flagship `
  --symbols SOL-PERP,ETH-PERP,BTC-PERP `
  --dates 20260527..20260625 `
  --rows-per-partition 5000 `
  --n-folds 8 `
  --train-window 3000 `
  --test-window 500 `
  --purge-seconds 10 `
  --embargo-seconds 10
```

Acceptance requires at least three symbols, thirty dates, recorded trades,
top-of-book partitions, recorded funding or asset-context series, eight policy
trials, eight folds, numeric PBO, and numeric DSR for the non-baseline policies.

## Published archive

The GitHub Pages artifact is built from committed files only:

```powershell
python -m hindsight.cli pages --output-dir _site
```

That command copies this benchmark directory to `_site/benchmarks/`, regenerates
the synthetic demo tearsheet under `_site/demo/`, copies publishable schema-v2
report artifacts from `reports/`, and writes a sortable `_site/index.html`
research ledger over every available `tearsheet.html`. Raw market data is not
copied.
