from __future__ import annotations

from datetime import UTC

import pytest

from hindsight.core.events import EventSource, HyperliquidFillEvent, Side
from hindsight.data.hyperliquid_adapter import HyperliquidLakeAdapter
from hindsight.data.lake import HyperliquidLakeLayout, write_parquet_records


def test_hyperliquid_adapter_streams_silver_fills(tmp_path) -> None:
    layout = HyperliquidLakeLayout(tmp_path)
    write_parquet_records(
        layout.silver_fills_path("SOL", "20260101"),
        [
            {
                "coin": "SOL",
                "ts_ms": 1_704_067_200_000,
                "px": 100.25,
                "sz": 2.5,
                "notional": 250.625,
                "side": "B",
                "crossed": True,
                "maker_side": -1,
                "fee": 0.02,
                "fee_bps": 0.8,
                "fee_token": "USDC",
                "oid": 11,
                "tid": 22,
                "hash": "0xabc",
                "direction": "Open Long",
            }
        ],
    )

    adapter = HyperliquidLakeAdapter(tmp_path)
    events = list(adapter.stream_events(symbol="SOL-PERP", date="20260101", limit=5))

    assert len(events) == 1
    event = events[0]
    assert isinstance(event, HyperliquidFillEvent)
    assert event.source == EventSource.HYPERLIQUID_PUBLIC.value
    assert event.symbol == "SOL-PERP"
    assert event.exchange == "hyperliquid"
    assert event.timestamp.tzinfo == UTC
    assert event.trade_id == 22
    assert event.side == Side.BUY
    assert event.maker_side == -1
    assert event.fee == pytest.approx(0.02)


def test_hyperliquid_adapter_requires_date(tmp_path) -> None:
    adapter = HyperliquidLakeAdapter(tmp_path)

    with pytest.raises(ValueError, match="requires an explicit date"):
        list(adapter.stream_events(symbol="SOL", date=None, limit=1))


def test_hyperliquid_adapter_rejects_missing_required_column(tmp_path) -> None:
    layout = HyperliquidLakeLayout(tmp_path)
    write_parquet_records(
        layout.silver_fills_path("SOL", "20260101"),
        [
            {
                "coin": "SOL",
                "ts_ms": 1_704_067_200_000,
                "px": 100.25,
                "sz": 2.5,
                "side": "B",
                "crossed": True,
                "fee": 0.02,
                "fee_token": "USDC",
                "tid": 22,
            }
        ],
    )
    adapter = HyperliquidLakeAdapter(tmp_path)

    with pytest.raises(ValueError, match="maker_side"):
        list(adapter.stream_events(symbol="SOL", date="20260101", limit=1))


def test_hyperliquid_adapter_loads_gold_markouts(tmp_path) -> None:
    layout = HyperliquidLakeLayout(tmp_path)
    write_parquet_records(
        layout.gold_markout_path("SOL", "20260101"),
        [
            {
                "coin": "SOL",
                "ts_ms": 1_704_067_200_000,
                "px": 100.25,
                "sz": 2.5,
                "side": "A",
                "crossed": False,
                "maker_side": -1,
                "oid": 11,
                "tid": 22,
                "markout_bps_10s": -4.5,
                "toxic_10s": True,
            }
        ],
    )

    records = HyperliquidLakeAdapter(tmp_path).load_markouts(
        symbol="SOL-PERP",
        date="20260101",
        limit=5,
    )

    assert len(records) == 1
    assert records[0].symbol == "SOL-PERP"
    assert records[0].side == Side.SELL
    assert records[0].markout_bps == {"10s": pytest.approx(-4.5)}
