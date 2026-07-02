from __future__ import annotations

from datetime import UTC, datetime

import pytest

from hindsight.core.events import (
    AgentOrderEvent,
    AggTradeEvent,
    BookDepthEvent,
    BookTickerEvent,
    EventType,
    HyperliquidFillEvent,
    KlineEvent,
    OrderAction,
    Side,
    TradeEvent,
    parse_event,
)

NOW = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.mark.parametrize(
    "event",
    [
        AggTradeEvent(
            symbol="BTCUSDT",
            timestamp=NOW,
            sequence=0,
            aggregate_trade_id=1,
            price=100,
            quantity=1,
            first_trade_id=1,
            last_trade_id=1,
            is_buyer_maker=True,
        ),
        TradeEvent(
            symbol="BTCUSDT",
            timestamp=NOW,
            sequence=1,
            trade_id=2,
            price=100,
            quantity=1,
            side=Side.BUY,
        ),
        HyperliquidFillEvent(
            symbol="SOL-PERP",
            timestamp=NOW,
            sequence=2,
            trade_id=3,
            price=100,
            quantity=1,
            side=Side.SELL,
            crossed=False,
            maker_side=-1,
            fee=0.01,
            fee_token="USDC",
        ),
        BookDepthEvent(
            symbol="BTCUSDT",
            timestamp=NOW,
            sequence=3,
            percentage=0.001,
            depth=10,
            notional=1000,
        ),
        BookTickerEvent(
            symbol="BTCUSDT",
            timestamp=NOW,
            sequence=4,
            update_id=5,
            bid_price=99,
            bid_quantity=1,
            ask_price=101,
            ask_quantity=1,
        ),
        KlineEvent(
            symbol="BTCUSDT",
            timestamp=NOW,
            sequence=5,
            interval="1m",
            open_time=NOW,
            close_time=NOW,
            open_price=100,
            high_price=101,
            low_price=99,
            close_price=100,
            volume=1,
            trade_count=1,
        ),
        AgentOrderEvent(
            symbol="BTCUSDT",
            timestamp=NOW,
            sequence=6,
            scenario_id="scenario",
            agent_id="agent",
            order_id="order",
            action=OrderAction.NEW,
            side=Side.BUY,
            price=100,
            quantity=1,
            remaining_quantity=1,
        ),
    ],
)
def test_vendored_event_payloads_roundtrip_through_parser(event) -> None:
    payload = event.model_dump(mode="json")

    parsed = parse_event(payload)

    assert parsed == event
    assert parsed.event_id == event.event_id
    assert payload["event_type"] in {event_type.value for event_type in EventType}
