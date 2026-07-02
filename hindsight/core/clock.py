"""Monotonic replay clock."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(slots=True)
class ReplayClock:
    """Tracks replay time and rejects backwards movement."""

    current: datetime | None = None

    def advance(self, timestamp: datetime) -> datetime:
        if self.current is not None and timestamp < self.current:
            raise ValueError("replay timestamps must be monotonic")
        self.current = timestamp
        return timestamp
