# Memory-industry research protocol v1

Status: software implementation / synthetic verification, not a profitable strategy.
Research question: does public issuer and industry evidence improve five-session
relative-return forecast loss beyond price-only information, under the same time
availability, model budget and execution contract?

The MU/SPY choice is retrospective and disclosed. A generalizable universe and a
prospectively frozen final evaluation are not supplied by this demonstration.

Decisions: 10:00 America/New_York on a supplied versioned regular-session calendar.
Entry: first minute boundary strictly after a declared 100 ms order delay.
Target: five-session non-reinvested total return minus the selected benchmark return.
Training: only completed and available targets, rolling 60 eligible observations;
15-observation initial minimum. Means/scales fitted only inside that window.
Model: regularized linear regression, penalty 1.0, intercept unpenalized.
Variants: A price; B A+issuer; C B+industry; D C+available supplies relationships.
D is conditional on evidence; no source-backed edges means no network trial.
Portfolio illustration: fixed 100-share maximum, unlevered long/cash, not a short
benchmark or five overlapping portfolios. Initial synthetic cash: USD100,000.
Execution assumptions: 2bps full spread, 1bps impact, 1bps fees; 1% prior-completed
minute volume cap; no current/future-bar-volume sizing. These are demo scenarios,
not empirically calibrated transaction costs.
Comparison: paired forecast losses and seeded moving time-block intervals. Net
account return, costs, turnover, exposure, drawdown and unfilled orders are separate.
No automatic parameter search, holdout reuse or best-scenario selection is performed.

Predetermined scenarios: observation delay +15min, doubled friction, remove industry
facts, remove graph. Current-graph backfill is an intentionally INVALID control.
Degree/sector-matched placebo tests are not implemented on a one-edge synthetic graph.

Real-data gate: verified permissions, original source context, historical identifiers,
consistent reporting durations/units, source timestamps, full session coverage and
corporate actions. Never substitute synthetic values to satisfy this gate.
