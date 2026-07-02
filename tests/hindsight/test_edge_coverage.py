from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from hindsight.core.events import Side
from hindsight.data.hyperliquid_adapter import HyperliquidLakeAdapter, HyperliquidMarkoutRecord
from hindsight.data.lake import HyperliquidLakeLayout
from hindsight.demo.naive import naive_random_folds
from hindsight.demo.runner import run_demo
from hindsight.evaluation.benchmark import (
    benchmark_quote_policies,
    label_intervals,
    quote_all_policy,
)
from hindsight.evaluation.metrics import (
    deflated_sharpe_probability,
    probability_of_backtest_overfit,
)
from hindsight.evaluation.walk_forward import LabelInterval, purged_walk_forward_folds
from hindsight.labels.markout import (
    MakerFill,
    future_mid_at,
    is_toxic,
    markout_bps,
    realized_markout_bps,
)

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def sample_intervals(count: int = 4) -> list[LabelInterval]:
    return [
        LabelInterval(
            index=index,
            start=NOW + timedelta(seconds=index * 20),
            end=NOW + timedelta(seconds=index * 20 + 10),
        )
        for index in range(count)
    ]


def test_markout_helpers_validate_inputs_and_missing_future_mid() -> None:
    assert markout_bps(100, 101, 1) == pytest.approx(100)
    assert is_toxic(-2, fee_bps=1)
    assert future_mid_at(0, 10, [1, 5], [100, 101]) is None
    assert realized_markout_bps(MakerFill(0, 100, 1), [11], [101], 10) == pytest.approx(100)

    with pytest.raises(ValueError, match="fill_price"):
        markout_bps(0, 101, 1)
    with pytest.raises(ValueError, match="side"):
        markout_bps(100, 101, 0)
    with pytest.raises(ValueError, match="same length"):
        future_mid_at(0, 10, [1], [100, 101])


def test_lake_layout_paths_cover_optional_artifact_types(tmp_path: Path) -> None:
    layout = HyperliquidLakeLayout(tmp_path)

    assert layout.bronze_fills_path("sol", "20260101").as_posix().endswith(
        "bronze/hyperliquid/fills/SOL/SOL-20260101.parquet"
    )
    assert layout.silver_l2_book_path("sol", "20260101").as_posix().endswith(
        "silver/hyperliquid/l2_book/SOL/SOL-20260101.parquet"
    )
    assert layout.silver_asset_ctxs_path("20260101").as_posix().endswith(
        "silver/hyperliquid/asset_ctxs/asset-ctxs-20260101.parquet"
    )
    assert layout.silver_candles_path("sol", "1m", "20260101").as_posix().endswith(
        "silver/hyperliquid/candles/1m/SOL/SOL-1m-20260101.parquet"
    )
    assert layout.gold_training_path("sol", "20260101").as_posix().endswith(
        "gold/hyperliquid/training/SOL/SOL-training-20260101.parquet"
    )


def test_hyperliquid_adapter_rejects_bad_arguments() -> None:
    adapter = HyperliquidLakeAdapter(Path("examples/sample_data/hyperliquid"))

    with pytest.raises(ValueError, match="explicit date"):
        list(adapter.stream_events(symbol="SOL-PERP", date=None, limit=1))
    with pytest.raises(ValueError, match="limit"):
        list(adapter.stream_events(symbol="SOL-PERP", date="20260101", limit=0))
    with pytest.raises(ValueError, match="limit"):
        adapter.load_markouts(symbol="SOL-PERP", date="20260101", limit=0)


def test_naive_fold_and_walk_forward_validation_errors() -> None:
    with pytest.raises(ValueError, match="n_folds"):
        naive_random_folds(sample_intervals(), n_folds=0, test_window=1, seed=0)
    with pytest.raises(ValueError, match="test_window"):
        naive_random_folds(sample_intervals(), n_folds=1, test_window=0, seed=0)
    with pytest.raises(ValueError, match="not enough"):
        naive_random_folds(sample_intervals(2), n_folds=2, test_window=1, seed=0)
    duplicate = sample_intervals()[0]
    with pytest.raises(ValueError, match="unique"):
        naive_random_folds(
            [duplicate, duplicate],
            n_folds=1,
            test_window=1,
            seed=0,
        )

    with pytest.raises(ValueError, match="horizon"):
        label_intervals([], horizon=timedelta(0))
    with pytest.raises(ValueError, match="n_folds"):
        purged_walk_forward_folds(
            sample_intervals(),
            n_folds=0,
            train_window=1,
            test_window=1,
            purge=timedelta(0),
            embargo=timedelta(0),
        )


def test_benchmark_and_metric_validation_errors(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="at least one"):
        benchmark_quote_policies(
            records=[],
            folds=(),
            policies=(),
            baseline_policy_name="ofi_quote",
            horizon_key="10s",
            target_name="markout_bps_10s",
            fail_on_leakage=True,
        )
    with pytest.raises(ValueError, match="at least two"):
        probability_of_backtest_overfit([[1.0]])
    with pytest.raises(ValueError, match="trials"):
        deflated_sharpe_probability([0.1, 0.2, 0.3], trials=0)

    with pytest.raises(FileNotFoundError, match="No Hyperliquid"):
        run_demo(sample_root=tmp_path, output_dir=tmp_path / "out", repo_root=Path.cwd())


def test_benchmark_rejects_missing_policy_and_mask_mismatch() -> None:
    records = [
        HyperliquidMarkoutRecord(
            symbol="SOL-PERP",
            timestamp=NOW,
            price=100,
            quantity=1,
            side=Side.BUY,
            maker_side=1,
            trade_id=1,
            markout_bps={"10s": 1.0},
        )
    ]
    folds = purged_walk_forward_folds(
        [
            LabelInterval(0, NOW, NOW + timedelta(seconds=10)),
            LabelInterval(1, NOW + timedelta(seconds=20), NOW + timedelta(seconds=30)),
        ],
        n_folds=1,
        train_window=1,
        test_window=1,
        purge=timedelta(0),
        embargo=timedelta(0),
    )

    with pytest.raises(ValueError, match="baseline"):
        benchmark_quote_policies(
            records=records,
            folds=folds,
            policies=(quote_all_policy(1),),
            baseline_policy_name="missing",
            horizon_key="10s",
            target_name="markout_bps_10s",
            fail_on_leakage=True,
        )
