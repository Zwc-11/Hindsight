# Desktop implementation validation

Machine: connected Apple Silicon Mac; repository `~/Desktop/Hindsight`.
Baseline revision: `52404c1` on `main`. Existing project work was preserved.

The reproducible gate is `bash scripts/verify-economic.sh`. Its output includes
raw command logs, generated reports, native falsification JSON, Parquet/DuckDB
exports and browser screenshots. `_SUCCESS.json` is written only after every
requested gate succeeds. See `reports/verification-desktop-final-20260916` for a
completed run; additional reruns use fresh immutable directory names.

Verified in that completed run:

- 202 Python tests passed, including the legacy suite and integration/storage tests.
- Python statement coverage: 96% (the existing 95% threshold remains enforced).
- 54 native Rust/C++ tests passed, including 1,000 generated future-correction cases.
- Rust formatting and warning-denying Clippy, Python Ruff and strict Mypy passed.
- Existing demo, replay, benchmark, report-site generation and 5/5 leakage controls passed.
- Native full-pipeline falsification: 9/9 checks passed.
- Repeated native report JSON and dashboard HTML were byte-identical.
- 14 browser checks passed with no JavaScript runtime errors. A subsequent mobile
  table readability improvement adds a fifteenth check; consult the newer run's
  browser/results.json rather than assuming an old report contains this change.
- Synthetic fixture: 81 supplied XNYS sessions, 31,590 minute bars per symbol,
  324 strategy decisions, 224 scored forecasts and 586 lifecycle records.
- Integer money, null predictions and empty-table schemas survived warehouse export.

A real Nanya release was captured over HTTPS and inspected for the expected
publication date and monthly-revenue content. The raw capture remains local under
`.economic-cache`; the source register records its hash and the access-test scope.
This is not validation of an Alpaca account or a real-data trading strategy.

CI configuration was added but has not been pushed or executed on GitHub here.
The C++ sanitizer attempt is an additional gate and is not implied by the normal
passing tests; consult its separately recorded command log/result.

Research limits remain in `economic-research.md`: no live execution, profitability
claim, untouched prospective holdout, production ticker master, full original
filing-context extraction, or calibrated queue simulation. Tests demonstrate the
stated contracts and cases, not immunity to every possible modeling error.
