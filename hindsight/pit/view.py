"""Point-in-time access to replay events."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta
from typing import cast

from hindsight.core.clock import ReplayClock
from hindsight.core.cursor import CursorEvent, EventCursor
from hindsight.core.events import (
    AggTradeEvent,
    BookTickerEvent,
    CanonicalEvent,
    KlineEvent,
    TradeEvent,
)


class LookaheadError(RuntimeError):
    """Raised when code asks for data beyond the replay clock."""


TradeLikeEvent = AggTradeEvent | TradeEvent


class PointInTimeView:
    """Read-only event view pinned to the replay clock."""

    def __init__(self, clock: ReplayClock, events: Iterable[CanonicalEvent]) -> None:
        self._clock = clock
        self._events = cast(
            list[CanonicalEvent],
            list(EventCursor(cast(Iterable[CursorEvent], events))),
        )

    @property
    def now(self) -> datetime:
        if self._clock.current is None:
            raise LookaheadError("point-in-time view cannot be read before the clock starts")
        return self._clock.current

    def recent_trades(
        self,
        *,
        window: timedelta,
        end: datetime | None = None,
    ) -> tuple[TradeLikeEvent, ...]:
        effective_end = self._checked_end(end)
        start = effective_end - window
        return tuple(
            event
            for event in self._events
            if isinstance(event, AggTradeEvent | TradeEvent)
            and start <= event.timestamp <= effective_end
        )

    def top_of_book(self, *, at: datetime | None = None) -> BookTickerEvent | None:
        effective_at = self._checked_end(at)
        books = [
            event
            for event in self._events
            if isinstance(event, BookTickerEvent) and event.timestamp <= effective_at
        ]
        if not books:
            return None
        return books[-1]

    def klines(self, *, end: datetime | None = None) -> tuple[KlineEvent, ...]:
        effective_end = self._checked_end(end)
        return tuple(
            event
            for event in self._events
            if isinstance(event, KlineEvent) and event.timestamp <= effective_end
        )

    def _checked_end(self, end: datetime | None) -> datetime:
        effective_end = self.now if end is None else end
        if effective_end > self.now:
            raise LookaheadError("point-in-time access requested future data")
        return effective_end
