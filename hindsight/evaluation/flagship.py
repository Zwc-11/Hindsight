"""Phase 0 flagship benchmark readiness and artifact generation."""

from __future__ import annotations

import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from hashlib import sha256
from math import isfinite
from pathlib import Path
from typing import Any, cast

import pyarrow as pa
import pyarrow.parquet as pq

from hindsight import __version__
from hindsight.core.hashing import stable_hash
from hindsight.data.hyperliquid_adapter import HyperliquidLakeAdapter, HyperliquidMarkoutRecord
from hindsight.data.lake import HyperliquidLakeLayout
from hindsight.evaluation.benchmark import (
    BenchmarkResult,
    QuotePolicy,
    benchmark_quote_policies,
    label_intervals,
    leaky_markout_policy,
    phase0_policy_suite,
)
from hindsight.evaluation.falsification import (
    falsification_payload,
    run_falsification_on_records,
    write_falsification_json,
)
from hindsight.evaluation.walk_forward import purged_walk_forward_folds
from hindsight.reporting.html_report import write_html_report
from hindsight.reporting.leaderboard import write_leaderboard_csv
from hindsight.reporting.leakage_report import quote_policy_leakage_payload, write_leakage_json
from hindsight.reporting.manifest import RunManifest, current_git_sha
from hindsight.reporting.report_v2 import build_report_v2, write_report_v2_json

PHASE0_SCHEMA_VERSION = "1.0.0"
DEFAULT_PHASE0_SYMBOLS = ("SOL-PERP", "ETH-PERP", "BTC-PERP")
MIN_PHASE0_SYMBOLS = 3
MIN_PHASE0_DATES = 30
MIN_PHASE0_POLICIES = 8
MIN_PHASE0_FOLDS = 8


class Phase0CoverageError(RuntimeError):
    """Raised when a local lake does not satisfy the Phase 0 input contract."""


@dataclass(frozen=True, slots=True)
class FlagshipPartition:
    """Artifact-only inventory for one symbol/date partition."""

    symbol: str
    coin: str
    date: str
    markout_path: Path
    markout_rows: int
    markout_sha256: str | None
    evaluated_rows: int
    fills_path: Path | None
    fills_sha256: str | None
    l2_book_path: Path | None
    l2_book_sha256: str | None
    funding_path: Path | None
    funding_sha256: str | None

    @property
    def has_markout(self) -> bool:
        return self.markout_sha256 is not None

    @property
    def has_trades(self) -> bool:
        return self.fills_sha256 is not None

    @property
    def has_top_of_book(self) -> bool:
        return self.l2_book_sha256 is not None

    @property
    def has_funding_series(self) -> bool:
        return self.funding_sha256 is not None


@dataclass(frozen=True, slots=True)
class FlagshipArtifacts:
    """Paths and payload produced by a Phase 0 flagship run."""

    coverage_path: Path
    json_path: Path
    csv_path: Path
    leakage_path: Path
    falsification_path: Path
    report_json_path: Path
    tearsheet_path: Path
    readme_path: Path
    result: BenchmarkResult
    payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class FlagshipCoverageArtifacts:
    """Coverage audit output for a local lake before benchmark execution."""

    coverage_path: Path
    payload: dict[str, Any]


def inventory_lake(
    *,
    lake_root: Path,
    symbols: Sequence[str],
    dates: Sequence[str],
    rows_per_partition: int,
) -> tuple[FlagshipPartition, ...]:
    """Return artifact-only coverage rows for requested symbol/date partitions."""

    if rows_per_partition < 1:
        raise ValueError("rows_per_partition must be positive")
    layout = HyperliquidLakeLayout(lake_root)
    partitions: list[FlagshipPartition] = []
    for symbol in _normalize_symbols(symbols):
        coin = _coin_from_symbol(symbol)
        for date in dates:
            markout_path = layout.gold_markout_path(coin, date)
            fills_path = _first_existing(
                layout.silver_fills_path(coin, date),
                layout.bronze_fills_path(coin, date),
            )
            l2_path = _existing_or_none(layout.silver_l2_book_path(coin, date))
            funding_path = _first_existing(
                layout.silver_funding_path(coin, date),
                layout.silver_asset_ctxs_path(date),
            )
            markout_rows = _parquet_rows(markout_path) if markout_path.is_file() else 0
            partitions.append(
                FlagshipPartition(
                    symbol=symbol,
                    coin=coin,
                    date=date,
                    markout_path=markout_path,
                    markout_rows=markout_rows,
                    markout_sha256=_sha256_or_none(markout_path),
                    evaluated_rows=min(markout_rows, rows_per_partition),
                    fills_path=fills_path,
                    fills_sha256=_sha256_or_none(fills_path),
                    l2_book_path=l2_path,
                    l2_book_sha256=_sha256_or_none(l2_path),
                    funding_path=funding_path,
                    funding_sha256=_sha256_or_none(funding_path),
                )
            )
    return tuple(partitions)


def write_flagship_coverage(
    *,
    lake_root: Path,
    output_dir: Path,
    symbols: Sequence[str],
    dates: Sequence[str],
    rows_per_partition: int,
    min_symbols: int = MIN_PHASE0_SYMBOLS,
    min_dates: int = MIN_PHASE0_DATES,
) -> FlagshipCoverageArtifacts:
    """Write Phase 0 coverage inventory without running the benchmark."""

    partitions = inventory_lake(
        lake_root=lake_root,
        symbols=symbols,
        dates=dates,
        rows_per_partition=rows_per_partition,
    )
    payload = _coverage_payload(
        partitions=partitions,
        requested_symbols=_normalize_symbols(symbols),
        requested_dates=tuple(dates),
        min_symbols=min_symbols,
        min_dates=min_dates,
    )
    coverage_path = output_dir / "coverage.json"
    _write_json(coverage_path, payload)
    return FlagshipCoverageArtifacts(coverage_path=coverage_path, payload=payload)


def run_flagship_benchmark(
    *,
    lake_root: Path,
    output_dir: Path,
    symbols: Sequence[str],
    dates: Sequence[str],
    rows_per_partition: int,
    horizon: str,
    label_horizon_seconds: float,
    n_folds: int,
    train_window: int,
    test_window: int,
    purge_seconds: float,
    embargo_seconds: float,
    min_symbols: int = MIN_PHASE0_SYMBOLS,
    min_dates: int = MIN_PHASE0_DATES,
) -> FlagshipArtifacts:
    """Run the Phase 0 flagship benchmark and write artifact-only outputs."""

    if label_horizon_seconds <= 0:
        raise ValueError("label_horizon_seconds must be positive")
    partitions = inventory_lake(
        lake_root=lake_root,
        symbols=symbols,
        dates=dates,
        rows_per_partition=rows_per_partition,
    )
    coverage = _coverage_payload(
        partitions=partitions,
        requested_symbols=_normalize_symbols(symbols),
        requested_dates=tuple(dates),
        min_symbols=min_symbols,
        min_dates=min_dates,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    coverage_path = output_dir / "coverage.json"
    _write_json(coverage_path, coverage)
    if not coverage["coverage"]["ready"]:
        raise Phase0CoverageError(_coverage_error_message(coverage, coverage_path))

    records = _load_records(
        lake_root=lake_root,
        partitions=partitions,
        rows_per_partition=rows_per_partition,
    )
    records = _records_with_horizon(records, horizon=horizon)
    folds = purged_walk_forward_folds(
        label_intervals(records, horizon=timedelta(seconds=label_horizon_seconds)),
        n_folds=n_folds,
        train_window=train_window,
        test_window=test_window,
        purge=timedelta(seconds=purge_seconds),
        embargo=timedelta(seconds=embargo_seconds),
    )
    policies = phase0_policy_suite(records)
    leaky_policy = leaky_markout_policy(records, horizon_key=horizon)
    leakage_payload = quote_policy_leakage_payload(
        policies=(*policies, leaky_policy),
        target_name=f"markout_bps_{horizon}",
    )
    result = benchmark_quote_policies(
        records=records,
        folds=folds,
        policies=policies,
        baseline_policy_name="ofi_quote",
        horizon_key=horizon,
        target_name=f"markout_bps_{horizon}",
        fail_on_leakage=True,
    )
    naive_result = benchmark_quote_policies(
        records=records,
        folds=folds,
        policies=(*policies, leaky_policy),
        baseline_policy_name="ofi_quote",
        horizon_key=horizon,
        target_name=f"markout_bps_{horizon}",
        fail_on_leakage=False,
    )
    acceptance = _acceptance_payload(
        coverage=coverage,
        result=result,
        policy_count=len(policies),
        fold_count=len(folds),
    )
    git_sha = current_git_sha(Path.cwd())
    cost_model = _flagship_cost_model(
        lake_root=lake_root,
        symbols=symbols,
        dates=dates,
    )
    payload: dict[str, Any] = {
        "schema_version": PHASE0_SCHEMA_VERSION,
        "data_label": "[local real Hyperliquid lake]",
        "hindsight_git_sha": git_sha,
        "lake_root": str(lake_root),
        "symbols": list(_normalize_symbols(symbols)),
        "dates": list(dates),
        "horizon": horizon,
        "rows_per_partition": rows_per_partition,
        "fold_config": {
            "n_folds": n_folds,
            "train_window": train_window,
            "test_window": test_window,
            "purge_seconds": purge_seconds,
            "embargo_seconds": embargo_seconds,
        },
        "coverage": coverage["coverage"],
        "sources": coverage["partitions"],
        "acceptance": acceptance,
        "benchmark": {
            "run_hash": result.run_hash,
            "pbo": result.pbo,
            "rows": _benchmark_rows_payload(result),
        },
    }
    payload["artifact_hash"] = stable_hash(payload)

    json_path = output_dir / "flagship.json"
    csv_path = output_dir / "flagship-leaderboard.csv"
    leakage_path = output_dir / "leakage.json"
    falsification_path = output_dir / "falsification.json"
    report_json_path = output_dir / "report.json"
    tearsheet_path = output_dir / "tearsheet.html"
    readme_path = output_dir / "README.md"
    config_hash = stable_hash(
        {
            "command": "flagship",
            "lake_root": str(lake_root),
            "symbols": list(_normalize_symbols(symbols)),
            "dates": list(dates),
            "horizon": horizon,
            "label_horizon_seconds": label_horizon_seconds,
            "rows_per_partition": rows_per_partition,
            "fold_config": payload["fold_config"],
            "cost_model": cost_model,
        }
    )
    manifest = RunManifest.build(
        engine_version=__version__,
        git_sha=git_sha,
        data_content_hash=coverage["coverage"]["source_hash"],
        config_hash=config_hash,
        seed=0,
    )
    falsification_result = run_falsification_on_records(
        records,
        horizon=horizon,
        label_horizon_seconds=label_horizon_seconds,
        max_records=256,
    )
    falsification = falsification_payload(falsification_result)
    report = build_report_v2(
        sample_root=lake_root,
        symbol="/".join(symbol.removesuffix("-PERP") for symbol in _normalize_symbols(symbols)),
        date=f"{dates[0]}..{dates[-1]}",
        horizon=horizon,
        data_label=payload["data_label"],
        records=records,
        baseline_policy=policies[0],
        clean_policy=_top_audited_policy(policies, result),
        leaky_policy=leaky_policy,
        naive_result=naive_result,
        honest_result=result,
        blocked_error=f"policy {leaky_policy.name} failed leakage probe",
        leakage_payload=leakage_payload,
        manifest=manifest,
        cost_model=cost_model,
        reproduce_command=_reproduce_command(
            lake_root=lake_root,
            output_dir=output_dir,
            symbols=symbols,
            dates=dates,
            rows_per_partition=rows_per_partition,
            horizon=horizon,
            label_horizon_seconds=label_horizon_seconds,
            n_folds=n_folds,
            train_window=train_window,
            test_window=test_window,
            purge_seconds=purge_seconds,
            embargo_seconds=embargo_seconds,
        ),
        pinned_versions=_pinned_versions(),
        falsification=falsification,
        curve_point_limit=1200,
    )
    _write_json(json_path, payload)
    write_leaderboard_csv(csv_path, result)
    write_leakage_json(leakage_path, leakage_payload)
    write_falsification_json(falsification_path, falsification_result)
    write_report_v2_json(report_json_path, report)
    write_html_report(tearsheet_path, report)
    readme_path.write_text(_readme(payload), encoding="utf-8")
    return FlagshipArtifacts(
        coverage_path=coverage_path,
        json_path=json_path,
        csv_path=csv_path,
        leakage_path=leakage_path,
        falsification_path=falsification_path,
        report_json_path=report_json_path,
        tearsheet_path=tearsheet_path,
        readme_path=readme_path,
        result=result,
        payload=payload,
    )


def _coverage_payload(
    *,
    partitions: Sequence[FlagshipPartition],
    requested_symbols: tuple[str, ...],
    requested_dates: tuple[str, ...],
    min_symbols: int,
    min_dates: int,
) -> dict[str, Any]:
    missing_markout = [p for p in partitions if not p.has_markout]
    missing_trades = [p for p in partitions if not p.has_trades]
    missing_l2 = [p for p in partitions if not p.has_top_of_book]
    missing_funding = [p for p in partitions if not p.has_funding_series]
    available_symbols = sorted({p.symbol for p in partitions if p.has_markout})
    available_dates = sorted({p.date for p in partitions if p.has_markout})
    failures: list[str] = []
    if len(available_symbols) < min_symbols:
        failures.append(f"requires >= {min_symbols} symbols with markout partitions")
    if len(available_dates) < min_dates:
        failures.append(f"requires >= {min_dates} dates with markout partitions")
    if missing_markout:
        failures.append("missing gold markout partitions")
    if missing_trades:
        failures.append("missing trade/fill partitions")
    if missing_l2:
        failures.append("missing top-of-book l2_book partitions")
    if missing_funding:
        failures.append("missing recorded funding/asset_ctxs series")
    return {
        "schema_version": PHASE0_SCHEMA_VERSION,
        "coverage": {
            "ready": not failures,
            "failures": failures,
            "requested_symbols": list(requested_symbols),
            "requested_dates": list(requested_dates),
            "available_symbols": available_symbols,
            "available_dates": available_dates,
            "partition_count": len(partitions),
            "markout_partition_count": sum(1 for p in partitions if p.has_markout),
            "trade_partition_count": sum(1 for p in partitions if p.has_trades),
            "top_of_book_partition_count": sum(
                1 for p in partitions if p.has_top_of_book
            ),
            "funding_partition_count": sum(
                1 for p in partitions if p.has_funding_series
            ),
            "source_hash": stable_hash([_partition_payload(p) for p in partitions]),
        },
        "partitions": [_partition_payload(p) for p in partitions],
    }


def _acceptance_payload(
    *,
    coverage: dict[str, Any],
    result: BenchmarkResult,
    policy_count: int,
    fold_count: int,
) -> dict[str, Any]:
    dsr_real_policies = sorted({
        row.policy_name
        for row in result.rows
        if isinstance(row.deflated_sharpe_probability, float)
    })
    pbo_real = isinstance(result.pbo, float)
    fold_matrix_cells = policy_count * fold_count
    failures: list[str] = list(coverage["coverage"]["failures"])
    if policy_count < MIN_PHASE0_POLICIES:
        failures.append(f"requires >= {MIN_PHASE0_POLICIES} policy trials")
    if fold_count < MIN_PHASE0_FOLDS:
        failures.append(f"requires >= {MIN_PHASE0_FOLDS} folds")
    if fold_matrix_cells < MIN_PHASE0_POLICIES * MIN_PHASE0_FOLDS:
        failures.append("fold matrix is smaller than 8x8")
    if not pbo_real:
        failures.append("PBO is not numeric")
    if len(dsr_real_policies) < policy_count - 1:
        failures.append("DSR is not numeric for enough non-baseline policies")
    return {
        "passed": not failures,
        "failures": failures,
        "pbo_real": pbo_real,
        "dsr_real_policy_count": len(dsr_real_policies),
        "dsr_real_policies": dsr_real_policies,
        "policy_count": policy_count,
        "fold_count": fold_count,
        "fold_matrix_cells": fold_matrix_cells,
        "min_required_fold_matrix_cells": MIN_PHASE0_POLICIES * MIN_PHASE0_FOLDS,
    }


def _load_records(
    *,
    lake_root: Path,
    partitions: Sequence[FlagshipPartition],
    rows_per_partition: int,
) -> list[HyperliquidMarkoutRecord]:
    adapter = HyperliquidLakeAdapter(lake_root)
    records: list[HyperliquidMarkoutRecord] = []
    for partition in partitions:
        if not partition.has_markout:
            continue
        records.extend(
            adapter.load_markouts(
                symbol=partition.symbol,
                date=partition.date,
                limit=min(rows_per_partition, partition.markout_rows),
            )
        )
    if not records:
        raise FileNotFoundError("No Hyperliquid markout rows loaded for flagship run")
    return sorted(
        records,
        key=lambda record: (record.timestamp, record.symbol, record.trade_id or -1),
    )


def _records_with_horizon(
    records: Sequence[HyperliquidMarkoutRecord],
    *,
    horizon: str,
) -> list[HyperliquidMarkoutRecord]:
    filtered = [record for record in records if horizon in record.markout_bps]
    if not filtered:
        raise ValueError(f"No Hyperliquid markout rows contain horizon {horizon}")
    return filtered


def _benchmark_rows_payload(result: BenchmarkResult) -> list[dict[str, Any]]:
    return [
        {
            "fold_id": row.fold_id,
            "policy_name": row.policy_name,
            "rows": row.rows,
            "quote_rate": row.quote_rate,
            "average_markout_bps": row.average_markout_bps,
            "baseline_average_markout_bps": row.baseline_average_markout_bps,
            "markout_lift_bps": row.markout_lift_bps,
            "markout_lift_ci_lower_bps": row.markout_lift_ci_lower_bps,
            "markout_lift_ci_upper_bps": row.markout_lift_ci_upper_bps,
            "deflated_sharpe_probability": row.deflated_sharpe_probability,
        }
        for row in result.rows
    ]


def _top_audited_policy(
    policies: Sequence[QuotePolicy],
    result: BenchmarkResult,
) -> QuotePolicy:
    lift_by_policy: dict[str, list[float]] = {}
    for row in result.rows:
        lift_by_policy.setdefault(row.policy_name, []).append(row.markout_lift_bps)
    ranked = sorted(
        lift_by_policy.items(),
        key=lambda item: (-sum(item[1]) / len(item[1]), item[0]),
    )
    policy_by_name = {policy.name: policy for policy in policies}
    for policy_name, _scores in ranked:
        if policy_name != "ofi_quote":
            return policy_by_name[policy_name]
    return policies[0]


def _flagship_cost_model(
    *,
    lake_root: Path,
    symbols: Sequence[str],
    dates: Sequence[str],
) -> dict[str, Any]:
    funding_rate_bps = _average_funding_bps(
        lake_root=lake_root,
        symbols=symbols,
        dates=dates,
    )
    return {
        "maker_fee_bps": 1.5,
        "taker_fee_bps": 4.5,
        "slippage_impact_bps": 0.5,
        "latency_ms": 50,
        "funding_rate_bps": funding_rate_bps,
        "funding_interval_hours": 8,
        "participation_cap": 0.1,
        "liquidity_assumption": "maker",
        "fee_tier_note": (
            "Hyperliquid base perps tier; funding_rate_bps is the mean recorded "
            "funding over the requested symbols and dates."
        ),
    }


def _average_funding_bps(
    *,
    lake_root: Path,
    symbols: Sequence[str],
    dates: Sequence[str],
) -> float:
    layout = HyperliquidLakeLayout(lake_root)
    values: list[float] = []
    for date in dates:
        for symbol in _normalize_symbols(symbols):
            coin = _coin_from_symbol(symbol)
            funding_path = layout.silver_funding_path(coin, date)
            asset_ctxs_path = layout.silver_asset_ctxs_path(date)
            if funding_path.is_file():
                values.extend(_funding_values_from_path(funding_path, coin=coin))
            elif asset_ctxs_path.is_file():
                values.extend(_funding_values_from_path(asset_ctxs_path, coin=coin))
    if not values:
        return 0.0
    return sum(values) / len(values)


def _funding_values_from_path(path: Path, *, coin: str) -> list[float]:
    table = pq.read_table(path)  # type: ignore[no-untyped-call]
    values: list[float] = []
    for raw_row in table.to_pylist():
        row = dict(raw_row)
        if not _funding_row_matches_coin(row, coin):
            continue
        raw = _first_present(row, ("funding", "asset_funding", "funding_rate", "fundingRate"))
        if raw is None:
            continue
        value = float(str(raw))
        if isfinite(value):
            values.append(value * 10_000.0)
    return values


def _funding_row_matches_coin(row: dict[str, Any], coin: str) -> bool:
    for key in ("coin", "symbol", "asset", "name"):
        raw = row.get(key)
        if raw is None:
            continue
        normalized = str(raw).strip().upper().removesuffix("-PERP")
        return normalized == coin
    return True


def _first_present(row: dict[str, Any], keys: Sequence[str]) -> object | None:
    for key in keys:
        value = row.get(key)
        if value is not None:
            return cast(object, value)
    return None


def _reproduce_command(
    *,
    lake_root: Path,
    output_dir: Path,
    symbols: Sequence[str],
    dates: Sequence[str],
    rows_per_partition: int,
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
            "python -m hindsight.cli flagship",
            f"--lake-root {lake_root}",
            f"--output-dir {output_dir}",
            f"--symbols {','.join(_normalize_symbols(symbols))}",
            f"--dates {dates[0]}..{dates[-1]}",
            f"--rows-per-partition {rows_per_partition}",
            f"--horizon {horizon}",
            f"--label-horizon-seconds {label_horizon_seconds:g}",
            f"--n-folds {n_folds}",
            f"--train-window {train_window}",
            f"--test-window {test_window}",
            f"--purge-seconds {purge_seconds:g}",
            f"--embargo-seconds {embargo_seconds:g}",
        ]
    )


def _pinned_versions() -> dict[str, str]:
    return {
        "hindsight": __version__,
        "python": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        "pyarrow": pa.__version__,
    }


def _partition_payload(partition: FlagshipPartition) -> dict[str, Any]:
    return {
        "symbol": partition.symbol,
        "date": partition.date,
        "markout": _source_payload(
            partition.markout_path,
            partition.markout_sha256,
            partition.markout_rows,
        ),
        "evaluated_rows": partition.evaluated_rows,
        "fills": _source_payload(partition.fills_path, partition.fills_sha256, None),
        "l2_book": _source_payload(partition.l2_book_path, partition.l2_book_sha256, None),
        "funding": _source_payload(partition.funding_path, partition.funding_sha256, None),
    }


def _source_payload(path: Path | None, digest: str | None, rows: int | None) -> dict[str, Any]:
    return {
        "path": "" if path is None else str(path),
        "sha256": digest or "",
        "rows": rows,
        "present": digest is not None,
    }


def _readme(payload: dict[str, Any]) -> str:
    acceptance = payload["acceptance"]
    coverage = payload["coverage"]
    failures = "\n".join(f"- `{failure}`" for failure in acceptance["failures"])
    if not failures:
        failures = "- none"
    return "\n".join([
        "# Local Hyperliquid Flagship Phase 0",
        "",
        f"Data label: {payload['data_label']}",
        "",
        "This artifact was produced from local Hyperliquid parquet files. Raw market "
        "data is not committed; this directory stores hashes, row counts, and "
        "benchmark outputs only.",
        "",
        "## Coverage",
        "",
        f"- Symbols: `{', '.join(coverage['available_symbols'])}`",
        f"- Dates: `{len(coverage['available_dates'])}`",
        f"- Markout partitions: `{coverage['markout_partition_count']}`",
        f"- Trade partitions: `{coverage['trade_partition_count']}`",
        f"- Top-of-book partitions: `{coverage['top_of_book_partition_count']}`",
        f"- Funding partitions: `{coverage['funding_partition_count']}`",
        "",
        "## Acceptance",
        "",
        f"- Passed: `{acceptance['passed']}`",
        f"- Policies: `{acceptance['policy_count']}`",
        f"- Folds: `{acceptance['fold_count']}`",
        f"- Fold matrix cells: `{acceptance['fold_matrix_cells']}`",
        f"- Numeric PBO: `{acceptance['pbo_real']}`",
        f"- Numeric DSR policies: `{acceptance['dsr_real_policy_count']}`",
        "",
        "Failures:",
        "",
        failures,
        "",
        "## Artifacts",
        "",
        "- [coverage.json](coverage.json)",
        "- [flagship.json](flagship.json)",
        "- [flagship-leaderboard.csv](flagship-leaderboard.csv)",
        "- [leakage.json](leakage.json)",
        "- [falsification.json](falsification.json)",
        "- [report.json](report.json)",
        "- [tearsheet.html](tearsheet.html)",
        "",
    ])


def _coverage_error_message(coverage: dict[str, Any], path: Path) -> str:
    failures = "; ".join(coverage["coverage"]["failures"])
    return f"Phase 0 coverage is incomplete: {failures}. Wrote {path}"


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, sort_keys=True, indent=2, default=str) + "\n",
        encoding="utf-8",
    )


def _normalize_symbols(symbols: Sequence[str]) -> tuple[str, ...]:
    normalized = tuple(dict.fromkeys(_perp_symbol(symbol) for symbol in symbols))
    if not normalized:
        raise ValueError("at least one symbol is required")
    return normalized


def _coin_from_symbol(symbol: str) -> str:
    cleaned = symbol.strip().upper()
    if not cleaned:
        raise ValueError("symbol cannot be empty")
    return cleaned.removesuffix("-PERP")


def _perp_symbol(symbol: str) -> str:
    return f"{_coin_from_symbol(symbol)}-PERP"


def _first_existing(*paths: Path) -> Path | None:
    for path in paths:
        if path.is_file():
            return path
    return None


def _existing_or_none(path: Path) -> Path | None:
    return path if path.is_file() else None


def _sha256_or_none(path: Path | None) -> str | None:
    if path is None or not path.is_file():
        return None
    return _sha256(path)


def _sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _parquet_rows(path: Path) -> int:
    parquet = pq.ParquetFile(path)  # type: ignore[no-untyped-call]
    metadata = parquet.metadata
    if metadata is None:
        return 0
    return int(metadata.num_rows)
