param(
    [ValidateSet("install", "test", "run", "compare", "benchmark", "demo")]
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

if ($Task -eq "run") {
    python -m hindsight.cli run
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

if ($Task -eq "compare") {
    python -m hindsight.cli compare
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

if ($Task -eq "benchmark") {
    python -m hindsight.cli benchmark
    exit $LASTEXITCODE
}

if ($Task -eq "demo") {
    python -m hindsight.cli demo
    exit $LASTEXITCODE
}
