#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$ROOT"
PYTHON=${PYTHON:-"$ROOT/.venv/bin/python"}
if [[ ! -x "$PYTHON" ]]; then PYTHON=python; fi
OUT=${1:-"reports/verification-$(date -u +%Y%m%dT%H%M%SZ)"}
if [[ -e "$OUT" ]]; then echo "Refusing to reuse verification directory: $OUT" >&2; exit 2; fi
mkdir -p "$OUT"
OUT=$(cd "$OUT" && pwd)
run() {
    local name=$1
    shift
    printf '\n=== %s ===\n' "$name"
    "$@" 2>&1 | tee "$OUT/$name.log"
}
run rust-format sh scripts/cargo-local.sh fmt --all --check
run rust-clippy sh scripts/cargo-local.sh clippy --locked --workspace --all-targets -- -D warnings
run rust-tests sh scripts/cargo-local.sh test --locked --workspace
run rust-build sh scripts/cargo-local.sh build --locked
run python-lint "$PYTHON" -m ruff check .
run python-types "$PYTHON" -m mypy
run python-tests "$PYTHON" -m coverage run -m pytest tests/hindsight -o addopts= -q
run python-coverage "$PYTHON" -m coverage report
run legacy-demo "$PYTHON" -m hindsight.cli demo --output-dir "$OUT/legacy-demo"
run legacy-run "$PYTHON" -m hindsight.cli run --output-dir "$OUT/legacy-run"
run legacy-benchmark "$PYTHON" -m hindsight.cli benchmark --output-dir "$OUT/legacy-benchmark"
run legacy-falsify "$PYTHON" -m hindsight.cli falsify --output-dir "$OUT/legacy-falsify"
PAGES_OUT=$(mktemp -d "${TMPDIR:-/tmp}/hindsight-pages.XXXXXX")
run legacy-pages "$PYTHON" -m hindsight.cli pages --output-dir "$PAGES_OUT"
printf '%s\n' "$PAGES_OUT" > "$OUT/legacy-pages-path.txt"
run native-demo-a "$PYTHON" -m hindsight.cli economic demo "$OUT/native-a"
run native-demo-b "$PYTHON" -m hindsight.cli economic demo "$OUT/native-b"
run deterministic-report cmp "$OUT/native-a/report.json" "$OUT/native-b/report.json"
run deterministic-dashboard cmp "$OUT/native-a/index.html" "$OUT/native-b/index.html"
run native-falsify target/debug/hindsight-economic falsify "$OUT/native-falsification.json"
run native-input target/debug/hindsight-economic demo-input "$OUT/synthetic-input.json"
run native-coverage target/debug/hindsight-economic coverage "$OUT/synthetic-input.json"
run warehouse "$PYTHON" -m hindsight.economic_store "$OUT/native-a/report.json" "$OUT/warehouse"
if [[ ${HINDSIGHT_BROWSER_TESTS:-0} == 1 ]]; then
    run browser node ui/economic/verify.mjs "$OUT/native-a/index.html" "$OUT/browser"
fi
printf '{"status":"passed","verification_dir":"%s"}\n' "$OUT" > "$OUT/_SUCCESS.json"
printf '\nVerified artifacts: %s\n' "$OUT"
