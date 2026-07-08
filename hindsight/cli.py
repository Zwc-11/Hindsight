"""Command-line entry point for Hindsight M0."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from hindsight import __version__
from hindsight.core.clock import ReplayClock
from hindsight.core.events import CanonicalEvent
from hindsight.core.hashing import run_hash, stable_hash
from hindsight.data.hyperliquid_adapter import HyperliquidLakeAdapter, HyperliquidMarkoutRecord
from hindsight.demo import run_demo
from hindsight.evaluation.benchmark import (
    BenchmarkResult,
    QuotePolicy,
    benchmark_quote_policies,
    label_intervals,
    leaky_markout_policy,
    maker_side_policy,
    quote_all_policy,
)
from hindsight.evaluation.falsification import (
    falsification_payload,
    run_falsification,
    run_falsification_on_records,
    write_falsification_json,
)
from hindsight.evaluation.flagship import (
    DEFAULT_PHASE0_SYMBOLS,
    Phase0CoverageError,
    run_flagship_benchmark,
    write_flagship_coverage,
)
from hindsight.evaluation.leakage import LeakageError
from hindsight.evaluation.walk_forward import WalkForwardFold, purged_walk_forward_folds
from hindsight.execution.config import ExecConfig
from hindsight.pit.view import PointInTimeView
from hindsight.reporting.html_report import write_html_report
from hindsight.reporting.json_report import HindsightReport, write_json_report
from hindsight.reporting.leaderboard import write_leaderboard_csv
from hindsight.reporting.leakage_report import (
    quote_policy_leakage_payload,
    write_leakage_json,
)
from hindsight.reporting.manifest import RunManifest, current_git_sha
from hindsight.reporting.markdown_report import write_markdown_report
from hindsight.reporting.pages import build_pages_site
from hindsight.reporting.report_v2 import (
    build_report_v2,
    validate_report_v2,
    write_report_v2_json,
)
from hindsight.strategy.base import NoopStrategy, Strategy


@dataclass(frozen=True, slots=True)
class RunArtifacts:
    """Paths and report returned by a CLI run."""

    json_path: Path
    markdown_path: Path
    manifest_path: Path
    report_json_path: Path | None
    tearsheet_path: Path | None
    report: HindsightReport


@dataclass(frozen=True, slots=True)
class BenchmarkArtifacts:
    """Paths and result returned by a benchmark run."""

    csv_path: Path
    leakage_path: Path
    report_json_path: Path
    tearsheet_path: Path
    result: BenchmarkResult


REPORT_PINNED_VERSIONS: dict[str, str] = {
    "python": ">=3.12",
    "hindsight": __version__,
    "pydantic": ">=2.7",
    "pyarrow": ">=15",
}


def default_exec_config() -> ExecConfig:
    """Return the explicit M0 CLI config.

    Default reason: the CLI requires `hindsight run` to work from the repo
    root against the bundled Hyperliquid sample. These values make that smoke path
    reproducible; callers can still pass a different config to `run_hindsight`.
    """
    return ExecConfig(
        engine_version=__version__,
        initial_cash=100_000.0,
        maker_fee_bps=1.5,
        taker_fee_bps=4.5,
        slippage_impact_bps=0.5,
        latency_ms=50,
        funding_rate_bps=0.0,
        funding_interval_hours=8,
        participation_cap=0.1,
        seed=0,
    )


def default_naive_config() -> ExecConfig:
    """Return the explicit costless comparison config used by tests and demos."""

    return ExecConfig(
        engine_version=__version__,
        initial_cash=100_000.0,
        maker_fee_bps=0.0,
        taker_fee_bps=0.0,
        slippage_impact_bps=0.0,
        latency_ms=0,
        funding_rate_bps=0.0,
        funding_interval_hours=8,
        participation_cap=1.0,
        seed=0,
    )


def run_hindsight(
    *,
    lake_root: Path,
    output_dir: Path,
    symbol: str,
    date: str | None,
    limit: int,
    config: ExecConfig,
    strategy: Strategy,
    repo_root: Path,
) -> RunArtifacts:
    adapter = HyperliquidLakeAdapter(lake_root)
    try:
        events = list(adapter.stream_events(symbol=symbol, date=date, limit=limit))
    except FileNotFoundError as exc:
        raise FileNotFoundError(
            f"No market events loaded for {symbol.upper()} (date={date})"
        ) from exc
    # Defensive reason: the data lake is an external filesystem dependency.
    # If no market events load, the run is misconfigured and must fail loudly
    # instead of writing a generic zero-event success report.
    if not events:
        raise FileNotFoundError(f"No market events loaded for {symbol.upper()} (date={date})")
    clock = ReplayClock()
    view = PointInTimeView(clock, events)
    strategy.on_start()
    orders_emitted = 0
    for event in events:
        clock.advance(event.timestamp)
        orders_emitted += len(strategy.on_event(event, view))
    strategy.on_finish()

    event_records = [_event_record(event) for event in events]
    data_content_hash = run_hash(event_records)
    config_hash = stable_hash(config.model_dump(mode="json"))
    manifest = RunManifest.build(
        engine_version=config.engine_version,
        git_sha=current_git_sha(repo_root),
        data_content_hash=data_content_hash,
        config_hash=config_hash,
        seed=config.seed,
    )
    first_timestamp = events[0].timestamp.isoformat()
    last_timestamp = events[-1].timestamp.isoformat()
    report = HindsightReport(
        strategy_name=strategy.name,
        symbol=symbol.upper(),
        date=date,
        events_processed=len(events),
        orders_emitted=orders_emitted,
        first_timestamp=first_timestamp,
        last_timestamp=last_timestamp,
        event_types=dict(Counter(str(event.event_type) for event in events)),
        run_hash=data_content_hash,
        manifest=manifest,
    )

    json_path = output_dir / "hindsight-report.json"
    markdown_path = output_dir / "hindsight-report.md"
    manifest_path = output_dir / "manifest.json"
    write_json_report(json_path, report)
    write_markdown_report(markdown_path, report)
    manifest_path.write_text(
        manifest.model_dump_json(indent=2) + "\n",
        encoding="utf-8",
    )
    report_json_path, tearsheet_path = _try_write_run_audit_report(
        adapter=adapter,
        lake_root=lake_root,
        output_dir=output_dir,
        symbol=symbol,
        date=date,
        limit=limit,
        config=config,
        repo_root=repo_root,
    )
    return RunArtifacts(
        json_path=json_path,
        markdown_path=markdown_path,
        manifest_path=manifest_path,
        report_json_path=report_json_path,
        tearsheet_path=tearsheet_path,
        report=report,
    )


def run_benchmark(
    *,
    lake_root: Path,
    output_dir: Path,
    symbol: str,
    date: str,
    limit: int,
    horizon: str,
    label_horizon_seconds: float,
    n_folds: int,
    train_window: int,
    test_window: int,
    purge_seconds: float,
    embargo_seconds: float,
    include_leaky: bool,
    dates: tuple[str, ...] | None = None,
) -> BenchmarkArtifacts:
    adapter = HyperliquidLakeAdapter(lake_root)
    benchmark_dates = dates if dates is not None else (date,)
    if not benchmark_dates:
        raise ValueError("benchmark requires at least one date")
    records = _load_markout_records(
        adapter,
        symbol=symbol,
        dates=benchmark_dates,
        limit=limit,
    )
    if not records:
        raise FileNotFoundError(f"No Hyperliquid markout rows loaded for {symbol.upper()}")
    intervals = label_intervals(records, horizon=timedelta(seconds=label_horizon_seconds))
    folds = purged_walk_forward_folds(
        intervals,
        n_folds=n_folds,
        train_window=train_window,
        test_window=test_window,
        purge=timedelta(seconds=purge_seconds),
        embargo=timedelta(seconds=embargo_seconds),
    )
    baseline = quote_all_policy(len(records))
    clean_policy = maker_side_policy(records)
    policies = [
        baseline,
        clean_policy,
    ]
    if include_leaky:
        policies.append(leaky_markout_policy(records, horizon_key=horizon))
    target_name = f"markout_bps_{horizon}"
    leakage_path = output_dir / "leakage.json"
    write_leakage_json(
        leakage_path,
        quote_policy_leakage_payload(policies=tuple(policies), target_name=target_name),
    )
    result = benchmark_quote_policies(
        records=records,
        folds=folds,
        policies=tuple(policies),
        baseline_policy_name="ofi_quote",
        horizon_key=horizon,
        target_name=target_name,
        fail_on_leakage=True,
    )
    csv_path = output_dir / "hindsight-leaderboard.csv"
    report_json_path = output_dir / "report.json"
    tearsheet_path = output_dir / "tearsheet.html"
    write_leaderboard_csv(csv_path, result)
    report_payload = _build_audit_report(
        sample_root=lake_root,
        symbol=symbol,
        date=_date_label(benchmark_dates),
        horizon=horizon,
        data_label="local Hyperliquid markout",
        records=records,
        folds=folds,
        baseline_policy=baseline,
        clean_policy=clean_policy,
        repo_root=Path.cwd(),
        config=default_exec_config(),
        honest_result=result,
        label_horizon_seconds=label_horizon_seconds,
        reproduce_command=_benchmark_reproduce_command(
            lake_root=lake_root,
            output_dir=output_dir,
            symbol=symbol,
            dates=benchmark_dates,
            limit=limit,
            horizon=horizon,
            label_horizon_seconds=label_horizon_seconds,
            n_folds=n_folds,
            train_window=train_window,
            test_window=test_window,
            purge_seconds=purge_seconds,
            embargo_seconds=embargo_seconds,
        ),
    )
    write_report_v2_json(report_json_path, report_payload)
    write_html_report(tearsheet_path, report_payload)
    return BenchmarkArtifacts(
        csv_path=csv_path,
        leakage_path=leakage_path,
        report_json_path=report_json_path,
        tearsheet_path=tearsheet_path,
        result=result,
    )


def _try_write_run_audit_report(
    *,
    adapter: HyperliquidLakeAdapter,
    lake_root: Path,
    output_dir: Path,
    symbol: str,
    date: str | None,
    limit: int,
    config: ExecConfig,
    repo_root: Path,
) -> tuple[Path | None, Path | None]:
    if date is None:
        return (None, None)
    try:
        records = adapter.load_markouts(symbol=symbol, date=date, limit=limit)
        horizon = _preferred_horizon(records)
        folds = _default_report_folds(records, horizon=horizon)
        baseline = quote_all_policy(len(records))
        clean_policy = maker_side_policy(records)
        honest_result = benchmark_quote_policies(
            records=records,
            folds=folds,
            policies=(baseline, clean_policy),
            baseline_policy_name=baseline.name,
            horizon_key=horizon,
            target_name=f"markout_bps_{horizon}",
            fail_on_leakage=True,
        )
        report_payload = _build_audit_report(
            sample_root=lake_root,
            symbol=symbol,
            date=date,
            horizon=horizon,
            data_label="local Hyperliquid markout",
            records=records,
            folds=folds,
            baseline_policy=baseline,
            clean_policy=clean_policy,
            repo_root=repo_root,
            config=config,
            honest_result=honest_result,
            label_horizon_seconds=_horizon_seconds(horizon),
            reproduce_command=_run_reproduce_command(
                lake_root=lake_root,
                output_dir=output_dir,
                symbol=symbol,
                date=date,
                limit=limit,
            ),
        )
    except (FileNotFoundError, ValueError):
        return (None, None)
    report_json_path = output_dir / "report.json"
    tearsheet_path = output_dir / "tearsheet.html"
    write_report_v2_json(report_json_path, report_payload)
    write_html_report(tearsheet_path, report_payload)
    return (report_json_path, tearsheet_path)


def _build_audit_report(
    *,
    sample_root: Path,
    symbol: str,
    date: str,
    horizon: str,
    data_label: str,
    records: list[HyperliquidMarkoutRecord],
    folds: tuple[WalkForwardFold, ...],
    baseline_policy: QuotePolicy,
    clean_policy: QuotePolicy,
    repo_root: Path,
    config: ExecConfig,
    honest_result: BenchmarkResult,
    label_horizon_seconds: float,
    reproduce_command: str,
) -> dict[str, Any]:
    leaky_policy = leaky_markout_policy(records, horizon_key=horizon)
    target_name = f"markout_bps_{horizon}"
    leakage_payload = quote_policy_leakage_payload(
        policies=(baseline_policy, clean_policy, leaky_policy),
        target_name=target_name,
    )
    naive_result = benchmark_quote_policies(
        records=records,
        folds=folds,
        policies=(baseline_policy, clean_policy, leaky_policy),
        baseline_policy_name=baseline_policy.name,
        horizon_key=horizon,
        target_name=target_name,
        fail_on_leakage=False,
    )
    blocked_error = _blocked_leaky_message(
        records=records,
        folds=folds,
        policies=(baseline_policy, clean_policy, leaky_policy),
        baseline_policy_name=baseline_policy.name,
        horizon_key=horizon,
        target_name=target_name,
    )
    manifest = RunManifest.build(
        engine_version=config.engine_version,
        git_sha=current_git_sha(repo_root),
        data_content_hash=run_hash([_markout_record_payload(record) for record in records]),
        config_hash=stable_hash(
            {
                "config": config.model_dump(mode="json"),
                "horizon": horizon,
                "reproduce_command": reproduce_command,
            }
        ),
        seed=config.seed,
    )
    return build_report_v2(
        sample_root=sample_root,
        symbol=symbol.upper(),
        date=date,
        horizon=horizon,
        data_label=data_label,
        records=records,
        baseline_policy=baseline_policy,
        clean_policy=clean_policy,
        leaky_policy=leaky_policy,
        naive_result=naive_result,
        honest_result=honest_result,
        blocked_error=blocked_error,
        leakage_payload=leakage_payload,
        manifest=manifest,
        cost_model=_cost_model_from_config(config),
        reproduce_command=reproduce_command,
        pinned_versions=REPORT_PINNED_VERSIONS,
        falsification=_falsification_for_records(
            records=records,
            horizon=horizon,
            label_horizon_seconds=label_horizon_seconds,
        ),
    )


def _falsification_for_records(
    *,
    records: Sequence[HyperliquidMarkoutRecord],
    horizon: str,
    label_horizon_seconds: float,
) -> dict[str, Any] | None:
    try:
        return falsification_payload(
            run_falsification_on_records(
                records,
                horizon=horizon,
                label_horizon_seconds=label_horizon_seconds,
                max_records=256,
            )
        )
    except ValueError:
        return None


def _blocked_leaky_message(
    *,
    records: list[HyperliquidMarkoutRecord],
    folds: tuple[WalkForwardFold, ...],
    policies: tuple[QuotePolicy, ...],
    baseline_policy_name: str,
    horizon_key: str,
    target_name: str,
) -> str:
    try:
        benchmark_quote_policies(
            records=records,
            folds=folds,
            policies=policies,
            baseline_policy_name=baseline_policy_name,
            horizon_key=horizon_key,
            target_name=target_name,
            fail_on_leakage=True,
        )
    except LeakageError as exc:
        return str(exc)
    return "leaky policy passed unexpectedly"


def _default_report_folds(
    records: list[HyperliquidMarkoutRecord],
    *,
    horizon: str,
) -> tuple[WalkForwardFold, ...]:
    if len(records) >= 8:
        return purged_walk_forward_folds(
            label_intervals(records, horizon=timedelta(seconds=_horizon_seconds(horizon))),
            n_folds=2,
            train_window=4,
            test_window=2,
            purge=timedelta(seconds=_horizon_seconds(horizon)),
            embargo=timedelta(seconds=_horizon_seconds(horizon)),
        )
    if len(records) >= 3:
        return purged_walk_forward_folds(
            label_intervals(records, horizon=timedelta(seconds=_horizon_seconds(horizon))),
            n_folds=1,
            train_window=1,
            test_window=1,
            purge=timedelta(0),
            embargo=timedelta(0),
        )
    raise ValueError("at least three markout rows are required for report v2")


def _preferred_horizon(records: list[HyperliquidMarkoutRecord]) -> str:
    horizons = sorted(
        {key for record in records for key in record.markout_bps},
        key=_horizon_seconds,
    )
    if not horizons:
        raise ValueError("markout rows do not contain any horizons")
    if "10s" in horizons:
        return "10s"
    return horizons[0]


def _horizon_seconds(horizon: str) -> float:
    units = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}
    for suffix in ("ms", "s", "m", "h", "d"):
        if horizon.endswith(suffix):
            try:
                return float(horizon[: -len(suffix)]) * units[suffix]
            except ValueError:
                break
    raise ValueError(f"unsupported markout horizon: {horizon}")


def _cost_model_from_config(config: ExecConfig) -> dict[str, Any]:
    return {
        "maker_fee_bps": config.maker_fee_bps,
        "taker_fee_bps": config.taker_fee_bps,
        "slippage_impact_bps": config.slippage_impact_bps,
        "latency_ms": config.latency_ms,
        "funding_rate_bps": config.funding_rate_bps or 0.0,
        "funding_interval_hours": config.funding_interval_hours,
        "participation_cap": config.participation_cap,
        "liquidity_assumption": "maker",
        "fee_tier_note": (
            "CLI default Hyperliquid base perps tier; override execution config "
            "for account-specific fees or rebates."
        ),
    }


def _markout_record_payload(record: HyperliquidMarkoutRecord) -> dict[str, Any]:
    return {
        "symbol": record.symbol,
        "timestamp": record.timestamp.isoformat(),
        "price": record.price,
        "quantity": record.quantity,
        "side": record.side.value,
        "maker_side": record.maker_side,
        "trade_id": record.trade_id,
        "markout_bps": dict(sorted(record.markout_bps.items())),
    }


def _date_label(dates: tuple[str, ...]) -> str:
    if len(dates) == 1:
        return dates[0]
    return ",".join(dates)


def _benchmark_reproduce_command(
    *,
    lake_root: Path,
    output_dir: Path,
    symbol: str,
    dates: tuple[str, ...],
    limit: int,
    horizon: str,
    label_horizon_seconds: float,
    n_folds: int,
    train_window: int,
    test_window: int,
    purge_seconds: float,
    embargo_seconds: float,
) -> str:
    return " ".join(
        [
            "python -m hindsight.cli benchmark",
            f"--lake-root {lake_root}",
            f"--output-dir {output_dir}",
            f"--symbol {symbol}",
            f"--dates {_date_label(dates)}",
            f"--limit {limit}",
            f"--horizon {horizon}",
            f"--label-horizon-seconds {label_horizon_seconds:g}",
            f"--n-folds {n_folds}",
            f"--train-window {train_window}",
            f"--test-window {test_window}",
            f"--purge-seconds {purge_seconds:g}",
            f"--embargo-seconds {embargo_seconds:g}",
        ]
    )


def _run_reproduce_command(
    *,
    lake_root: Path,
    output_dir: Path,
    symbol: str,
    date: str,
    limit: int,
) -> str:
    return " ".join(
        [
            "python -m hindsight.cli run",
            f"--lake-root {lake_root}",
            f"--output-dir {output_dir}",
            f"--symbol {symbol}",
            f"--date {date}",
            f"--limit {limit}",
        ]
    )


def run_report(*, run_dir: Path, output: Path | None) -> Path:
    """Render a tearsheet HTML from a saved ``report.json`` in ``run_dir``.

    Works for any historical run that carries a schema-v2 ``report.json`` (demo
    runs write one). The payload is validated before rendering so a malformed or
    older report fails loudly instead of producing a broken tearsheet.
    """
    report_path = run_dir / "report.json"
    if not report_path.is_file():
        raise FileNotFoundError(f"No report.json found in {run_dir}")
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    validate_report_v2(payload)
    output_path = output if output is not None else run_dir / "tearsheet.html"
    write_html_report(output_path, payload)
    return output_path


def parse_date_spec(value: str) -> tuple[str, ...]:
    """Parse comma-separated YYYYMMDD dates and inclusive YYYYMMDD..YYYYMMDD ranges."""

    dates: list[str] = []
    for token in _split_csv(value):
        if ".." in token:
            dates.extend(_expand_date_range(token))
        else:
            dates.append(_validate_date(token))
    if not dates:
        raise ValueError("date spec cannot be empty")
    return tuple(dict.fromkeys(dates))


def parse_symbol_spec(value: str) -> tuple[str, ...]:
    """Parse comma-separated perp symbols and normalize the ``-PERP`` suffix."""

    symbols: list[str] = []
    for raw in value.split(","):
        cleaned = raw.strip().upper()
        if not cleaned:
            continue
        coin = cleaned.removesuffix("-PERP")
        if not coin:
            raise ValueError("symbol cannot be empty")
        symbols.append(f"{coin}-PERP")
    if not symbols:
        raise ValueError("symbol list cannot be empty")
    return tuple(dict.fromkeys(symbols))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hindsight")
    subparsers = parser.add_subparsers(dest="command", required=True)
    run_parser = subparsers.add_parser("run")
    # Defaults reason: the spec calls for `hindsight run` to stream the bundled
    # synthetic Hyperliquid sample without extra setup. These point at that
    # explicit on-disk dataset and output location; user-supplied flags override them.
    run_parser.add_argument(
        "--lake-root",
        type=Path,
        default=Path("examples/sample_data/hyperliquid"),
    )
    run_parser.add_argument("--output-dir", type=Path, default=Path("reports/hindsight"))
    run_parser.add_argument("--symbol", default="SOL-PERP")
    run_parser.add_argument("--date", default="20260101")
    run_parser.add_argument("--limit", type=int, default=1440)
    benchmark_parser = subparsers.add_parser("benchmark")
    benchmark_parser.add_argument(
        "--lake-root",
        type=Path,
        default=Path("examples/sample_data/hyperliquid"),
    )
    benchmark_parser.add_argument("--output-dir", type=Path, default=Path("reports/hindsight"))
    benchmark_parser.add_argument("--symbol", default="SOL-PERP")
    benchmark_parser.add_argument("--date", default="20260101")
    benchmark_parser.add_argument("--dates")
    benchmark_parser.add_argument("--limit", type=int, default=120)
    benchmark_parser.add_argument("--horizon", default="10s")
    benchmark_parser.add_argument("--label-horizon-seconds", type=float, default=10.0)
    benchmark_parser.add_argument("--n-folds", type=int, default=2)
    benchmark_parser.add_argument("--train-window", type=int, default=4)
    benchmark_parser.add_argument("--test-window", type=int, default=2)
    benchmark_parser.add_argument("--purge-seconds", type=float, default=0.0)
    benchmark_parser.add_argument("--embargo-seconds", type=float, default=0.0)
    benchmark_parser.add_argument("--include-leaky", action="store_true")
    demo_parser = subparsers.add_parser("demo")
    demo_parser.add_argument(
        "--sample-root",
        type=Path,
        default=Path("examples/sample_data/hyperliquid"),
    )
    demo_parser.add_argument("--output-dir", type=Path, default=Path("reports/hindsight-demo"))
    demo_parser.add_argument("--symbol", default="SOL-PERP")
    demo_parser.add_argument("--date", default="20260101")
    demo_parser.add_argument("--limit", type=int, default=8)
    demo_parser.add_argument("--horizon", default="10s")
    demo_parser.add_argument("--label-horizon-seconds", type=float, default=10.0)
    demo_parser.add_argument("--seed", type=int, default=0)
    report_parser = subparsers.add_parser("report")
    report_parser.add_argument("run_dir", type=Path)
    report_parser.add_argument("--output", type=Path, default=None)
    pages_parser = subparsers.add_parser("pages")
    pages_parser.add_argument("--output-dir", type=Path, default=Path("_site"))
    falsify_parser = subparsers.add_parser("falsify")
    falsify_parser.add_argument(
        "--sample-root",
        type=Path,
        default=Path("examples/sample_data/hyperliquid"),
    )
    falsify_parser.add_argument(
        "--output-dir", type=Path, default=Path("reports/hindsight-falsify")
    )
    falsify_parser.add_argument("--symbol", default="SOL-PERP")
    falsify_parser.add_argument("--date", default="20260101")
    falsify_parser.add_argument("--horizon", default="10s")
    falsify_parser.add_argument("--label-horizon-seconds", type=float, default=10.0)
    falsify_parser.add_argument("--limit", type=int, default=8)
    flagship_parser = subparsers.add_parser("flagship")
    flagship_parser.add_argument(
        "--lake-root",
        type=Path,
        default=Path("C:/MarketImmune/data/hyperliquid"),
    )
    flagship_parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("docs/benchmarks/phase0-flagship"),
    )
    flagship_parser.add_argument(
        "--symbols",
        default=",".join(DEFAULT_PHASE0_SYMBOLS),
        help="comma-separated symbols, e.g. SOL-PERP,ETH-PERP,BTC-PERP",
    )
    flagship_parser.add_argument("--dates", required=True)
    flagship_parser.add_argument("--rows-per-partition", type=int, default=5000)
    flagship_parser.add_argument("--horizon", default="10s")
    flagship_parser.add_argument("--label-horizon-seconds", type=float, default=10.0)
    flagship_parser.add_argument("--n-folds", type=int, default=8)
    flagship_parser.add_argument("--train-window", type=int, default=3000)
    flagship_parser.add_argument("--test-window", type=int, default=500)
    flagship_parser.add_argument("--purge-seconds", type=float, default=10.0)
    flagship_parser.add_argument("--embargo-seconds", type=float, default=10.0)
    flagship_parser.add_argument("--coverage-only", action="store_true")
    flagship_parser.add_argument(
        "--allow-incomplete",
        action="store_true",
        help="exit zero for coverage-only audits that do not yet satisfy Phase 0",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "benchmark":
        benchmark_artifacts = run_benchmark(
            lake_root=args.lake_root,
            output_dir=args.output_dir,
            symbol=args.symbol,
            date=args.date,
            dates=parse_date_spec(args.dates) if args.dates is not None else None,
            limit=args.limit,
            horizon=args.horizon,
            label_horizon_seconds=args.label_horizon_seconds,
            n_folds=args.n_folds,
            train_window=args.train_window,
            test_window=args.test_window,
            purge_seconds=args.purge_seconds,
            embargo_seconds=args.embargo_seconds,
            include_leaky=args.include_leaky,
        )
        print(f"Wrote {benchmark_artifacts.csv_path}")
        print(f"Wrote {benchmark_artifacts.leakage_path}")
        print(f"Wrote {benchmark_artifacts.report_json_path}")
        print(f"Wrote {benchmark_artifacts.tearsheet_path}")
        print(f"Benchmark hash {benchmark_artifacts.result.run_hash}")
        return 0
    if args.command == "demo":
        demo_artifacts = run_demo(
            sample_root=args.sample_root,
            output_dir=args.output_dir,
            repo_root=Path.cwd(),
            symbol=args.symbol,
            date=args.date,
            limit=args.limit,
            horizon=args.horizon,
            label_horizon_seconds=args.label_horizon_seconds,
            seed=args.seed,
        )
        print("Sample data: synthetic")
        print("Naive control: deliberately unsafe control - do not use")
        print("Hindsight audit: blocked leaky policy")
        print(f"Wrote {demo_artifacts.json_path}")
        print(f"Wrote {demo_artifacts.markdown_path}")
        print(f"Wrote {demo_artifacts.manifest_path}")
        print(f"Wrote {demo_artifacts.report_json_path}")
        print(f"Wrote {demo_artifacts.tearsheet_path}")
        print(f"Demo run id {demo_artifacts.manifest.run_id}")
        return 0
    if args.command == "report":
        tearsheet_path = run_report(run_dir=args.run_dir, output=args.output)
        print(f"Wrote {tearsheet_path}")
        return 0
    if args.command == "pages":
        site = build_pages_site(repo_root=Path.cwd(), output_dir=args.output_dir)
        print(f"Wrote {site.index_path}")
        print(f"Wrote {site.demo_tearsheet}")
        for path in site.benchmark_tearsheets:
            print(f"Wrote {path}")
        return 0
    if args.command == "falsify":
        result = run_falsification(
            sample_root=args.sample_root,
            symbol=args.symbol,
            date=args.date,
            horizon=args.horizon,
            label_horizon_seconds=args.label_horizon_seconds,
            limit=args.limit,
        )
        output_path = args.output_dir / "falsification.json"
        write_falsification_json(output_path, result)
        for case in result.cases:
            status = "CAUGHT" if case.caught else "ESCAPED"
            print(f"{status:8} {case.defect:22} -> {case.probe}")
        caught = sum(1 for case in result.cases if case.caught)
        print(f"Falsification: {caught}/{len(result.cases)} defects caught")
        print(f"Falsification hash {result.run_hash}")
        print(f"Wrote {output_path}")
        return 0 if result.all_caught else 1
    if args.command == "flagship":
        symbols = parse_symbol_spec(args.symbols)
        dates = parse_date_spec(args.dates)
        if args.coverage_only:
            coverage = write_flagship_coverage(
                lake_root=args.lake_root,
                output_dir=args.output_dir,
                symbols=symbols,
                dates=dates,
                rows_per_partition=args.rows_per_partition,
            )
            ready = bool(coverage.payload["coverage"]["ready"])
            print(f"Wrote {coverage.coverage_path}")
            print("Phase 0 coverage: ready" if ready else "Phase 0 coverage: incomplete")
            return 0 if ready or args.allow_incomplete else 1
        try:
            artifacts = run_flagship_benchmark(
                lake_root=args.lake_root,
                output_dir=args.output_dir,
                symbols=symbols,
                dates=dates,
                rows_per_partition=args.rows_per_partition,
                horizon=args.horizon,
                label_horizon_seconds=args.label_horizon_seconds,
                n_folds=args.n_folds,
                train_window=args.train_window,
                test_window=args.test_window,
                purge_seconds=args.purge_seconds,
                embargo_seconds=args.embargo_seconds,
            )
        except Phase0CoverageError as exc:
            print(str(exc))
            return 1
        print(f"Wrote {artifacts.coverage_path}")
        print(f"Wrote {artifacts.json_path}")
        print(f"Wrote {artifacts.csv_path}")
        print(f"Wrote {artifacts.leakage_path}")
        print(f"Wrote {artifacts.falsification_path}")
        print(f"Wrote {artifacts.report_json_path}")
        print(f"Wrote {artifacts.tearsheet_path}")
        print(f"Wrote {artifacts.readme_path}")
        print(f"Flagship benchmark hash {artifacts.result.run_hash}")
        return 0 if artifacts.payload["acceptance"]["passed"] else 1
    run_artifacts = run_hindsight(
        lake_root=args.lake_root,
        output_dir=args.output_dir,
        symbol=args.symbol,
        date=args.date,
        limit=args.limit,
        config=default_exec_config(),
        strategy=NoopStrategy(),
        repo_root=Path.cwd(),
    )
    print(f"Wrote {run_artifacts.json_path}")
    print(f"Wrote {run_artifacts.markdown_path}")
    print(f"Wrote {run_artifacts.manifest_path}")
    if run_artifacts.report_json_path is not None:
        print(f"Wrote {run_artifacts.report_json_path}")
    if run_artifacts.tearsheet_path is not None:
        print(f"Wrote {run_artifacts.tearsheet_path}")
    return 0


def _event_record(event: CanonicalEvent) -> dict[str, object]:
    return dict(event.model_dump(mode="json"))


def _load_markout_records(
    adapter: HyperliquidLakeAdapter,
    *,
    symbol: str,
    dates: tuple[str, ...],
    limit: int,
) -> list[HyperliquidMarkoutRecord]:
    if limit < 1:
        raise ValueError("limit must be positive")
    records: list[HyperliquidMarkoutRecord] = []
    remaining = limit
    for date in dates:
        if remaining <= 0:
            break
        date_records = adapter.load_markouts(symbol=symbol, date=date, limit=remaining)
        records.extend(date_records)
        remaining = limit - len(records)
    return records


def _split_csv(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())


def _validate_date(value: str) -> str:
    if len(value) != 8 or not value.isdigit():
        raise ValueError(f"invalid YYYYMMDD date: {value}")
    try:
        datetime.strptime(value, "%Y%m%d")
    except ValueError as exc:
        raise ValueError(f"invalid YYYYMMDD date: {value}") from exc
    return value


def _expand_date_range(value: str) -> list[str]:
    start_text, end_text = value.split("..", 1)
    start = datetime.strptime(_validate_date(start_text), "%Y%m%d").date()
    end = datetime.strptime(_validate_date(end_text), "%Y%m%d").date()
    if end < start:
        raise ValueError("date range end must be >= start")
    dates: list[str] = []
    current = start
    while current <= end:
        dates.append(current.strftime("%Y%m%d"))
        current += timedelta(days=1)
    return dates


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
