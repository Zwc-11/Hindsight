# Hindsight Audit Tearsheet — Master Plan

Goal: the strongest possible 90-second recruiter experience for a top-tier quant
firm, built so that every visual element is itself evidence of research rigor.
Theme: dark terminal / trading-desk. Zero new runtime dependencies.

The recruiter path this plan optimizes:

README hero GIF → "View live report" link → tearsheet verdict banner →
fold heatmap + capacity curve → provenance hashes → clone-and-reproduce section.

---

## Phase 0 — Flagship data foundation (the substance)

No interface survives thin data. The committed demo (2 rows/fold, DSR/PBO
"n/a") cannot headline.

- Extend the local Hyperliquid lake: target 3+ symbols (e.g. SOL, ETH, BTC
  perps), 30+ days of top-of-book + trades, historical funding series.
- Replace flat funding rate with the recorded funding series (removes a
  documented limitation and upgrades the fill model disclosure).
- Define >= 8 policy trials (parameter variants of maker_side_filter /
  ofi_quote plus 2 new baselines) so PBO computes and DSR is meaningful.
- >= 8 purged walk-forward folds so the fold matrix has texture.
- Output: one flagship run per symbol committed under
  `docs/benchmarks/<date>-flagship/` (artifact hashes only, no raw data,
  producing command recorded — same policy as the existing SOL proof).

Acceptance: PBO and DSR are real numbers; fold matrix >= 8x8 cells; funding
comes from recorded series.

## Phase 1 — Report data contract v2

One self-sufficient `report.json` per run. Additions to `hindsight.reporting`:

- `comparison`: per-policy {naive_rank, audited_status, block_reason} — feeds
  the verdict banner without scraping two leaderboards.
- `markout_ladder`: markout in bps at multiple horizons (e.g. 1s/5s/30s/2m/10m)
  with bootstrap CI bands per policy per fold.
- `cost_attribution`: gross → fees → slippage → funding → net, per policy.
- `probes`: each leakage tripwire with pass/fail, the statistic it computed,
  and the offending evidence (e.g. feature name + timestamp) when it fires.
- `equity_curves`: already exists via `curves.py`; add per-policy drawdown
  series and position/mark overlays.
- `sensitivity`: parameter-sweep grid results (param values × metric) for the
  overfit-surface heatmap.
- `capacity`: markout lift vs participation-cap sweep (3–5 cap levels).
- `regimes`: metrics sliced by realized-vol tercile and time-of-day bucket.
- `fills`: flag counts (`top_of_book_missing`, latency, maker/taker mix).
- Schema versioned (`report_schema: 2`), validated in tests.

Acceptance: `hindsight run`/`benchmark`/`demo` all emit v2 JSON; unit tests
cover every block; goldens updated.

## Phase 2 — Tearsheet renderer (`hindsight/reporting/html_report.py`)

Single self-contained HTML file. Inline CSS + hand-rolled SVG charts + ~300
lines of vanilla JS for crosshair tooltips, panel collapse, and click-to-copy.
No CDN, no framework, works offline, byte-deterministic.

Panels, top to bottom:

1. Verdict banner — "Naive split: leaky ranks #1" vs "Hindsight: BLOCKED",
   red/green, block reason inline.
2. Dual leaderboards with rank-shift arrows (naive → audited).
3. Equity curve + drawdown underlay + position/mark overlay, per policy tabs.
4. Markout ladder — multi-horizon curves with CI bands.
5. Fold × policy heatmap of markout lift, CI whisker on hover.
6. Cost attribution waterfall.
7. Capacity curve — lift vs participation cap (quant catnip: shows the edge's
   size limit honestly).
8. Sensitivity heatmap — parameter grid vs metric; a smooth plateau vs a
   single spike is the overfit story told visually.
9. Regime panel — vol-tercile × time-of-day small multiples.
10. Leakage tripwire panel — each probe, pass/fail, what it caught.
11. DSR / PBO gauges + bootstrap distribution histograms.
12. Fill-model disclosure — flag counts, latency, fee tier, maker/taker mix.
13. Provenance footer — run_id, git_sha, seed, config_hash,
    data_content_hash, report self-hash; monospace chips, click-to-copy.
14. "Reproduce this report" — the exact command, pinned versions.

Print CSS → clean 2-page PDF export for applications.

Acceptance: golden-file test asserts byte-identical HTML across two fresh
runs; renders correctly with JS disabled (SVG is static; JS only adds
affordances); Lighthouse-clean, < 2 MB single file.

## Phase 3 — Adversarial falsification suite (the differentiator)

A `hindsight falsify` command that deliberately injects known leaks and proves
each tripwire catches them:

- label lookahead (shift labels −1 bar)
- feature-from-the-future (join a t+k feature)
- survivorship filter on symbols
- fold contamination (remove purging)
- fee/slippage zeroing (free-lunch execution)

Output: a falsification matrix panel in the tearsheet — injected defect ×
probe, every cell must be CAUGHT. Run in CI. This is the single most
distinctive artifact in the plan: nobody else ships a backtester that ships
proof it catches itself lying.

Acceptance: 5 injected defects, 100% caught, CI-enforced.

## Phase 4 — Determinism, CI, publishing

- CI job builds the demo tearsheet twice, diffs hashes (extends the existing
  run_id/data_content_hash double-run check to the report itself).
- GitHub Pages: deploy `demo` tearsheet + flagship tearsheets on main.
- `hindsight report <run_dir>` CLI for any historical run in `reports/`.

## Phase 5 — Research-log index

Generated static `index.html` over `reports/` and `docs/benchmarks/`:
sortable table (date, symbol, policies, lift, DSR, PBO, verdict), inline SVG
sparklines, links into each tearsheet. Reads as a living lab notebook — 40
runs already exist locally, which is itself a signal of sustained work.

## Phase 6 — Packaging for the 90 seconds

- Regenerate the README GIF to end on the red BLOCKED verdict banner.
- README hero: screenshot linking to the live flagship tearsheet, one line:
  "Every number below links to a hash-stamped, CI-reproduced artifact."
- Resume/application line cites the falsification suite + live report URL.

---

## What is deliberately NOT built, and why

- Streamlit/Dash/React app: recruiters don't run servers; heavy deps
  contradict the project's zero-dep discipline; not committable, not
  hashable, dies in CI. A static hash-stamped report is the only interface
  that is itself evidence of the project's thesis (reproducibility).
- Live trading UI / websocket dashboards: Hindsight is an evaluation
  harness; pretending otherwise reads as scope confusion.
- Fabricated "cool data": every pixel traces to a committed artifact hash.

## Effort and order

0 (data) and 1 (contract) first — 2 sessions. 2 (renderer) — 2 sessions.
3 (falsify) — 1–2 sessions. 4–6 — 1 session. Phases 2 and 3 can start against
existing synthetic demo artifacts in parallel with Phase 0 data collection.

## Risks

- Local lake too thin → Phase 0 blocks flagship claims; tearsheet still ships
  on synthetic demo with limitations stated (honesty is on-brand).
- Historical funding series availability → fall back to flat rate with the
  limitation kept in the disclosure panel.
- Hand-rolled SVG scope creep → cap JS at tooltips/collapse/copy; no zoom.
