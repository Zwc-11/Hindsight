from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

import hindsight.evaluation
import hindsight.pit
import hindsight.portfolio
from hindsight.core.clock import ReplayClock
from hindsight.core.cursor import EventCursor
from hindsight.core.hashing import run_hash, stable_hash


@dataclass(frozen=True, slots=True)
class TinyEvent:
    timestamp: datetime
    sequence: int
    event_id: str | None


NOW = datetime(2026, 1, 1, tzinfo=UTC)


def test_replay_clock_rejects_backwards_time() -> None:
    clock = ReplayClock()
    clock.advance(NOW + timedelta(seconds=1))
    with pytest.raises(ValueError, match="monotonic"):
        clock.advance(NOW)


def test_event_cursor_tie_breaks_by_timestamp_sequence_and_event_id() -> None:
    events = [
        TinyEvent(timestamp=NOW, sequence=2, event_id="b"),
        TinyEvent(timestamp=NOW, sequence=1, event_id="c"),
        TinyEvent(timestamp=NOW, sequence=1, event_id="a"),
        TinyEvent(timestamp=NOW - timedelta(seconds=1), sequence=9, event_id="z"),
    ]
    ordered = list(EventCursor(events))
    assert [event.event_id for event in ordered] == ["z", "a", "c", "b"]
    assert len(EventCursor(events)) == 4


def test_hashes_are_stable_and_key_order_independent() -> None:
    first = stable_hash({"b": 2, "a": 1})
    second = stable_hash({"a": 1, "b": 2})
    assert first == second
    assert run_hash([{"event_id": "1", "timestamp": NOW.isoformat()}]) == run_hash(
        [{"timestamp": NOW.isoformat(), "event_id": "1"}]
    )


def test_scaffold_packages_import() -> None:
    assert hindsight.evaluation is not None
    assert hindsight.pit is not None
    assert hindsight.portfolio is not None
