"""Strategy protocol and the M0 no-op implementation."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from hindsight.core.events import CanonicalEvent
from hindsight.core.types import Fill, OrderIntent
from hindsight.pit.view import PointInTimeView


class Strategy(Protocol):
    """Lifecycle hooks used by the Hindsight execution layer."""

    name: str

    def on_start(self) -> None: ...

    def on_event(self, event: CanonicalEvent, view: PointInTimeView) -> Sequence[OrderIntent]: ...

    def on_fill(self, fill: Fill) -> None: ...

    def on_finish(self) -> None: ...


class NoopStrategy:
    """A strategy that observes events and emits no orders."""

    name = "noop"

    def on_start(self) -> None:
        return None

    def on_event(self, event: CanonicalEvent, view: PointInTimeView) -> Sequence[OrderIntent]:
        return ()

    def on_fill(self, fill: Fill) -> None:
        return None

    def on_finish(self) -> None:
        return None
