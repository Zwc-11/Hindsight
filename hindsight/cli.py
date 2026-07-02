"""Command-line entry point for Hindsight M0."""

from __future__ import annotations

import argparse
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from hindsight import __version__
from hindsight.core.clock import ReplayClock
from hindsight.core.events import CanonicalEvent
from hindsight.core.hashing import run_hash, stable_hash
from hindsight.data.hyperliquid_adapter import HyperliquidLakeAdapter, HyperliquidMarkoutRecord
from hindsight.demo import run_demo
from hindsight.evaluation.benchmark import (
    BenchmarkResult,
    benchmark_quote_policies,
    label_intervals,
    leaky_markout_policy,
    maker_side_policy,
    quote_all_policy,
)
from hindsight.evaluation.walk_forward import purged_walk_forward_folds
from hindsight.execution.config import ExecConfig
from hindsight.pit.view import PointInTimeView
from hindsight.reporting.json_report import HindsightReport, write_json_report
from hindsight.reporting.leaderboard import write_leaderboard_csv
from hindsight.reporting.leakage_report import (
    quote_policy_leakage_payload,
    write_leakage_json,
)
from hindsight.reporting.manifest import RunManifest, current_git_sha
from hindsight.reporting.markdown_report import write_markdown_report
from hindsight.strategy.base import NoopStrategy, Strategy


@dataclass(frozen=True, slots=True)
class RunArtifacts:
    """Paths and report returned by a CLI run."""

    json_path: Path
    markdown_path: Path
    manifest_path: Path
    report: HindsightReport


@dataclass(frozen=True, slots=True)
class BenchmarkArtifacts:
    """Paths and result returned by a benchmark run."""

    csv_path: Path
    leakage_path: Path
    result: BenchmarkResult


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
    return RunArtifacts(
        json_path=json_path,
        markdown_path=markdown_path,
        manifest_path=manifest_path,
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
    policies = [
        quote_all_policy(len(records)),
        maker_side_policy(records),
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
    write_leaderboard_csv(csv_path, result)
    return BenchmarkArtifacts(csv_path=csv_path, leakage_path=leakage_path, result=result)


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
        print(f"Demo run id {demo_artifacts.manifest.run_id}")
        return 0
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
