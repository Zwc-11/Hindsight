from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from hindsight.core.clock import ReplayClock
from hindsight.core.events import AggTradeEvent, BookTickerEvent, KlineEvent
from hindsight.pit.features import FeatureRow, asof_features
from hindsight.pit.view import LookaheadError, PointInTimeView

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def kline(sequence: int, close: float) -> KlineEvent:
    timestamp = NOW + timedelta(minutes=sequence)
    return KlineEvent(
        symbol="BTCUSDT",
        timestamp=timestamp,
        sequence=sequence,
        interval="1m",
        open_time=timestamp,
        close_time=timestamp,
        open_price=close,
        high_price=close + 1,
        low_price=close - 1,
        close_price=close,
        volume=1,
        trade_count=1,
    )


def trade(sequence: int, seconds: int, price: float) -> AggTradeEvent:
    return AggTradeEvent(
        symbol="BTCUSDT",
        timestamp=NOW + timedelta(seconds=seconds),
        sequence=sequence,
        aggregate_trade_id=sequence,
        price=price,
        quantity=1,
        first_trade_id=sequence,
        last_trade_id=sequence,
        is_buyer_maker=True,
    )


def book(sequence: int, seconds: int) -> BookTickerEvent:
    return BookTickerEvent(
        symbol="BTCUSDT",
        timestamp=NOW + timedelta(seconds=seconds),
        sequence=sequence,
        update_id=sequence,
        bid_price=99,
        bid_quantity=1,
        ask_price=101,
        ask_quantity=1,
    )


def test_point_in_time_view_filters_visible_data_and_rejects_future_access() -> None:
    events = [kline(0, 100), trade(1, 10, 100.5), book(2, 20), kline(1, 101)]
    clock = ReplayClock()
    view = PointInTimeView(clock, events)
    with pytest.raises(LookaheadError, match="before the clock"):
        view.klines()
    clock.advance(NOW + timedelta(seconds=30))
    assert len(view.recent_trades(window=timedelta(seconds=30))) == 1
    assert view.top_of_book() is not None
    assert [event.close_price for event in view.klines()] == [100]
    with pytest.raises(LookaheadError, match="future"):
        view.klines(end=NOW + timedelta(minutes=2))


def test_point_in_time_view_returns_none_when_no_book_is_visible() -> None:
    clock = ReplayClock(NOW)
    view = PointInTimeView(clock, [book(1, 20)])
    assert view.top_of_book() is None


def test_asof_features_respects_strict_lag() -> None:
    rows = [
        FeatureRow(timestamp=NOW, values={"ofi": 1.0}),
        FeatureRow(timestamp=NOW + timedelta(minutes=1), values={"ofi": 2.0}),
    ]
    selected = asof_features(
        rows,
        timestamp=NOW + timedelta(minutes=2),
        feature_lag=timedelta(minutes=1),
    )
    assert selected.values["ofi"] == 2.0
    with pytest.raises(ValueError, match="feature_lag"):
        asof_features(rows, timestamp=NOW, feature_lag=timedelta(0))
    with pytest.raises(LookaheadError, match="feature"):
        asof_features(rows, timestamp=NOW, feature_lag=timedelta(minutes=1))
