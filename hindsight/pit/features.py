"""Point-in-time feature joins."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from hindsight.pit.view import LookaheadError


@dataclass(frozen=True, slots=True)
class FeatureRow:
    """Feature values materialized at a timestamp."""

    timestamp: datetime
    values: dict[str, float]


def asof_features(
    rows: list[FeatureRow],
    *,
    timestamp: datetime,
    feature_lag: timedelta = timedelta(minutes=1),
) -> FeatureRow:
    """Return the latest feature row available before `timestamp - feature_lag`."""

    if feature_lag <= timedelta(0):
        raise ValueError("feature_lag must be positive")
    cutoff = timestamp - feature_lag
    eligible = [row for row in rows if row.timestamp <= cutoff]
    if not eligible:
        raise LookaheadError("no point-in-time-safe feature row is available")
    return max(eligible, key=lambda row: row.timestamp)
