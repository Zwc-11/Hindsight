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
