"""Command-line entry point for Hindsight M0."""

from __future__ import annotations

import argparse
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from hindsight import __version__
from hindsight.core.clock import ReplayClock
from hindsight.core.events import CanonicalEvent
from hindsight.core.hashing import run_hash, stable_hash
from hindsight.data.binance_adapter import BinanceLakeAdapter
from hindsight.data.hyperliquid_adapter import HyperliquidLakeAdapter
from hindsight.evaluation.benchmark import (
    BenchmarkResult,
    benchmark_quote_policies,
    label_intervals,
    leaky_markout_policy,
    maker_side_policy,
    quote_all_policy,
)
from hindsight.evaluation.walk_forward import purged_walk_forward_folds
from hindsight.execution.compare import ComparisonResult, compare_naive_vs_realistic
from hindsight.execution.config import ExecConfig
from hindsight.pit.view import PointInTimeView
from hindsight.reporting.backtest_report import (
    write_comparison_json,
    write_comparison_markdown,
)
from hindsight.reporting.curves import write_curve_png
from hindsight.reporting.json_report import HindsightReport, write_json_report
from hindsight.reporting.leaderboard import write_leaderboard_csv
from hindsight.reporting.manifest import RunManifest, current_git_sha
from hindsight.reporting.markdown_report import write_markdown_report
from hindsight.strategy.base import NoopStrategy, Strategy
from hindsight.strategy.baselines.momentum import MomentumStrategy


@dataclass(frozen=True, slots=True)
class RunArtifacts:
    """Paths and report returned by a CLI run."""

    json_path: Path
    markdown_path: Path
    manifest_path: Path
    report: HindsightReport


@dataclass(frozen=True, slots=True)
class ComparisonArtifacts:
    """Paths and result returned by a comparison run."""

    json_path: Path
    markdown_path: Path
    png_path: Path | None
    result: ComparisonResult


@dataclass(frozen=True, slots=True)
class BenchmarkArtifacts:
    """Paths and result returned by a benchmark run."""

    csv_path: Path
    result: BenchmarkResult


def default_exec_config() -> ExecConfig:
    """Return the explicit M0 CLI config.

    Default reason: the M0 DoD requires `hindsight run` to work from the repo
    root against the bundled Binance lake. These values make that smoke path
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
    """Return the explicit naive M1 comparison config."""

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
    adapter = BinanceLakeAdapter(lake_root)
    events = list(adapter.stream_events(symbol=symbol, date=date, limit=limit))
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


def run_comparison(
    *,
    lake_root: Path,
    output_dir: Path,
    symbol: str,
    date: str | None,
    limit: int,
    quantity: float,
    lookback_bars: int,
    threshold_bps: float,
) -> ComparisonArtifacts:
    adapter = BinanceLakeAdapter(lake_root)
    events = list(adapter.stream_events(symbol=symbol, date=date, limit=limit))
    # Defensive reason: the data lake is an external filesystem dependency.
    # If no market events load, the comparison must fail loudly.
    if not events:
        raise FileNotFoundError(f"No market events loaded for {symbol.upper()} (date={date})")
    result = compare_naive_vs_realistic(
        events=events,
        strategy_factory=lambda: MomentumStrategy(
            symbol=symbol.upper(),
            quantity=quantity,
            lookback_bars=lookback_bars,
            threshold_bps=threshold_bps,
        ),
        symbol=symbol.upper(),
        naive_config=default_naive_config(),
        realistic_config=default_exec_config(),
    )
    json_path = output_dir / "hindsight-comparison.json"
    markdown_path = output_dir / "hindsight-comparison.md"
    png_path = output_dir / "hindsight-realistic-equity.png"
    write_comparison_json(json_path, result)
    write_comparison_markdown(markdown_path, result)
    rendered_png = write_curve_png(png_path, result.realistic.equity_curve)
    return ComparisonArtifacts(
        json_path=json_path,
        markdown_path=markdown_path,
        png_path=png_path if rendered_png else None,
        result=result,
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
) -> BenchmarkArtifacts:
    adapter = HyperliquidLakeAdapter(lake_root)
    records = adapter.load_markouts(symbol=symbol, date=date, limit=limit)
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
    result = benchmark_quote_policies(
        records=records,
        folds=folds,
        policies=tuple(policies),
        baseline_policy_name="ofi_quote",
        horizon_key=horizon,
        target_name=f"markout_bps_{horizon}",
        fail_on_leakage=True,
    )
    csv_path = output_dir / "hindsight-leaderboard.csv"
    write_leaderboard_csv(csv_path, result)
    return BenchmarkArtifacts(csv_path=csv_path, result=result)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hindsight")
    subparsers = parser.add_subparsers(dest="command", required=True)
    run_parser = subparsers.add_parser("run")
    # Defaults reason: the spec calls for `hindsight run` to stream the bundled
    # synthetic Binance slice without extra setup. These point at that explicit on-disk
    # dataset and output location; user-supplied flags override them.
    run_parser.add_argument(
        "--lake-root",
        type=Path,
        default=Path("examples/sample_data/binance_usdm"),
    )
    run_parser.add_argument("--output-dir", type=Path, default=Path("reports/hindsight"))
    run_parser.add_argument("--symbol", default="BTCUSDT")
    run_parser.add_argument("--date", default=None)
    run_parser.add_argument("--limit", type=int, default=1440)
    compare_parser = subparsers.add_parser("compare")
    compare_parser.add_argument(
        "--lake-root",
        type=Path,
        default=Path("examples/sample_data/binance_usdm"),
    )
    compare_parser.add_argument("--output-dir", type=Path, default=Path("reports/hindsight"))
    compare_parser.add_argument("--symbol", default="BTCUSDT")
    compare_parser.add_argument("--date", default=None)
    compare_parser.add_argument("--limit", type=int, default=1440)
    compare_parser.add_argument("--quantity", type=float, default=0.01)
    compare_parser.add_argument("--lookback-bars", type=int, default=2)
    compare_parser.add_argument("--threshold-bps", type=float, default=1.0)
    benchmark_parser = subparsers.add_parser("benchmark")
    benchmark_parser.add_argument(
        "--lake-root",
        type=Path,
        default=Path("examples/sample_data/hyperliquid"),
    )
    benchmark_parser.add_argument("--output-dir", type=Path, default=Path("reports/hindsight"))
    benchmark_parser.add_argument("--symbol", default="SOL-PERP")
    benchmark_parser.add_argument("--date", default="20260101")
    benchmark_parser.add_argument("--limit", type=int, default=120)
    benchmark_parser.add_argument("--horizon", default="10s")
    benchmark_parser.add_argument("--label-horizon-seconds", type=float, default=10.0)
    benchmark_parser.add_argument("--n-folds", type=int, default=2)
    benchmark_parser.add_argument("--train-window", type=int, default=4)
    benchmark_parser.add_argument("--test-window", type=int, default=2)
    benchmark_parser.add_argument("--purge-seconds", type=float, default=0.0)
    benchmark_parser.add_argument("--embargo-seconds", type=float, default=0.0)
    benchmark_parser.add_argument("--include-leaky", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "compare":
        comparison_artifacts = run_comparison(
            lake_root=args.lake_root,
            output_dir=args.output_dir,
            symbol=args.symbol,
            date=args.date,
            limit=args.limit,
            quantity=args.quantity,
            lookback_bars=args.lookback_bars,
            threshold_bps=args.threshold_bps,
        )
        print(f"Wrote {comparison_artifacts.json_path}")
        print(f"Wrote {comparison_artifacts.markdown_path}")
        if comparison_artifacts.png_path is not None:
            print(f"Wrote {comparison_artifacts.png_path}")
        return 0
    if args.command == "benchmark":
        benchmark_artifacts = run_benchmark(
            lake_root=args.lake_root,
            output_dir=args.output_dir,
            symbol=args.symbol,
            date=args.date,
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


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
