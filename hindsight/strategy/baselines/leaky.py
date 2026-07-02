"""Intentionally leaky baseline used to verify the leakage auditor."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from hindsight.core.events import CanonicalEvent, KlineEvent, Side
from hindsight.core.types import Fill, OrderIntent, OrderType, TimeInForce
from hindsight.pit.view import PointInTimeView

LEAKY_FEATURE_NAMES = ("future_return_bps",)


@dataclass(slots=True)
class LeakyFutureReturnStrategy:
    """Trades from precomputed future returns and must fail leakage review."""

    symbol: str
    quantity: float
    future_return_bps_by_event_id: Mapping[str, float]
    threshold_bps: float
    name: str = "leaky"
    _order_index: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        if self.symbol != self.symbol.upper():
            raise ValueError("symbol must be uppercase")
        if self.quantity <= 0:
            raise ValueError("quantity must be positive")
        if self.threshold_bps < 0:
            raise ValueError("threshold_bps cannot be negative")

    def on_start(self) -> None:
        self._order_index = 0

    def on_event(self, event: CanonicalEvent, view: PointInTimeView) -> Sequence[OrderIntent]:
        if not isinstance(event, KlineEvent):
            return ()
        event_id = event.event_id
        if event_id is None:
            raise ValueError("leaky strategy requires event_id")
        future_return = self.future_return_bps_by_event_id[event_id]
        if abs(future_return) < self.threshold_bps:
            return ()
        self._order_index += 1
        return (
            OrderIntent(
                order_id=f"{self.name}-{self._order_index}",
                symbol=self.symbol,
                timestamp=event.timestamp,
                side=Side.BUY if future_return > 0 else Side.SELL,
                quantity=self.quantity,
                order_type=OrderType.MARKET,
                time_in_force=TimeInForce.IOC,
                limit_price=None,
                strategy_name=self.name,
            ),
        )

    def on_fill(self, fill: Fill) -> None:
        return None

    def on_finish(self) -> None:
        return None
