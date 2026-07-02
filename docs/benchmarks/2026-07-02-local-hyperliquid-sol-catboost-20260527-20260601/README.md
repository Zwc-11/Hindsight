# Local Hyperliquid SOL CatBoost Validation

Data label: [local real Hyperliquid lake]
Model label: [real-model]

This artifact was produced on the local machine from MarketImmune's promoted SOL CatBoost markout model and local Hyperliquid Gold training parquet. Raw market data and model files are not committed.

## Command

```powershell
python docs/benchmarks/2026-07-02-local-hyperliquid-sol-catboost-20260527-20260601/generate.py
```

## Holdout 20260601

- Rows: `210530`
- PR-AUC: `0.5562060309668825`
- Brier: `0.23320601294418586`
- ECE: `0.0270673987519088`
- Markout lift bps: `0.8603969586966207`
- Quote rate: `0.24593169619531657`

## Panel Replay 20260527..20260601

- Rows: `934680`
- PR-AUC: `0.6064384870338012`
- Brier: `0.21298435665375168`
- ECE: `0.013041290475929812`
- Markout lift bps: `0.9542075921575426`
- Quote rate: `0.29838447383061584`

## Reconciliation

- Verdict: `pass`
- Max absolute metric delta: `2.220446049250313e-16`
- Row delta: `0`

## Artifacts

- [validation.json](validation.json)
- [partition-metrics.csv](partition-metrics.csv)

## Caveats

- The panel replay scores the promoted final model over all requested dates; it is not an out-of-fold training metric.
- This benchmark is reproducible only on machines with the same local MarketImmune lake and promoted model artifacts.
