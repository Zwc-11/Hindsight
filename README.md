# Hindsight

Leakage-audited, point-in-time evaluation for market microstructure strategies.

Hindsight is a Python backtesting and evaluation harness extracted from
MarketImmune. It focuses on correctness of research evaluation: deterministic
event replay, point-in-time feature access, purged walk-forward folds, leakage
tripwires, execution-cost modeling, and reproducible run manifests.

It is not a live-trading system and it does not claim queue-position or
latency-distribution fidelity. The execution model is deliberately explicit:
market orders fill as takers at the touch when top-of-book is available, limit
orders fill as makers when prints cross the limit, participation is capped, and
funding uses the configured flat rate.

## Quick Start

```powershell
python -m pip install -e ".[dev]"
python -m pytest
python -m hindsight.cli run
python -m hindsight.cli compare
python -m hindsight.cli benchmark
```

On PowerShell, the same starter demo is:

```powershell
.\make.ps1 install
.\make.ps1 demo
```

After installation, the console entry point is also available:

```powershell
hindsight run
hindsight compare
hindsight benchmark
```

The default commands use tiny synthetic sample lakes under
`examples/sample_data/` and write generated reports under `reports/`.

## What Is Included

- `hindsight.core`: canonical event schemas, replay clock, stable hashing.
- `hindsight.pit`: point-in-time data access with lookahead guards.
- `hindsight.evaluation`: purged walk-forward validation, leakage probes,
  markout metrics, deflated Sharpe, and probability of backtest overfit.
- `hindsight.execution`: costed replay simulator with fees, slippage, latency,
  funding, and participation caps.
- `hindsight.strategy.baselines`: simple clean and intentionally leaky baselines.
- `hindsight.reporting`: JSON, Markdown, leaderboard, manifest, and curve output.
- `hindsight.data`: standalone parquet readers for the bundled samples and
  Hyperliquid markout lake layout.

## Current Status

This is the standalone starter cut. It has no `marketimmune` imports and the
ported Hindsight test suite runs offline:

```powershell
python -m pytest tests/hindsight -q
```

The bundled sample data is synthetic and exists only to make smoke tests and
demo commands reproducible. Use your own Hyperliquid lake for real benchmark
claims.
