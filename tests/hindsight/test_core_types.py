from __future__ import annotations

from datetime import UTC, datetime

import pytest

from hindsight.core.events import Side
from hindsight.core.types import Fill, OrderIntent, OrderType, TimeInForce

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def test_order_intent_validates_symbol_quantity_and_limit_price() -> None:
    with pytest.raises(ValueError, match="quantity"):
        OrderIntent("o1", "BTCUSDT", NOW, Side.BUY, 0, OrderType.MARKET, TimeInForce.IOC, None, "s")
    with pytest.raises(ValueError, match="uppercase"):
        OrderIntent("o1", "btcusdt", NOW, Side.BUY, 1, OrderType.MARKET, TimeInForce.IOC, None, "s")
    with pytest.raises(ValueError, match="limit_price"):
        OrderIntent("o1", "BTCUSDT", NOW, Side.BUY, 1, OrderType.LIMIT, TimeInForce.GTC, None, "s")
    with pytest.raises(ValueError, match="positive"):
        OrderIntent("o1", "BTCUSDT", NOW, Side.BUY, 1, OrderType.MARKET, TimeInForce.IOC, 0, "s")


def test_fill_validates_price_quantity_and_fee() -> None:
    with pytest.raises(ValueError, match="price"):
        Fill("f", "o", "BTCUSDT", NOW, Side.BUY, 0, 1, 0, "taker", ())
    with pytest.raises(ValueError, match="quantity"):
        Fill("f", "o", "BTCUSDT", NOW, Side.BUY, 1, 0, 0, "taker", ())
    with pytest.raises(ValueError, match="fee"):
        Fill("f", "o", "BTCUSDT", NOW, Side.BUY, 1, 1, -1, "taker", ())
