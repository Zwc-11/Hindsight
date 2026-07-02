# Benchmarks

This directory is reserved for command-produced benchmark artifacts.

Rules:

- Do not hand-type benchmark numbers.
- Every artifact must name the command that produced it.
- Every public metric must link to the artifact that contains it.
- Synthetic demo artifacts belong under `examples/runs/`; real or licensed
  benchmark artifacts belong here.

Artifacts:

- [2026-07-02 local Hyperliquid SOL benchmark](2026-07-02-local-hyperliquid-sol-20260527)
  uses a local real Hyperliquid lake file and commits provenance plus benchmark
  output only. Raw market data is not committed.
- [2026-07-02 local Hyperliquid SOL CatBoost validation](2026-07-02-local-hyperliquid-sol-catboost-20260527-20260601)
  replays the promoted MarketImmune CatBoost markout model over full local SOL
  holdout and panel partitions, with reconciliation against the MarketImmune
  holdout report.
