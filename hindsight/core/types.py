"""Execution value objects for Hindsight."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Literal

from hindsight.core.events import Side

Liquidity = Literal["maker", "taker"]


class OrderType(StrEnum):
    """Supported synthetic order types."""

    MARKET = "market"
    LIMIT = "limit"


class TimeInForce(StrEnum):
    """Supported time-in-force policies."""

    GTC = "gtc"
    IOC = "ioc"


@dataclass(frozen=True, slots=True)
class OrderIntent:
    """A strategy's requested order before simulator latency and fills."""

    order_id: str
    symbol: str
    timestamp: datetime
    side: Side
    quantity: float
    order_type: OrderType
    time_in_force: TimeInForce
    limit_price: float | None
    strategy_name: str

    def __post_init__(self) -> None:
        if self.quantity <= 0:
            raise ValueError("order quantity must be positive")
        if self.symbol != self.symbol.upper():
            raise ValueError("order symbol must be uppercase")
        if self.order_type is OrderType.LIMIT and self.limit_price is None:
            raise ValueError("limit orders require limit_price")
        if self.limit_price is not None and self.limit_price <= 0:
            raise ValueError("limit_price must be positive when provided")


@dataclass(frozen=True, slots=True)
class Fill:
    """A simulator fill for an active order."""

    fill_id: str
    order_id: str
    symbol: str
    timestamp: datetime
    side: Side
    price: float
    quantity: float
    fee: float
    liquidity: Liquidity
    flags: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.price <= 0:
            raise ValueError("fill price must be positive")
        if self.quantity <= 0:
            raise ValueError("fill quantity must be positive")
        if self.fee < 0:
            raise ValueError("fill fee cannot be negative")


@dataclass(frozen=True, slots=True)
class Position:
    """Signed position state for one symbol."""

    symbol: str
    quantity: float
    average_entry_price: float


@dataclass(frozen=True, slots=True)
class PortfolioState:
    """Marked portfolio snapshot."""

    timestamp: datetime
    cash: float
    position: Position
    realized_pnl: float
    unrealized_pnl: float
    equity: float
    fees_paid: float
    funding_paid: float


@dataclass(frozen=True, slots=True)
class EquityPoint:
    """One point on an equity curve."""

    timestamp: datetime
    equity: float
    cash: float
    position_quantity: float
    mark_price: float
