# Limitations

Hindsight is an evaluation harness, not a live-trading stack.

Current explicit cuts:

- No live trading hooks.
- No queue-position simulation.
- No latency-distribution modeling.
- No order-book-depth walking in the execution model.
- No claim of exchange-wide or multi-venue coverage from the bundled samples.
- Funding uses a configured flat rate, not a historical funding curve.
- Bundled sample data is synthetic and only proves the offline workflow.

Execution assumptions are intentionally simple and disclosed: market orders fill
as takers at the touch when top-of-book is available, limit orders fill as makers
when a print crosses the limit, fills are participation-capped, and configured
fees, slippage, latency, and funding are applied deterministically.

The default demo/run fee values match Hyperliquid's published base perps tier as
of July 2, 2026: 1.5 bps maker and 4.5 bps taker
([official fees doc](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/fees)).
Fees remain explicit config inputs because account volume tiers, staking
discounts, aligned quote assets, and maker rebates can change the effective fee
paid by a real strategy.
