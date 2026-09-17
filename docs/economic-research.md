# Economic-information research in Hindsight

The existing Python/Hyperliquid evaluation path remains intact. `crates/economic`
adds a native Rust research engine with a small C++20 ridge-regression kernel
connected through CXX. The Python CLI only launches that binary; it does not
implement a second model, account, or execution simulator.

## Run on this checkout

```sh
cd ~/Desktop/Hindsight
sh scripts/cargo-local.sh build --locked
.venv/bin/python -m hindsight.cli economic demo reports/economic-new-run
open reports/economic-new-run/index.html
```

The default demo is entirely synthetic: prices, facts, corrections and the supplier
relationship. Only the exchange session schedule is an exported calendar. It is
not an investment result or a real memory-market dataset. Every output directory
is immutable: choose a new name for another run.

On another machine, install Rust using its official rustup installer and a C++20
compiler, then use `cargo build --locked`. The pinned toolchain is in
`rust-toolchain.toml`; `Cargo.lock` pins the dependency resolution. On the connected
Mac, Rust was installed under `.tools` without changing the shell startup files.

## Research commands

```sh
# Save a complete editable synthetic input contract.
target/debug/hindsight-economic demo-input reports/economic-input.json
# Run an explicitly supplied, normalized input.
target/debug/hindsight-economic run reports/economic-input.json reports/economic-custom
# Inspect missing minutes, release counts and timestamp precision.
target/debug/hindsight-economic coverage reports/economic-input.json
# Query only eligible sources, fact revisions and relationships.
target/debug/hindsight-economic snapshot reports/economic-input.json 2026-02-02T15:00:00Z reconstructed
# Rebuild the entire pipeline under future-data and metadata mutations.
target/debug/hindsight-economic falsify reports/economic-audit.json
# Fixed ablations and delay/cost scenarios; no best-scenario selection.
target/debug/hindsight-economic scenarios reports/economic-input.json reports/economic-scenarios
```

## What is implemented

Rust owns validated input contracts, UTC timing, exact fact identities, source and
fact readiness, revision selection, graph visibility, completed-bar feature access,
walk-forward label maturity, account state, order transitions and reporting. A/B/C/D
use the same dates, model family, ridge penalty and fixed portfolio mapping. D is
omitted when the supplied graph is empty. The graph feature currently uses only
source-backed `supplies` edges; other relationship types are preserved, not
silently collapsed into supply relationships.

C++ solves a small regularized regression system after Rust fits means and scales
on eligible training rows only. The intercept is unpenalized. Inputs, dimensions,
finite values and solve failures are checked. Near-threshold signals use a stated
1e-10 dead band. Floating-point results are tested within tolerances, not promised
bit-identical across all processors.

Money uses checked integer USD micro-units, with whole-share positions. There is
one account per comparison strategy, not five overlapping full-capital portfolios.
Every decision, no-order, rejection, submission, arrival, fill, report and cancelled
remainder is retained. Duplicate order IDs are rejected. Dividends enter receivables
before their payment date and become cash only when payable. Splits with fractional
shares require cash-in-lieu data and fail rather than inventing proceeds. Actions
must be consolidated per symbol at a supplied session open; arbitrary intraday
actions are deliberately unsupported.

## Evidence modes and identities

`observed` requires genuine receipt and extraction timestamps as well as publication.
`reconstructed` uses a separately recorded historical availability assumption.
Date-only publication is conservatively deferred until the end of the publisher's
local date plus an explicit allowance. Timezone and daylight-saving boundaries are
handled with pinned libraries, not a fixed 24-hour addition.

A fact identity includes entity, metric, source series, unit, scale, reporting start,
reporting end and context dimensions. Conflicting source revisions fail closed.
Later corrections, retractions and relationships cannot rewrite earlier snapshots.
The initial synthetic identifiers are not a historical issuer/ticker master. Real
inputs must supply historically correct entity and symbol mappings; automated
universe construction and listing-history ingestion are not implemented.

## Execution assumptions

Minute bars have a start, end and availability timestamp. Features never use a bar
at its opening timestamp. Orders use the first minute boundary strictly after
arrival; a missing reference cancels the order instead of skipping to a favorable
later bar. The opening print is a research reference, not proof of an executable
quote or sufficient market depth. A modeled half-spread and impact adjust the
reference, with fees charged once. Sizing capacity is based on already-completed
volume, not the future fill minute.

Observation delays affect the strategy view, not the simulated market's historical
clock. The current engine requires sub-minute order/report latency and makes one
10:00 New York decision per supplied regular session. There is no queue model,
intraminute stop/limit path, borrowing, leverage, options, broker integration or
live trading. Remaining positions are marked, not forcibly liquidated. The
buy-and-hold number is explicitly an uncosted return reference, not a matched
execution account. A price-feature window spanning a split is withheld rather
than treating the split as an economic return.

## Data adapters and permissions

`capture-public URL ARCHIVE_DIR` supports allowlisted public SEC, Nanya and Micron
HTTPS sources, bounded response sizes and only same-host HTTPS redirects. Raw
bytes are content-addressed and receipt times are recorded. SEC access requires
`HINDSIGHT_SEC_USER_AGENT` with your actual application/contact information. No
contact information is fabricated.

`fetch-alpaca SYMBOLS START END ARCHIVE_DIR` reads `APCA_API_KEY_ID` and
`APCA_API_SECRET_KEY` from the environment. It requests raw one-minute historical
SIP bars, follows all page tokens, detects cycles, preserves volume and archives
each response. The end must be at least fifteen minutes old. Keys are never
written to manifests. HTTP/rate-limit failures stop explicitly and retain completed
captures; the tool does not retry indefinitely or pretend partial data is complete.

`import-sec FILE TAXONOMY TAG ENTITY METRIC OUTPUT_JSON` normalizes a captured
companyfacts response. It preserves accessions, units and periods and rejects
ambiguous same-day corrections. This is consolidated aggregate XBRL support, NOT
original segment/custom-context document extraction or a frames-based PIT store.
The original issuer documents and consistent reporting-duration series still need
review. Normalized adapter output is not automatically approved for research: join
it to a reviewed calendar, corporate actions and source register first.

Public visibility is not redistribution or commercial-use permission. Captures,
credentials and generated local reports are ignored by git. Detailed historical
memory spot prices are not claimed to be freely available.

## Dashboard and warehouse

The self-contained `index.html` has Industry, Evidence, Historical Replay and
Experiments views, uses no external script or model, escapes embedded JSON, and
renders source text as text rather than HTML. Replay only displays evidence in the
selected historical snapshot. Its blind-practice mode supports local notes and
explicit outcome reveal; it is a learning UI, not an adversarial security sandbox.

```sh
.venv/bin/python -m pip install -e '.[dev,economic]'
.venv/bin/python -m hindsight.economic_store reports/economic-new-run/report.json reports/economic-new-warehouse
```

The optional Python storage boundary exports stable-schema Parquet tables and a
DuckDB database for decisions, scored forecasts, events and provenance. It does
not recalculate forecasts or money. A `_SUCCESS.json` marks completed warehouses;
partial exports are not successful research artifacts.

## Interpretation and remaining work

The primary statistic is paired out-of-sample forecast-loss improvement. Moving
blocks reflect overlapping forecast horizons; release counts are reported
separately from expanded market rows. Walk-forward results are development
results, not an untouched prospective holdout. The demo's retrospective security
selection and synthetic relationships cannot substantiate alpha.

A larger, evidence-backed universe, original filing-context extraction, automatic
calendar/corporate-action ingestion, matched degree/sector placebo networks,
prospective validation and production operations remain separate work. No capital
should be deployed based on this software demonstration. Family-business decision
support and securities trading require different permissions and evaluation.

## Validation

Run `bash scripts/verify-economic.sh` for native formatting/lint/tests, the existing
Python checks, old and new demos, falsification and deterministic replay. Browser
checks use `npm --prefix ui/economic ci --ignore-scripts`, a Playwright browser
installation, then `node ui/economic/verify.mjs RUN/index.html OUTPUT_DIR`.
Set `HINDSIGHT_CHROME` to an existing Chrome executable to avoid another browser
download. Test evidence is generated under `reports`, not hand-written into results.
