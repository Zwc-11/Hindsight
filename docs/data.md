# Data

Hindsight ships with tiny synthetic sample data so the project can be installed,
tested, and demoed offline.

## Bundled Samples

`examples/sample_data/hyperliquid/` contains a synthetic SOL-PERP fill stream
used by `hindsight run`, plus synthetic SOL-PERP markout rows used by
`hindsight demo` and `hindsight benchmark`.

The sample manifest is `examples/sample_data/data_manifest.json`. It records the
data label, source, UTC window, per-file row counts, and content hash. The
committed demo manifest in `examples/runs/demo/manifest.json` uses the same
`hyperliquid_markout` sample `content_hash`, which is the anti-fabrication check
for the offline demo.

These samples are not real exchange data and must not be used for benchmark
claims.

## Licensing Check

The preferred public demo data would be a small real Hyperliquid slice, but this
repo does not currently commit one. Hyperliquid's public historical-data docs
describe archive access paths and requester-pays S3 usage, while the public
terms page governs interface access; neither was treated here as an explicit
redistribution grant for bundling real exchange data in this repository.

References checked:

- https://hyperliquid.gitbook.io/hyperliquid-docs/historical-data
- https://app.hyperliquid.xyz/terms

Until redistribution is explicitly cleared, `examples/sample_data/` remains
synthetic.

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

The first local-real benchmark proof is
[2026-07-02-local-hyperliquid-sol-20260527](benchmarks/2026-07-02-local-hyperliquid-sol-20260527).
It uses a gitignored local MarketImmune lake file and commits only the benchmark
CSV plus provenance hashes.
