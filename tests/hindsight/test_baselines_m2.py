from __future__ import annotations

from datetime import UTC, datetime
from typing import cast

from hindsight.core.events import BookTickerEvent, KlineEvent, Side
from hindsight.pit.view import PointInTimeView
from hindsight.strategy.baselines.leaky import LEAKY_FEATURE_NAMES, LeakyFutureReturnStrategy
from hindsight.strategy.baselines.ofi_quote import OfiQuoteStrategy

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def test_ofi_quote_places_passive_order_on_large_imbalance() -> None:
    strategy = OfiQuoteStrategy(symbol="BTC-PERP", quantity=0.5, imbalance_threshold=0.5)
    event = BookTickerEvent(
        symbol="BTC-PERP",
        timestamp=NOW,
        sequence=0,
        update_id=1,
        bid_price=100,
        bid_quantity=9,
        ask_price=101,
        ask_quantity=1,
    )

    orders = strategy.on_event(event, cast(PointInTimeView, object()))

    assert len(orders) == 1
    assert orders[0].side == Side.BUY
    assert orders[0].limit_price == 100


def test_leaky_strategy_uses_future_return_feature() -> None:
    event = KlineEvent(
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
