"""Order-flow-imbalance quote baseline."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from hindsight.core.events import BookTickerEvent, CanonicalEvent, Side
from hindsight.core.types import Fill, OrderIntent, OrderType, TimeInForce
from hindsight.pit.view import PointInTimeView


@dataclass(slots=True)
class OfiQuoteStrategy:
    """Places a passive quote when top-of-book imbalance is large enough."""

    symbol: str
    quantity: float
    imbalance_threshold: float
    name: str = "ofi_quote"
    _order_index: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        if self.symbol != self.symbol.upper():
            raise ValueError("symbol must be uppercase")
        if self.quantity <= 0:
            raise ValueError("quantity must be positive")
        if not 0 <= self.imbalance_threshold <= 1:
            raise ValueError("imbalance_threshold must be between 0 and 1")

    def on_start(self) -> None:
        self._order_index = 0

    def on_event(self, event: CanonicalEvent, view: PointInTimeView) -> Sequence[OrderIntent]:
        if not isinstance(event, BookTickerEvent):
            return ()
        total_quantity = event.bid_quantity + event.ask_quantity
        if total_quantity <= 0:
            raise ValueError("book ticker quantities cannot both be zero")
        imbalance = (event.bid_quantity - event.ask_quantity) / total_quantity
        if abs(imbalance) < self.imbalance_threshold:
            return ()
        side = Side.BUY if imbalance > 0 else Side.SELL
        price = event.bid_price if side == Side.BUY else event.ask_price
        self._order_index += 1
        return (
            OrderIntent(
                order_id=f"{self.name}-{self._order_index}",
                symbol=self.symbol,
                timestamp=event.timestamp,
                side=side,
                quantity=self.quantity,
                order_type=OrderType.LIMIT,
                time_in_force=TimeInForce.GTC,
                limit_price=price,
                strategy_name=self.name,
            ),
        )

    def on_fill(self, fill: Fill) -> None:
        return None

    def on_finish(self) -> None:
        return None
