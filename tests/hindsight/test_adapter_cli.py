from __future__ import annotations

import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from hindsight.cli import default_exec_config, main, run_hindsight
from hindsight.data.binance_adapter import BinanceLakeAdapter
from hindsight.strategy.base import NoopStrategy


def write_kline_file(root: Path) -> None:
    path = root / "klines" / "BTCUSDT" / "1m" / "BTCUSDT-klines-1m-2026-01-01.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    table = pa.table(
        {
            "event_id": [""],
            "timestamp": ["2026-01-01T00:00:00Z"],
            "open_price": [100.0],
            "high_price": [101.0],
            "low_price": [99.0],
            "close_price": [100.5],
            "volume": [10.0],
            "trade_count": [3],
        }
    )
    pq.write_table(table, path)


def write_depth_file(root: Path) -> None:
    path = root / "bookDepth" / "BTCUSDT" / "BTCUSDT-bookDepth-2026-01-01.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    table = pa.table(
        {
            "timestamp": ["2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"],
            "percentage": [-0.001, 0.001],
            "depth": [1.0, 1.5],
            "notional": [100.0, 150.0],
        }
    )
    pq.write_table(table, path)


def write_agg_trade_file(root: Path) -> None:
    path = root / "aggTrades" / "BTCUSDT" / "BTCUSDT-aggTrades-2026-01-01.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    table = pa.table(
        {
            "event_id": ["agg-1"],
            "timestamp": ["2026-01-01T00:00:01Z"],
            "aggregate_trade_id": [1],
            "price": [100.6],
            "quantity": [0.1],
            "first_trade_id": [10],
            "last_trade_id": [11],
            "is_buyer_maker": [True],
        }
    )
    pq.write_table(table, path)


def write_book_ticker_file(root: Path) -> None:
    path = root / "bookTicker" / "BTCUSDT" / "BTCUSDT-bookTicker-2026-01-01.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    table = pa.table(
        {
            "event_id": ["bt-1"],
            "timestamp": ["2026-01-01T00:00:02Z"],
            "update_id": [1],
            "bid_price": [100.0],
            "bid_quantity": [1.0],
            "ask_price": [101.0],
            "ask_quantity": [1.0],
        }
    )
    pq.write_table(table, path)


def test_binance_adapter_normalizes_naive_loader_timestamps_to_utc(tmp_path: Path) -> None:
    write_kline_file(tmp_path)
    events = list(
        BinanceLakeAdapter(tmp_path).stream_events(
            symbol="BTCUSDT",
            date="2026-01-01",
            limit=10,
        )
    )
    assert len(events) == 1
    assert events[0].timestamp.tzinfo is not None
    assert events[0].timestamp.utcoffset().total_seconds() == 0


def test_binance_adapter_auto_date_with_no_depth_streams_kline_only(
    tmp_path: Path,
) -> None:
    write_kline_file(tmp_path)
    events = list(
        BinanceLakeAdapter(tmp_path).stream_events(
            symbol="BTCUSDT",
            date=None,
            limit=10,
        )
    )
    assert [str(event.event_type) for event in events] == ["kline"]


def test_binance_adapter_streams_all_m0_event_types_and_auto_depth_date(
    tmp_path: Path,
) -> None:
    write_kline_file(tmp_path)
    write_depth_file(tmp_path)
    write_agg_trade_file(tmp_path)
    write_book_ticker_file(tmp_path)
    events = list(
        BinanceLakeAdapter(tmp_path).stream_events(
            symbol="BTCUSDT",
            date=None,
            limit=10,
        )
    )
    event_types = [str(event.event_type) for event in events]
    assert event_types == ["kline", "book_depth", "book_depth", "agg_trade", "book_ticker"]
    assert all(event.timestamp.tzinfo is not None for event in events)


def test_run_hindsight_writes_schema_valid_outputs(tmp_path: Path) -> None:
    lake = tmp_path / "lake"
    out = tmp_path / "out"
    write_kline_file(lake)
    write_depth_file(lake)
    artifacts = run_hindsight(
        lake_root=lake,
        output_dir=out,
        symbol="BTCUSDT",
        date="2026-01-01",
        limit=10,
        config=default_exec_config(),
        strategy=NoopStrategy(),
        repo_root=tmp_path,
    )
    payload = json.loads(artifacts.json_path.read_text(encoding="utf-8"))
    manifest = json.loads(artifacts.manifest_path.read_text(encoding="utf-8"))
    assert payload["events_processed"] == 3
    assert payload["orders_emitted"] == 0
    assert payload["manifest"]["run_id"] == manifest["run_id"]
    assert artifacts.markdown_path.read_text(encoding="utf-8").startswith("# Hindsight")


def test_run_hindsight_fails_loudly_when_no_market_events_load(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="No market events loaded"):
        run_hindsight(
            lake_root=tmp_path / "missing-lake",
            output_dir=tmp_path / "out",
            symbol="BTCUSDT",
            date="2026-01-01",
            limit=10,
            config=default_exec_config(),
            strategy=NoopStrategy(),
            repo_root=tmp_path,
        )


def test_cli_run_command_uses_defaults_when_flags_are_omitted(tmp_path: Path) -> None:
    lake = tmp_path / "lake"
    out = tmp_path / "out"
    write_kline_file(lake)
    exit_code = main(
        [
            "run",
            "--lake-root",
            str(lake),
            "--output-dir",
            str(out),
            "--date",
            "2026-01-01",
        ]
    )
    assert exit_code == 0
    assert (out / "hindsight-report.json").exists()

