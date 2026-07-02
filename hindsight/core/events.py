from __future__ import annotations

import json
from datetime import UTC, datetime
from enum import StrEnum
from hashlib import sha256
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SCHEMA_VERSION = "1.0.0"


class EventSource(StrEnum):
    BINANCE_PUBLIC = "binance_public"
    HYPERLIQUID_PUBLIC = "hyperliquid_public"
    SYNTHETIC_AGENT = "synthetic_agent"


class EventType(StrEnum):
    AGG_TRADE = "agg_trade"
    TRADE = "trade"
    HYPERLIQUID_FILL = "hyperliquid_fill"
    BOOK_DEPTH = "book_depth"
    BOOK_TICKER = "book_ticker"
    KLINE = "kline"
    AGENT_ORDER = "agent_order"


class Side(StrEnum):
    BUY = "buy"
    SELL = "sell"


class OrderAction(StrEnum):
    NEW = "new"
    CANCEL = "cancel"
    REPLACE = "replace"
    FILL = "fill"


def utc_ms(timestamp_ms: int) -> datetime:
    return datetime.fromtimestamp(timestamp_ms / 1000, tz=UTC)


def stable_hash(payload: dict[str, Any]) -> str:
    clean = {key: value for key, value in payload.items() if key != "event_id"}
    encoded = json.dumps(clean, sort_keys=True, separators=(",", ":"), default=str)
    return sha256(encoded.encode("utf-8")).hexdigest()


class BaseEvent(BaseModel):
    model_config = ConfigDict(
        frozen=True,
        use_enum_values=True,
        extra="forbid",
    )

    event_id: str | None = None
    schema_version: str = SCHEMA_VERSION
    event_type: EventType
    source: EventSource
    symbol: str = Field(min_length=1)
    exchange: str = "binance_usdm"
    timestamp: datetime
    sequence: int = Field(ge=0)

    @field_validator("timestamp")
    @classmethod
    def timestamp_must_be_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamp must include timezone information")
        return value.astimezone(UTC)

    @field_validator("symbol")
    @classmethod
    def symbol_must_be_uppercase(cls, value: str) -> str:
        normalized = value.upper()
        if normalized != value:
            raise ValueError("symbol must be uppercase")
        return value

    def model_post_init(self, __context: Any) -> None:
        if self.event_id is None:
            object.__setattr__(self, "event_id", stable_hash(self.model_dump(mode="json")))


class AggTradeEvent(BaseEvent):
    event_type: Literal[EventType.AGG_TRADE] = EventType.AGG_TRADE
    source: Literal[EventSource.BINANCE_PUBLIC] = EventSource.BINANCE_PUBLIC
    aggregate_trade_id: int = Field(ge=0)
    price: float = Field(gt=0)
    quantity: float = Field(gt=0)
    first_trade_id: int = Field(ge=0)
    last_trade_id: int = Field(ge=0)
    is_buyer_maker: bool

    @model_validator(mode="after")
    def trade_id_range_is_valid(self) -> AggTradeEvent:
        if self.last_trade_id < self.first_trade_id:
            raise ValueError("last_trade_id must be >= first_trade_id")
        return self


class TradeEvent(BaseEvent):
    event_type: Literal[EventType.TRADE] = EventType.TRADE
    source: Literal[EventSource.BINANCE_PUBLIC] = EventSource.BINANCE_PUBLIC
    trade_id: int = Field(ge=0)
    price: float = Field(gt=0)
    quantity: float = Field(gt=0)
    side: Side


class HyperliquidFillEvent(BaseEvent):
    event_type: Literal[EventType.HYPERLIQUID_FILL] = EventType.HYPERLIQUID_FILL
    source: Literal[EventSource.HYPERLIQUID_PUBLIC] = EventSource.HYPERLIQUID_PUBLIC
    exchange: Literal["hyperliquid"] = "hyperliquid"
    trade_id: int | None = Field(ge=0)
    price: float = Field(gt=0)
    quantity: float = Field(gt=0)
    side: Side
    crossed: bool | None
    maker_side: int
    fee: float | None = Field(ge=0)
    fee_token: str | None

    @model_validator(mode="after")
    def maker_side_is_signed(self) -> HyperliquidFillEvent:
        if self.maker_side not in {-1, 1}:
            raise ValueError("maker_side must be -1 or 1")
        return self


class BookTickerEvent(BaseEvent):
    event_type: Literal[EventType.BOOK_TICKER] = EventType.BOOK_TICKER
    source: Literal[EventSource.BINANCE_PUBLIC] = EventSource.BINANCE_PUBLIC
    update_id: int = Field(ge=0)
    bid_price: float = Field(gt=0)
    bid_quantity: float = Field(ge=0)
    ask_price: float = Field(gt=0)
    ask_quantity: float = Field(ge=0)

    @model_validator(mode="after")
    def spread_is_valid(self) -> BookTickerEvent:
        if self.bid_price >= self.ask_price:
            raise ValueError("bid_price must be below ask_price")
        return self


class BookDepthEvent(BaseEvent):
    event_type: Literal[EventType.BOOK_DEPTH] = EventType.BOOK_DEPTH
    source: Literal[EventSource.BINANCE_PUBLIC] = EventSource.BINANCE_PUBLIC
    percentage: float
    depth: float = Field(ge=0)
    notional: float = Field(ge=0)


class KlineEvent(BaseEvent):
    event_type: Literal[EventType.KLINE] = EventType.KLINE
    source: Literal[EventSource.BINANCE_PUBLIC] = EventSource.BINANCE_PUBLIC
    interval: str = Field(min_length=1)
    open_time: datetime
    close_time: datetime
    open_price: float = Field(gt=0)
    high_price: float = Field(gt=0)
    low_price: float = Field(gt=0)
    close_price: float = Field(gt=0)
    volume: float = Field(ge=0)
    trade_count: int = Field(ge=0)

    @model_validator(mode="after")
    def candle_is_valid(self) -> KlineEvent:
        prices = [self.open_price, self.high_price, self.low_price, self.close_price]
        if self.low_price > min(prices) or self.high_price < max(prices):
            raise ValueError("kline high/low must contain open and close")
        if self.close_time < self.open_time:
            raise ValueError("close_time must be >= open_time")
        return self


class AgentOrderEvent(BaseEvent):
    event_type: Literal[EventType.AGENT_ORDER] = EventType.AGENT_ORDER
    source: Literal[EventSource.SYNTHETIC_AGENT] = EventSource.SYNTHETIC_AGENT
    scenario_id: str = Field(min_length=1)
    agent_id: str = Field(min_length=1)
    order_id: str = Field(min_length=1)
    action: OrderAction
    side: Side
    price: float = Field(gt=0)
    quantity: float = Field(gt=0)
    remaining_quantity: float = Field(ge=0)

    @model_validator(mode="after")
    def remaining_cannot_exceed_quantity(self) -> AgentOrderEvent:
        if self.remaining_quantity > self.quantity:
            raise ValueError("remaining_quantity cannot exceed quantity")
        return self


CanonicalEvent = (
    AggTradeEvent
    | TradeEvent
    | HyperliquidFillEvent
    | BookDepthEvent
    | BookTickerEvent
    | KlineEvent
    | AgentOrderEvent
)

EVENT_MODEL_BY_TYPE: dict[str, type[CanonicalEvent]] = {
    EventType.AGG_TRADE.value: AggTradeEvent,
    EventType.TRADE.value: TradeEvent,
    EventType.HYPERLIQUID_FILL.value: HyperliquidFillEvent,
    EventType.BOOK_DEPTH.value: BookDepthEvent,
    EventType.BOOK_TICKER.value: BookTickerEvent,
    EventType.KLINE.value: KlineEvent,
    EventType.AGENT_ORDER.value: AgentOrderEvent,
}


def parse_event(payload: dict[str, Any]) -> CanonicalEvent:
    event_type = str(payload["event_type"])
    return EVENT_MODEL_BY_TYPE[event_type].model_validate(payload)
