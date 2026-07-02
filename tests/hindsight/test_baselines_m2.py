from __future__ import annotations

from datetime import UTC, datetime
from typing import cast

import pytest

from hindsight.core.events import BookTickerEvent, KlineEvent, Side, TradeEvent
from hindsight.core.types import Fill
from hindsight.pit.view import PointInTimeView
from hindsight.strategy.baselines.leaky import LEAKY_FEATURE_NAMES, LeakyFutureReturnStrategy
from hindsight.strategy.baselines.momentum import MomentumStrategy
from hindsight.strategy.baselines.ofi_quote import OfiQuoteStrategy

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def test_ofi_quote_places_passive_order_on_large_imbalance() -> None:
    strategy = OfiQuoteStrategy(symbol="BTC-PERP", quantity=0.5, imbalance_threshold=0.5)
    event = book_ticker(bid_quantity=9, ask_quantity=1)

    orders = strategy.on_event(event, cast(PointInTimeView, object()))

    assert len(orders) == 1
    assert orders[0].side == Side.BUY
    assert orders[0].limit_price == 100


def test_ofi_quote_validates_config() -> None:
    with pytest.raises(ValueError, match="symbol"):
        OfiQuoteStrategy(symbol="btc-perp", quantity=1, imbalance_threshold=0.5)
    with pytest.raises(ValueError, match="quantity"):
        OfiQuoteStrategy(symbol="BTC-PERP", quantity=0, imbalance_threshold=0.5)
    with pytest.raises(ValueError, match="imbalance_threshold"):
        OfiQuoteStrategy(symbol="BTC-PERP", quantity=1, imbalance_threshold=1.1)


def test_ofi_quote_ignores_non_book_events_and_small_imbalance() -> None:
    strategy = OfiQuoteStrategy(symbol="BTC-PERP", quantity=0.5, imbalance_threshold=0.5)
    kline = KlineEvent(
        symbol="BTC-PERP",
        timestamp=NOW,
        sequence=0,
        interval="1m",
        open_time=NOW,
        close_time=NOW,
        open_price=100,
        high_price=102,
        low_price=99,
        close_price=101,
        volume=1,
        trade_count=1,
    )

    assert strategy.on_event(kline, cast(PointInTimeView, object())) == ()
    assert strategy.on_event(
        book_ticker(bid_quantity=6, ask_quantity=4),
        cast(PointInTimeView, object()),
    ) == ()


def test_ofi_quote_rejects_empty_top_of_book() -> None:
    strategy = OfiQuoteStrategy(symbol="BTC-PERP", quantity=0.5, imbalance_threshold=0.5)

    with pytest.raises(ValueError, match="quantities"):
        strategy.on_event(
            book_ticker(bid_quantity=0, ask_quantity=0),
            cast(PointInTimeView, object()),
        )


def test_ofi_quote_places_sell_order_on_negative_imbalance_and_resets_index() -> None:
    strategy = OfiQuoteStrategy(symbol="BTC-PERP", quantity=0.5, imbalance_threshold=0.5)
    first = strategy.on_event(
        book_ticker(bid_quantity=1, ask_quantity=9),
        cast(PointInTimeView, object()),
    )
    strategy.on_start()
    second = strategy.on_event(
        book_ticker(bid_quantity=1, ask_quantity=9),
        cast(PointInTimeView, object()),
    )

    assert len(first) == 1
    assert first[0].side == Side.SELL
    assert first[0].limit_price == 101
    assert first[0].order_id == "ofi_quote-1"
    assert second[0].order_id == "ofi_quote-1"


def test_ofi_quote_lifecycle_noops() -> None:
    strategy = OfiQuoteStrategy(symbol="BTC-PERP", quantity=0.5, imbalance_threshold=0.5)
    fill = cast(Fill, object())

    assert strategy.on_fill(fill) is None
    assert strategy.on_finish() is None


def test_leaky_strategy_uses_future_return_feature() -> None:
    event = kline(close=101)
    assert event.event_id is not None
    strategy = LeakyFutureReturnStrategy(
        symbol="BTC-PERP",
        quantity=1,
        future_return_bps_by_event_id={event.event_id: -5},
        threshold_bps=1,
    )

    orders = strategy.on_event(event, cast(PointInTimeView, object()))

    assert LEAKY_FEATURE_NAMES == ("future_return_bps",)
    assert len(orders) == 1
    assert orders[0].side == Side.SELL


def test_leaky_strategy_validates_config_and_ignores_non_klines() -> None:
    with pytest.raises(ValueError, match="symbol"):
        LeakyFutureReturnStrategy("btc-perp", 1, {}, 1)
    with pytest.raises(ValueError, match="quantity"):
        LeakyFutureReturnStrategy("BTC-PERP", 0, {}, 1)
    with pytest.raises(ValueError, match="threshold_bps"):
        LeakyFutureReturnStrategy("BTC-PERP", 1, {}, -1)

    strategy = LeakyFutureReturnStrategy("BTC-PERP", 1, {}, 1)
    assert strategy.on_event(
        book_ticker(bid_quantity=1, ask_quantity=1),
        cast(PointInTimeView, object()),
    ) == ()


def test_leaky_strategy_threshold_buy_reset_and_lifecycle() -> None:
    event = kline(close=101)
    assert event.event_id is not None
    strategy = LeakyFutureReturnStrategy(
        symbol="BTC-PERP",
        quantity=1,
        future_return_bps_by_event_id={event.event_id: 5},
        threshold_bps=10,
    )

    assert strategy.on_event(event, cast(PointInTimeView, object())) == ()
    strategy.threshold_bps = 1
    first = strategy.on_event(event, cast(PointInTimeView, object()))
    strategy.on_start()
    second = strategy.on_event(event, cast(PointInTimeView, object()))

    assert first[0].side == Side.BUY
    assert first[0].order_id == "leaky-1"
    assert second[0].order_id == "leaky-1"
    assert strategy.on_fill(cast(Fill, object())) is None
    assert strategy.on_finish() is None


def test_momentum_lifecycle_noops() -> None:
    strategy = MomentumStrategy("BTC-PERP", 1, 1, 1)

    assert strategy.on_fill(cast(Fill, object())) is None
    assert strategy.on_finish() is None


def test_momentum_uses_trade_event_prices() -> None:
    strategy = MomentumStrategy("BTC-PERP", 1, 1, 0)

    assert strategy.on_event(trade(price=100), cast(PointInTimeView, object())) == ()
    orders = strategy.on_event(trade(price=101), cast(PointInTimeView, object()))

    assert len(orders) == 1
    assert orders[0].side == Side.BUY


def kline(*, close: float) -> KlineEvent:
    return KlineEvent(
        symbol="BTC-PERP",
        timestamp=NOW,
        sequence=0,
        interval="1m",
        open_time=NOW,
        close_time=NOW,
        open_price=100,
        high_price=102,
        low_price=99,
        close_price=close,
        volume=1,
        trade_count=1,
    )


def trade(*, price: float) -> TradeEvent:
    return TradeEvent(
        symbol="BTC-PERP",
        timestamp=NOW,
        sequence=0,
        trade_id=1,
        price=price,
        quantity=1,
        side=Side.BUY,
    )


def book_ticker(*, bid_quantity: float, ask_quantity: float) -> BookTickerEvent:
    return BookTickerEvent(
        symbol="BTC-PERP",
        timestamp=NOW,
        sequence=0,
        update_id=1,
        bid_price=100,
        bid_quantity=bid_quantity,
        ask_price=101,
        ask_quantity=ask_quantity,
    )
