"""Deterministic event ordering."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from datetime import datetime
from typing import Protocol


class CursorEvent(Protocol):
    """Minimum event shape needed for replay ordering."""

    timestamp: datetime
    sequence: int
    event_id: str | None

class EventCursor[EventT: CursorEvent]:
    """Iterates events by timestamp, sequence, then stable event id."""

    def __init__(self, events: Iterable[EventT]) -> None:
        self._events = sorted(
            events,
            key=lambda event: (event.timestamp, event.sequence, event.event_id or ""),
        )

    def __iter__(self) -> Iterator[EventT]:
        return iter(self._events)

    def __len__(self) -> int:
        return len(self._events)
