from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from hindsight.data.repositories import AggTradeRepository, BookTickerRepository


def write_agg_trade_file(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    table = pa.table(
        {
            "event_id": ["agg-2", "agg-1"],
            "timestamp": ["2026-01-01T00:00:01Z", "2026-01-01T00:00:00Z"],
            "aggregate_trade_id": [2, 1],
            "price": [101.0, 100.0],
            "quantity": [0.2, 0.1],
            "first_trade_id": [20, 10],
            "last_trade_id": [21, 11],
            "is_buyer_maker": [False, True],
        }
    )
    pq.write_table(table, path)


def write_agg_trade_file_with_string_bool(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    table = pa.table(
        {
            "event_id": ["agg-1", "agg-2"],
            "timestamp": [1_767_225_600_000, 1_767_225_601_000.0],
            "aggregate_trade_id": [1, 2],
            "price": [100.0, 101.0],
            "quantity": [0.1, 0.2],
            "first_trade_id": [10, 20],
            "last_trade_id": [11, 21],
            "is_buyer_maker": ["true", "false"],
        }
    )
    pq.write_table(table, path)


def write_invalid_bool_agg_trade_file(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    table = pa.table(
        {
            "event_id": ["agg-1"],
            "timestamp": [datetime(2026, 1, 1, tzinfo=UTC)],
            "aggregate_trade_id": [1],
            "price": [100.0],
            "quantity": [0.1],
            "first_trade_id": [10],
            "last_trade_id": [11],
            "is_buyer_maker": ["not-bool"],
        }
    )
    pq.write_table(table, path)


def write_book_ticker_file(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    table = pa.table(
        {
            "event_id": ["bt-1"],
            "timestamp": ["2026-01-01T00:00:00Z"],
            "update_id": [300],
            "bid_price": [99.0],
            "bid_quantity": [1.0],
            "ask_price": [101.0],
            "ask_quantity": [2.0],
        }
    )
    pq.write_table(table, path)


def write_book_ticker_file_with_int_timestamp(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    table = pa.table(
        {
            "event_id": ["bt-1"],
            "timestamp": [1_767_225_600_000],
            "update_id": [300],
            "bid_price": [99.0],
            "bid_quantity": [1.0],
            "ask_price": [101.0],
            "ask_quantity": [2.0],
        }
    )
    pq.write_table(table, path)


def test_agg_trade_repository_missing_directory_returns_empty(tmp_path: Path) -> None:
    repo = AggTradeRepository(tmp_path)
    assert repo.available_dates("BTCUSDT") == []
    assert repo.load("BTCUSDT", "2026-01-01", 10) == []


def test_agg_trade_repository_loads_and_sorts_records(tmp_path: Path) -> None:
    path = tmp_path / "aggTrades" / "BTCUSDT" / "BTCUSDT-aggTrades-2026-01-01.parquet"
    write_agg_trade_file(path)
    repo = AggTradeRepository(tmp_path)
    records = repo.load("BTCUSDT", "2026-01-01", 10)
    assert repo.available_dates("BTCUSDT") == ["2026-01-01"]
    assert [record.aggregate_trade_id for record in records] == [1, 2]
    assert records[0].timestamp == datetime.fromisoformat("2026-01-01T00:00:00+00:00")


def test_agg_trade_repository_handles_epoch_timestamps_and_string_bools(
    tmp_path: Path,
) -> None:
    path = tmp_path / "aggTrades" / "BTCUSDT" / "BTCUSDT-aggTrades-2026-01-01.parquet"
    write_agg_trade_file_with_string_bool(path)
    repo = AggTradeRepository(tmp_path)
    records = repo.load("BTCUSDT", "2026-01-01", 2)
    assert len(records) == 2
    assert records[0].timestamp == datetime.fromtimestamp(1_767_225_600, tz=UTC)
    assert records[0].is_buyer_maker is True
    assert records[1].is_buyer_maker is False


def test_agg_trade_repository_rejects_invalid_bool(tmp_path: Path) -> None:
    path = tmp_path / "aggTrades" / "BTCUSDT" / "BTCUSDT-aggTrades-2026-01-01.parquet"
    write_invalid_bool_agg_trade_file(path)
    repo = AggTradeRepository(tmp_path)
    with pytest.raises(ValueError, match="boolean parquet"):
        repo.load("BTCUSDT", "2026-01-01", 10)


def test_agg_trade_repository_existing_empty_directory_returns_empty(tmp_path: Path) -> None:
    (tmp_path / "aggTrades" / "BTCUSDT").mkdir(parents=True)
    repo = AggTradeRepository(tmp_path)
    assert repo.load("BTCUSDT", None, 10) == []


def test_book_ticker_repository_missing_directory_returns_empty(tmp_path: Path) -> None:
    repo = BookTickerRepository(tmp_path)
    assert repo.available_dates("BTCUSDT") == []
    assert repo.load("BTCUSDT", "2026-01-01", 10) == []


def test_book_ticker_repository_loads_records(tmp_path: Path) -> None:
    path = tmp_path / "bookTicker" / "BTCUSDT" / "BTCUSDT-bookTicker-2026-01-01.parquet"
    write_book_ticker_file(path)
    repo = BookTickerRepository(tmp_path)
    records = repo.load("BTCUSDT", None, 10)
    assert repo.available_dates("BTCUSDT") == ["2026-01-01"]
    assert len(records) == 1
    assert records[0].bid_price == 99.0


def test_book_ticker_repository_handles_int_timestamp(tmp_path: Path) -> None:
    path = tmp_path / "bookTicker" / "BTCUSDT" / "BTCUSDT-bookTicker-2026-01-01.parquet"
    write_book_ticker_file_with_int_timestamp(path)
    repo = BookTickerRepository(tmp_path)
    records = repo.load("BTCUSDT", "2026-01-01", 10)
    assert records[0].timestamp == datetime.fromtimestamp(1_767_225_600, tz=UTC)
