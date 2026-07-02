from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from hindsight.cli import main, run_benchmark
from hindsight.core.events import Side
from hindsight.data.hyperliquid_adapter import HyperliquidMarkoutRecord
from hindsight.data.lake import HyperliquidLakeLayout, write_parquet_records
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

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def record(index: int, markout: float, maker_side: int) -> HyperliquidMarkoutRecord:
    return HyperliquidMarkoutRecord(
        symbol="SOL-PERP",
        timestamp=NOW + timedelta(seconds=index * 20),
        price=100 + index,
        quantity=1,
        side=Side.BUY if maker_side > 0 else Side.SELL,
        maker_side=maker_side,
        trade_id=index,
        markout_bps={"10s": markout},
    )


def records() -> list[HyperliquidMarkoutRecord]:
    return [
        record(0, -4, 1),
        record(1, 2, -1),
        record(2, -3, 1),
        record(3, 3, -1),
        record(4, -2, 1),
        record(5, 4, -1),
        record(6, -1, 1),
        record(7, 5, -1),
    ]


def folds_for(records_: list[HyperliquidMarkoutRecord]):
    return purged_walk_forward_folds(
        label_intervals(records_, horizon=timedelta(seconds=10)),
        n_folds=2,
        train_window=4,
        test_window=2,
        purge=timedelta(0),
        embargo=timedelta(0),
    )


def test_benchmark_leaderboard_is_reproducible(tmp_path: Path) -> None:
    rows = records()
    result = benchmark_quote_policies(
        records=rows,
        folds=folds_for(rows),
        policies=(quote_all_policy(len(rows)), maker_side_policy(rows)),
        baseline_policy_name="ofi_quote",
        horizon_key="10s",
        target_name="markout_bps_10s",
        fail_on_leakage=True,
    )
    first = tmp_path / "first.csv"
    second = tmp_path / "second.csv"

    write_leaderboard_csv(first, result)
    write_leaderboard_csv(second, result)

    assert first.read_bytes() == second.read_bytes()
    assert result.run_hash == benchmark_quote_policies(
        records=rows,
        folds=folds_for(rows),
        policies=(quote_all_policy(len(rows)), maker_side_policy(rows)),
        baseline_policy_name="ofi_quote",
        horizon_key="10s",
        target_name="markout_bps_10s",
        fail_on_leakage=True,
    ).run_hash
    text = first.read_text(encoding="utf-8")
    assert "markout_lift_bps" in text
    assert "deflated_sharpe_probability" in text
    assert "run_pbo" in text
    assert result.pbo == "n/a (requires >=4 trials)"


def test_leaderboard_formats_numeric_diagnostics(tmp_path: Path) -> None:
    result = BenchmarkResult(
        rows=(
            BenchmarkRow(
                fold_id=0,
                policy_name="catboost",
                rows=2,
                quote_rate=0.5,
                average_markout_bps=1.0,
                baseline_average_markout_bps=0.25,
                markout_lift_bps=0.75,
                markout_lift_ci_lower_bps=0.1,
                markout_lift_ci_upper_bps=1.4,
                deflated_sharpe_probability=0.1234567890123,
            ),
        ),
        pbo=0.25,
        run_hash="hash",
    )
    path = tmp_path / "leaderboard.csv"

    write_leaderboard_csv(path, result)

    text = path.read_text(encoding="utf-8")
    assert "0.123456789012" in text
    assert ",0.25\n" in text


def test_leaky_policy_fails_benchmark_run() -> None:
    rows = records()

    with pytest.raises(LeakageError, match="leaky"):
        benchmark_quote_policies(
            records=rows,
            folds=folds_for(rows),
            policies=(
                quote_all_policy(len(rows)),
                leaky_markout_policy(rows, horizon_key="10s"),
            ),
            baseline_policy_name="ofi_quote",
            horizon_key="10s",
            target_name="markout_bps_10s",
            fail_on_leakage=True,
        )


def test_benchmark_cli_writes_leaderboard(tmp_path: Path) -> None:
    layout = HyperliquidLakeLayout(tmp_path / "lake")
    write_parquet_records(
        layout.gold_markout_path("SOL", "20260101"),
        [
            {
                "coin": "SOL",
                "ts_ms": int((NOW + timedelta(seconds=index * 20)).timestamp() * 1000),
                "px": 100.0 + index,
                "sz": 1.0,
                "side": "B" if index % 2 == 0 else "A",
                "crossed": False,
                "maker_side": 1 if index % 2 == 0 else -1,
                "oid": index,
                "tid": index,
                "markout_bps_10s": float(index - 3),
                "toxic_10s": index < 3,
            }
            for index in range(8)
        ],
    )
    output = tmp_path / "out"

    exit_code = main([
        "benchmark",
        "--lake-root",
        str(tmp_path / "lake"),
        "--output-dir",
        str(output),
        "--symbol",
        "SOL-PERP",
        "--date",
        "20260101",
        "--limit",
        "8",
        "--train-window",
        "4",
        "--test-window",
        "2",
        "--n-folds",
        "2",
    ])

    assert exit_code == 0
    assert (output / "hindsight-leaderboard.csv").exists()
    leakage = json.loads((output / "leakage.json").read_text(encoding="utf-8"))
    assert leakage["verdict"] == "pass"
    assert all(audit["verdict"] == "pass" for audit in leakage["audits"])
    with pytest.raises(LeakageError):
        run_benchmark(
            lake_root=tmp_path / "lake",
            output_dir=output,
            symbol="SOL-PERP",
            date="20260101",
            limit=8,
            horizon="10s",
            label_horizon_seconds=10,
            n_folds=2,
            train_window=4,
            test_window=2,
            purge_seconds=0,
            embargo_seconds=0,
            include_leaky=True,
        )
    leakage = json.loads((output / "leakage.json").read_text(encoding="utf-8"))
    assert leakage["verdict"] == "fail"
    leaky_audit = next(audit for audit in leakage["audits"] if audit["policy_name"] == "leaky")
    clean_audit = next(audit for audit in leakage["audits"] if audit["policy_name"] == "ofi_quote")
    assert leaky_audit["violations"][0]["probe"] == "target_leakage"
    assert leaky_audit["violations"][0]["severity"] == "hard"
    assert leaky_audit["violations"][0]["offending_features"] == ["markout_bps_10s"]
    assert clean_audit["violations"] == []
