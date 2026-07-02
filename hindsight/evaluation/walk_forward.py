"""Purged and embargoed walk-forward folds."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass(frozen=True, slots=True)
class LabelInterval:
    """A sample's feature timestamp and label availability interval."""

    index: int
    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        if self.end < self.start:
            raise ValueError("label interval end must be >= start")


@dataclass(frozen=True, slots=True)
class WalkForwardFold:
    """One walk-forward train/test split."""

    fold_id: int
    train_indices: tuple[int, ...]
    test_indices: tuple[int, ...]
    test_start: datetime
    test_end: datetime


def purged_walk_forward_folds(
    intervals: list[LabelInterval],
    *,
    n_folds: int,
    train_window: int,
    test_window: int,
    purge: timedelta,
    embargo: timedelta,
) -> tuple[WalkForwardFold, ...]:
    """Build count-based walk-forward folds with time purge and embargo."""
    if n_folds < 1:
        raise ValueError("n_folds must be positive")
    if train_window < 1:
        raise ValueError("train_window must be positive")
    if test_window < 1:
        raise ValueError("test_window must be positive")
    ordered = sorted(intervals, key=lambda item: (item.start, item.end, item.index))
    _assert_unique_indices(ordered)
    folds: list[WalkForwardFold] = []
    for fold_id in range(n_folds):
        test_start_pos = train_window + fold_id * test_window
        test_end_pos = test_start_pos + test_window
        if test_end_pos > len(ordered):
            raise ValueError("not enough samples for requested folds")
        test_samples = ordered[test_start_pos:test_end_pos]
        test_start = test_samples[0].start
        test_end = max(sample.end for sample in test_samples)
        purge_start = test_start - purge
        embargo_end = test_end + embargo
        eligible_train_samples = [
            sample
            for sample in ordered[:test_start_pos]
            if sample.end < purge_start or sample.start > embargo_end
        ]
        train_samples = eligible_train_samples[-train_window:]
        if not train_samples:
            raise ValueError("purge and embargo removed every training sample")
        folds.append(
            WalkForwardFold(
                fold_id=fold_id,
                train_indices=tuple(sample.index for sample in train_samples),
                test_indices=tuple(sample.index for sample in test_samples),
                test_start=test_start,
                test_end=test_end,
            )
        )
    return tuple(folds)


def _assert_unique_indices(intervals: list[LabelInterval]) -> None:
    seen: set[int] = set()
    for interval in intervals:
        if interval.index in seen:
            raise ValueError(f"duplicate sample index: {interval.index}")
        seen.add(interval.index)
