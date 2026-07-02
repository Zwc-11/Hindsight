# Data

Hindsight ships with tiny synthetic sample data so the project can be installed,
tested, and demoed offline.

## Bundled Samples

`examples/sample_data/binance_usdm/` contains a synthetic BTCUSDT parquet slice
used by the generic smoke commands.

`examples/sample_data/hyperliquid/` contains synthetic SOL-PERP markout rows used
by `hindsight demo` and `hindsight benchmark`.

The sample manifest is `examples/sample_data/data_manifest.json`. These samples
are not real exchange data and must not be used for benchmark claims.

## Real Data

Real acquisition and backfill workflows are intentionally outside this starter
repo. The extracted engine expects already-built parquet artifacts and reads
them through the adapter layer.

For public claims, commit the generated report artifact and document:

- data origin
- symbol and date range
- row count
- command used to produce the artifact
- content hash or run manifest
