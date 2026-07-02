# Related Work

Hindsight is closest to backtesting and evaluation tooling, but its center of
gravity is leakage-audited ML evaluation rather than high-fidelity market
simulation.

Use Hindsight when the core question is:

- Did this strategy or model look into the future?
- Were features accessed point-in-time?
- Did cross-validation respect label intervals?
- Are results reproducible from content hashes and manifests?

Use a dedicated execution simulator when the core question is queue position,
latency distribution, matching-engine behavior, or detailed order-book replay.
