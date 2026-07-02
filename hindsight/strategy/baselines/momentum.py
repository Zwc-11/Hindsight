"""Kline-close momentum baseline."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from hindsight.core.events import CanonicalEvent, HyperliquidFillEvent, KlineEvent, Side, TradeEvent
from hindsight.core.types import Fill, OrderIntent, OrderType, TimeInForce
from hindsight.pit.view import PointInTimeView


@dataclass(slots=True)
class MomentumStrategy:
    """Simple breakout strategy that trades kline close momentum."""

    symbol: str
    quantity: float
    lookback_bars: int
    threshold_bps: float
    name: str = "momentum"
    _closes: list[float] = field(default_factory=list, init=False)
    _order_index: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        if self.symbol != self.symbol.upper():
            raise ValueError("symbol must be uppercase")
        if self.quantity <= 0:
            raise ValueError("quantity must be positive")
        if self.lookback_bars < 1:
            raise ValueError("lookback_bars must be positive")
        if self.threshold_bps < 0:
            raise ValueError("threshold_bps cannot be negative")

    def on_start(self) -> None:
        self._closes.clear()
        self._order_index = 0

    def on_event(self, event: CanonicalEvent, view: PointInTimeView) -> Sequence[OrderIntent]:
        price = _event_price(event)
        if price is None:
            return ()
        self._closes.append(price)
        if len(self._closes) <= self.lookback_bars:
            return ()
        reference = self._closes[-self.lookback_bars - 1]
        move_bps = (price - reference) / reference * 10_000
        if abs(move_bps) < self.threshold_bps:
            return ()
        side = Side.BUY if move_bps > 0 else Side.SELL
        self._order_index += 1
        return (
            OrderIntent(
                order_id=f"{self.name}-{self._order_index}",
                symbol=self.symbol,
                timestamp=event.timestamp,
                side=side,
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


def _event_price(event: CanonicalEvent) -> float | None:
    if isinstance(event, KlineEvent):
        return event.close_price
    if isinstance(event, (HyperliquidFillEvent, TradeEvent)):
        return event.price
    return None
