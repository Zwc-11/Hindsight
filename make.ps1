param(
    [ValidateSet("install", "lint", "typecheck", "test", "coverage", "run", "benchmark", "smoke", "demo", "gates")]
    [string] $Task = "test"
)

if ($Task -eq "install") {
    python -m pip install -e ".[dev]"
    exit $LASTEXITCODE
}

if ($Task -eq "test") {
    python -m pytest tests/hindsight -q
    exit $LASTEXITCODE
}

if ($Task -eq "coverage" -or $Task -eq "gates") {
    python -m coverage run -m pytest tests/hindsight -q
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    python -m coverage report
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

if ($Task -eq "lint" -or $Task -eq "gates") {
    python -m ruff check .
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

if ($Task -eq "typecheck" -or $Task -eq "gates") {
    python -m mypy
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

if ($Task -eq "run" -or $Task -eq "smoke") {
    python -m hindsight.cli run
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

if ($Task -eq "benchmark" -or $Task -eq "smoke") {
    python -m hindsight.cli benchmark
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

if ($Task -eq "smoke") {
    exit $LASTEXITCODE
}

if ($Task -eq "demo" -or $Task -eq "gates") {
    python -m hindsight.cli demo
    exit $LASTEXITCODE
}
