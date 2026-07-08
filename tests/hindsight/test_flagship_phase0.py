from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from hindsight.cli import main, parse_symbol_spec
from hindsight.core.events import Side
from hindsight.data.hyperliquid_adapter import HyperliquidMarkoutRecord
from hindsight.data.lake import HyperliquidLakeLayout, write_parquet_records
from hindsight.evaluation.benchmark import phase0_policy_suite
from hindsight.evaluation.flagship import (
    DEFAULT_PHASE0_SYMBOLS,
    run_flagship_benchmark,
    write_flagship_coverage,
)

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def test_parse_symbol_spec_normalizes_and_dedupes() -> None:
    assert parse_symbol_spec("sol,ETH-PERP, sol-perp") == ("SOL-PERP", "ETH-PERP")


def test_phase0_policy_suite_has_eight_non_leaky_trials() -> None:
    rows = [
        HyperliquidMarkoutRecord(
            symbol="SOL-PERP",
            timestamp=NOW + timedelta(seconds=index),
            price=100.0 + index,
            quantity=1.0 + index,
            side=Side.BUY if index % 2 == 0 else Side.SELL,
            maker_side=1 if index % 2 == 0 else -1,
            trade_id=index,
            markout_bps={"10s": float(index - 4)},
        )
        for index in range(8)
    ]

    policies = phase0_policy_suite(rows)

    assert len(policies) == 8
    assert len({policy.name for policy in policies}) == 8
    assert all("markout_bps_10s" not in policy.feature_names for policy in policies)
    assert all(len(policy.quote_mask) == len(rows) for policy in policies)


def test_write_flagship_coverage_reports_phase0_gaps(tmp_path: Path) -> None:
    layout = HyperliquidLakeLayout(tmp_path / "lake")
    write_parquet_records(
        layout.gold_markout_path("SOL", "20260101"),
        [_markout_row(symbol_index=0, date_index=0, row_index=0)],
    )

    coverage = write_flagship_coverage(
        lake_root=tmp_path / "lake",
        output_dir=tmp_path / "out",
        symbols=DEFAULT_PHASE0_SYMBOLS,
        dates=("20260101", "20260102"),
        rows_per_partition=1,
    )

    payload = json.loads(coverage.coverage_path.read_text(encoding="utf-8"))
    assert payload == coverage.payload
    assert payload["coverage"]["ready"] is False
    assert "missing gold markout partitions" in payload["coverage"]["failures"]


def test_flagship_benchmark_writes_artifact_only_outputs(tmp_path: Path) -> None:
    lake_root = tmp_path / "lake"
    dates = _dates(30)
    symbols = ("SOL-PERP", "ETH-PERP", "BTC-PERP")
    _write_complete_lake(lake_root, symbols=symbols, dates=dates, rows_per_partition=4)

    artifacts = run_flagship_benchmark(
        lake_root=lake_root,
        output_dir=tmp_path / "out",
        symbols=symbols,
        dates=dates,
        rows_per_partition=4,
        horizon="10s",
        label_horizon_seconds=10.0,
        n_folds=8,
        train_window=32,
        test_window=8,
        purge_seconds=0.0,
        embargo_seconds=0.0,
    )

    assert artifacts.coverage_path.exists()
    assert artifacts.json_path.exists()
    assert artifacts.csv_path.exists()
    assert artifacts.leakage_path.exists()
    assert artifacts.falsification_path.exists()
    assert artifacts.report_json_path.exists()
    assert artifacts.tearsheet_path.exists()
    assert artifacts.readme_path.exists()
    assert artifacts.payload["acceptance"]["passed"] is True
    assert artifacts.payload["acceptance"]["policy_count"] == 8
    assert artifacts.payload["acceptance"]["fold_count"] == 8
    assert artifacts.payload["acceptance"]["fold_matrix_cells"] == 64
    assert isinstance(artifacts.result.pbo, float)
    report = json.loads(artifacts.report_json_path.read_text(encoding="utf-8"))
    assert report["meta"]["report_schema"] == 2
    assert report["sample_data"]["rows"] == 30 * 3 * 4
    assert report["fold_heatmap"]["folds"] == list(range(8))
    assert len(report["fold_heatmap"]["policies"]) == 8
    assert len(report["fold_heatmap"]["cells"]) == 64
    assert report["probes"]["verdict"] == "fail"
    assert report["falsification"]["available"] is True
    assert report["falsification"]["caught"] == report["falsification"]["defects"] == 5
    assert any(item["policy_name"] == "leaky" for item in report["probes"]["items"])
    falsification = json.loads(artifacts.falsification_path.read_text(encoding="utf-8"))
    assert falsification["all_caught"] is True
    assert falsification["sample_rows"] >= 6
    assert "BLOCKED" in artifacts.tearsheet_path.read_text(encoding="utf-8")
    assert "raw" not in artifacts.json_path.read_text(encoding="utf-8").lower()


def test_flagship_cli_coverage_only_allow_incomplete(tmp_path: Path) -> None:
    assert (
        main([
            "flagship",
            "--lake-root",
            str(tmp_path / "missing-lake"),
            "--output-dir",
            str(tmp_path / "out"),
            "--dates",
            "20260101..20260130",
            "--coverage-only",
            "--allow-incomplete",
        ])
        == 0
    )
    assert (tmp_path / "out" / "coverage.json").exists()


def _write_complete_lake(
    lake_root: Path,
    *,
    symbols: tuple[str, ...],
    dates: tuple[str, ...],
    rows_per_partition: int,
) -> None:
    layout = HyperliquidLakeLayout(lake_root)
    for date_index, date in enumerate(dates):
        write_parquet_records(
            layout.silver_asset_ctxs_path(date),
            [
                {
                    "coin": symbol.removesuffix("-PERP"),
                    "ts_ms": _ts_ms(date_index, 0),
                    "funding": 0.0001,
                }
                for symbol in symbols
            ],
        )
        for symbol_index, symbol in enumerate(symbols):
            coin = symbol.removesuffix("-PERP")
            rows = [
                _markout_row(
                    symbol_index=symbol_index,
                    date_index=date_index,
                    row_index=row_index,
                )
                for row_index in range(rows_per_partition)
            ]
            write_parquet_records(layout.gold_markout_path(coin, date), rows)
            write_parquet_records(layout.silver_fills_path(coin, date), rows)
            write_parquet_records(
                layout.silver_l2_book_path(coin, date),
                [{"ts_ms": _ts_ms(date_index, 0), "bid_px": 99.9, "ask_px": 100.1}],
            )


def _markout_row(*, symbol_index: int, date_index: int, row_index: int) -> dict[str, object]:
    global_index = date_index * 100 + symbol_index * 10 + row_index
    maker_side = 1 if (global_index % 3) else -1
    side = "B" if global_index % 2 == 0 else "A"
    markout = ((global_index * 7) % 23 - 11) * 0.4 + symbol_index * 0.15
    return {
        "coin": ("SOL", "ETH", "BTC")[symbol_index],
        "ts_ms": _ts_ms(date_index, global_index),
        "px": 100.0 + symbol_index * 10 + row_index + date_index * 0.01,
        "sz": 1.0 + (global_index % 5) * 0.1,
        "side": side,
        "crossed": False,
        "maker_side": maker_side,
        "oid": global_index,
        "tid": global_index,
        "markout_bps_10s": markout,
        "toxic_10s": markout < 0,
    }


def _dates(count: int) -> tuple[str, ...]:
    return tuple((NOW + timedelta(days=index)).strftime("%Y%m%d") for index in range(count))


def _ts_ms(date_index: int, offset: int) -> int:
    ts = NOW + timedelta(days=date_index, seconds=offset)
    return int(ts.timestamp() * 1000)
