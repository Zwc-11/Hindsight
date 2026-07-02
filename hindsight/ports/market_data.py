"""Market-data port for canonical event streams."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Protocol

from hindsight.core.events import CanonicalEvent


class MarketDataPort(Protocol):
    """Streams canonical market events for one symbol/date slice."""

    def stream_events(
        self,
        *,
        symbol: str,
        date: str | None,
        limit: int,
    ) -> Iterator[CanonicalEvent]: ...
