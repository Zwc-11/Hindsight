"""Portfolio accounting for simulated fills."""

from __future__ import annotations

from datetime import datetime

from hindsight.core.events import Side
from hindsight.core.types import EquityPoint, Fill, PortfolioState, Position


class Portfolio:
    """Single-symbol cash and position accounting."""

    def __init__(self, *, initial_cash: float, symbol: str) -> None:
        if initial_cash <= 0:
            raise ValueError("initial_cash must be positive")
        if symbol != symbol.upper():
            raise ValueError("symbol must be uppercase")
        self._initial_cash = initial_cash
        self._symbol = symbol
        self._cash = initial_cash
        self._quantity = 0.0
        self._average_entry_price = 0.0
        self._realized_pnl = 0.0
        self._fees_paid = 0.0
        self._funding_paid = 0.0
        self._last_state: PortfolioState | None = None

    @property
    def cash(self) -> float:
        return self._cash

    @property
    def quantity(self) -> float:
        return self._quantity

    def apply_fill(self, fill: Fill) -> None:
        if fill.symbol != self._symbol:
            raise ValueError("fill symbol does not match portfolio")
        signed_quantity = fill.quantity if fill.side == Side.BUY else -fill.quantity
        self._cash -= signed_quantity * fill.price
        self._cash -= fill.fee
        self._fees_paid += fill.fee
        self._apply_position_change(signed_quantity, fill.price)

    def apply_funding(self, payment: float) -> None:
        self._cash -= payment
        self._funding_paid += payment

    def mark(self, *, timestamp: datetime, mark_price: float) -> PortfolioState:
        if mark_price <= 0:
            raise ValueError("mark_price must be positive")
        unrealized = (mark_price - self._average_entry_price) * self._quantity
        equity = self._cash + self._quantity * mark_price
        state = PortfolioState(
            timestamp=timestamp,
            cash=self._cash,
            position=Position(
                symbol=self._symbol,
                quantity=self._quantity,
                average_entry_price=self._average_entry_price,
            ),
            realized_pnl=self._realized_pnl,
            unrealized_pnl=unrealized,
            equity=equity,
            fees_paid=self._fees_paid,
            funding_paid=self._funding_paid,
        )
        self._last_state = state
        return state

    def equity_point(self, *, timestamp: datetime, mark_price: float) -> EquityPoint:
        state = self.mark(timestamp=timestamp, mark_price=mark_price)
        return EquityPoint(
            timestamp=timestamp,
            equity=state.equity,
            cash=state.cash,
            position_quantity=state.position.quantity,
            mark_price=mark_price,
        )

    def state(self) -> PortfolioState:
        if self._last_state is None:
            raise ValueError("portfolio has not been marked")
        return self._last_state

    def _apply_position_change(self, signed_quantity: float, price: float) -> None:
        previous_quantity = self._quantity
        new_quantity = previous_quantity + signed_quantity
        if previous_quantity == 0 or _same_direction(previous_quantity, signed_quantity):
            total_abs = abs(previous_quantity) + abs(signed_quantity)
            self._average_entry_price = (
                (abs(previous_quantity) * self._average_entry_price)
                + (abs(signed_quantity) * price)
            ) / total_abs
            self._quantity = new_quantity
            return

        closed_quantity = min(abs(previous_quantity), abs(signed_quantity))
        direction = 1.0 if previous_quantity > 0 else -1.0
        self._realized_pnl += closed_quantity * (price - self._average_entry_price) * direction
        self._quantity = new_quantity
        if new_quantity == 0:
            self._average_entry_price = 0.0
        elif _same_direction(new_quantity, previous_quantity):
            pass
        else:
            self._average_entry_price = price


def _same_direction(left: float, right: float) -> bool:
    return (left > 0 and right > 0) or (left < 0 and right < 0)
