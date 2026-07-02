"""Artifact-backed offline demo for Hindsight."""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

from hindsight import __version__
from hindsight.core.hashing import run_hash, stable_hash
from hindsight.data.hyperliquid_adapter import HyperliquidLakeAdapter, HyperliquidMarkoutRecord
from hindsight.demo.naive import naive_random_folds
from hindsight.evaluation.benchmark import (
    BenchmarkResult,
    BenchmarkRow,
    benchmark_quote_policies,
    label_intervals,
    leaky_markout_policy,
    maker_side_policy,
    quote_all_policy,
)
from hindsight.evaluation.leakage import LeakageError
from hindsight.evaluation.walk_forward import purged_walk_forward_folds
from hindsight.reporting.leaderboard import write_leaderboard_csv
from hindsight.reporting.manifest import RunManifest, current_git_sha

DEMO_WARNING = "deliberately unsafe control - do not use"


@dataclass(frozen=True, slots=True)
class DemoArtifacts:
    """Paths produced by `hindsight demo`."""

    json_path: Path
    markdown_path: Path
    manifest_path: Path
    naive_csv_path: Path
    honest_csv_path: Path
    manifest: RunManifest


def run_demo(
    *,
    sample_root: Path,
    output_dir: Path,
    repo_root: Path,
    symbol: str = "SOL-PERP",
    date: str = "20260101",
    limit: int = 8,
    horizon: str = "10s",
    label_horizon_seconds: float = 10.0,
    seed: int = 0,
) -> DemoArtifacts:
    """Run the offline leakage demo and write reproducible artifacts."""
    adapter = HyperliquidLakeAdapter(sample_root)
    try:
        records = adapter.load_markouts(symbol=symbol, date=date, limit=limit)
    except FileNotFoundError as exc:
        raise FileNotFoundError(
            f"No Hyperliquid markout rows loaded for {symbol.upper()}"
        ) from exc
    if not records:
        raise FileNotFoundError(f"No Hyperliquid markout rows loaded for {symbol.upper()}")

    intervals = label_intervals(records, horizon=timedelta(seconds=label_horizon_seconds))
    naive_folds = naive_random_folds(intervals, n_folds=2, test_window=2, seed=seed)
    honest_folds = purged_walk_forward_folds(
        intervals,
        n_folds=2,
        train_window=4,
        test_window=2,
        purge=timedelta(seconds=label_horizon_seconds),
        embargo=timedelta(seconds=label_horizon_seconds),
    )

    baseline = quote_all_policy(len(records))
    clean_policy = maker_side_policy(records)
    leaky_policy = leaky_markout_policy(records, horizon_key=horizon)
    target_name = f"markout_bps_{horizon}"

    naive_result = benchmark_quote_policies(
        records=records,
        folds=naive_folds,
        policies=(baseline, clean_policy, leaky_policy),
        baseline_policy_name=baseline.name,
        horizon_key=horizon,
        target_name=target_name,
        fail_on_leakage=False,
    )

    blocked_error = _blocked_leaky_message(
        records=records,
        folds=honest_folds,
        policies=(baseline, clean_policy, leaky_policy),
        baseline_policy_name=baseline.name,
        horizon_key=horizon,
        target_name=target_name,
    )

    honest_result = benchmark_quote_policies(
        records=records,
        folds=honest_folds,
        policies=(baseline, clean_policy),
        baseline_policy_name=baseline.name,
        horizon_key=horizon,
        target_name=target_name,
        fail_on_leakage=True,
    )

    data_content_hash = run_hash([_record_payload(record) for record in records])
    config_hash = stable_hash(
        {
            "command": "hindsight demo",
            "sample_root": str(sample_root.as_posix()),
            "symbol": symbol,
            "date": date,
            "limit": limit,
            "horizon": horizon,
            "label_horizon_seconds": label_horizon_seconds,
            "seed": seed,
        }
    )
    manifest = RunManifest.build(
        engine_version=__version__,
        git_sha=current_git_sha(repo_root),
        data_content_hash=data_content_hash,
        config_hash=config_hash,
        seed=seed,
    )

    output_dir.mkdir(parents=True, exist_ok=True)
    naive_csv_path = output_dir / "naive-control-leaderboard.csv"
    honest_csv_path = output_dir / "hindsight-clean-leaderboard.csv"
    json_path = output_dir / "demo.json"
    markdown_path = output_dir / "demo.md"
    manifest_path = output_dir / "manifest.json"

    write_leaderboard_csv(naive_csv_path, naive_result)
    write_leaderboard_csv(honest_csv_path, honest_result)

    payload = _demo_payload(
        sample_root=sample_root,
        output_dir=output_dir,
        symbol=symbol,
        date=date,
        horizon=horizon,
        records=records,
        naive_result=naive_result,
        honest_result=honest_result,
        blocked_error=blocked_error,
        manifest=manifest,
        naive_csv_path=naive_csv_path,
        honest_csv_path=honest_csv_path,
    )
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    markdown_path.write_text(_demo_markdown(payload), encoding="utf-8")
    manifest_path.write_text(manifest.model_dump_json(indent=2) + "\n", encoding="utf-8")

    return DemoArtifacts(
        json_path=json_path,
        markdown_path=markdown_path,
        manifest_path=manifest_path,
        naive_csv_path=naive_csv_path,
        honest_csv_path=honest_csv_path,
        manifest=manifest,
    )


def _blocked_leaky_message(
    *,
    records: list[HyperliquidMarkoutRecord],
    folds: tuple[Any, ...],
    policies: tuple[Any, ...],
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
    raise AssertionError("leaky policy was expected to fail the leakage audit")


def _demo_payload(
    *,
    sample_root: Path,
    output_dir: Path,
    symbol: str,
    date: str,
    horizon: str,
    records: list[HyperliquidMarkoutRecord],
    naive_result: BenchmarkResult,
    honest_result: BenchmarkResult,
    blocked_error: str,
    manifest: RunManifest,
    naive_csv_path: Path,
    honest_csv_path: Path,
) -> dict[str, Any]:
    naive_summary = _policy_summary(naive_result)
    honest_summary = _policy_summary(honest_result)
    naive_winner = max(
        naive_summary,
        key=lambda row: (row["average_markout_bps"], row["policy_name"]),
    )
    return {
        "schema_version": "1.0.0",
        "title": "The backtester that catches you lying to yourself",
        "sample_data": {
            "root": str(sample_root.as_posix()),
            "label": "synthetic",
            "symbol": symbol,
            "date": date,
            "rows": len(records),
            "horizon": horizon,
        },
        "naive_control": {
            "warning": DEMO_WARNING,
            "benchmark_hash": naive_result.run_hash,
            "pbo": naive_result.pbo,
            "winner_policy": naive_winner["policy_name"],
            "summary": naive_summary,
            "rows": [_row_payload(row) for row in naive_result.rows],
        },
        "hindsight_audit": {
            "leaky_policy_status": "blocked",
            "blocked_policy": "leaky",
            "error": blocked_error,
        },
        "honest_benchmark": {
            "benchmark_hash": honest_result.run_hash,
            "pbo": honest_result.pbo,
            "summary": honest_summary,
            "rows": [_row_payload(row) for row in honest_result.rows],
        },
        "manifest": manifest.model_dump(mode="json"),
        "artifacts": {
            "demo_json": _artifact_name(output_dir, output_dir / "demo.json"),
            "demo_markdown": _artifact_name(output_dir, output_dir / "demo.md"),
            "manifest": _artifact_name(output_dir, output_dir / "manifest.json"),
            "naive_control_csv": _artifact_name(output_dir, naive_csv_path),
            "hindsight_clean_csv": _artifact_name(output_dir, honest_csv_path),
        },
    }


def _policy_summary(result: BenchmarkResult) -> list[dict[str, Any]]:
    grouped: dict[str, list[BenchmarkRow]] = defaultdict(list)
    for row in result.rows:
        grouped[row.policy_name].append(row)
    summary: list[dict[str, Any]] = []
    for policy_name, rows in sorted(grouped.items()):
        summary.append(
            {
                "policy_name": policy_name,
                "folds": len(rows),
                "average_markout_bps": _mean(row.average_markout_bps for row in rows),
                "average_markout_lift_bps": _mean(row.markout_lift_bps for row in rows),
                "average_quote_rate": _mean(row.quote_rate for row in rows),
                "deflated_sharpe_probability": rows[0].deflated_sharpe_probability,
            }
        )
    return summary


def _row_payload(row: BenchmarkRow) -> dict[str, Any]:
    return {
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


def _record_payload(record: HyperliquidMarkoutRecord) -> dict[str, Any]:
    return {
        "symbol": record.symbol,
        "timestamp": record.timestamp.isoformat(),
        "price": record.price,
        "quantity": record.quantity,
        "side": str(record.side),
        "maker_side": record.maker_side,
        "trade_id": record.trade_id,
        "markout_bps": dict(sorted(record.markout_bps.items())),
    }


def _demo_markdown(payload: dict[str, Any]) -> str:
    naive = payload["naive_control"]
    audit = payload["hindsight_audit"]
    honest = payload["honest_benchmark"]
    sample = payload["sample_data"]
    lines = [
        "# Hindsight Demo",
        "",
        "The backtester that catches you lying to yourself.",
        "",
        "## Sample Data",
        "",
        f"- Label: {sample['label']}",
        f"- Symbol: {sample['symbol']}",
        f"- Date: {sample['date']}",
        f"- Rows: {sample['rows']}",
        f"- Horizon: {sample['horizon']}",
        "",
        "## Result",
        "",
        f"- Naive control winner: `{naive['winner_policy']}`",
        f"- Naive control warning: {naive['warning']}",
        f"- Hindsight audit: `{audit['leaky_policy_status']}` `{audit['blocked_policy']}`",
        f"- Audit error: `{audit['error']}`",
        f"- Honest benchmark hash: `{honest['benchmark_hash']}`",
        f"- Naive control PBO: `{naive['pbo']}`",
        f"- Honest benchmark PBO: `{honest['pbo']}`",
        f"- Manifest run id: `{payload['manifest']['run_id']}`",
        "",
        "## Naive Control",
        "",
        *_summary_table(naive["summary"]),
        "",
        "## Hindsight Clean Benchmark",
        "",
        *_summary_table(honest["summary"]),
        "",
        "## Artifacts",
        "",
        *[f"- `{name}`: `{path}`" for name, path in payload["artifacts"].items()],
        "",
    ]
    return "\n".join(lines)


def _summary_table(rows: list[dict[str, Any]]) -> list[str]:
    lines = [
        "| Policy | Avg markout bps | Avg lift bps | Quote rate | Deflated Sharpe probability |",
        "| --- | ---: | ---: | ---: | --- |",
    ]
    for row in rows:
        lines.append(
            "| {policy_name} | {average_markout_bps:.6g} | "
            "{average_markout_lift_bps:.6g} | {average_quote_rate:.6g} | "
            "{deflated_sharpe_probability} |".format(**row)
        )
    return lines


def _mean(values: Iterable[float]) -> float:
    items = list(values)
    if not items:
        raise ValueError("cannot average an empty sequence")
    return sum(items) / len(items)


def _artifact_name(output_dir: Path, path: Path) -> str:
    try:
        return path.relative_to(output_dir).as_posix()
    except ValueError:
        return path.as_posix()
