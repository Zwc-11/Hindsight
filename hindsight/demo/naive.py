"""Deliberately unsafe demo-only fold builders."""

from __future__ import annotations

import random
from datetime import datetime

from hindsight.evaluation.walk_forward import LabelInterval, WalkForwardFold


def naive_random_folds(
    intervals: list[LabelInterval],
    *,
    n_folds: int,
    test_window: int,
    seed: int,
) -> tuple[WalkForwardFold, ...]:
    """Build random folds with no purge, embargo, or temporal ordering.

    This is an intentionally unsafe control for the public demo. It exists to
    show why Hindsight's leakage checks matter, not as a recommended evaluator.
    """
    if n_folds < 1:
        raise ValueError("n_folds must be positive")
    if test_window < 1:
        raise ValueError("test_window must be positive")
    if len(intervals) < n_folds * test_window + 1:
        raise ValueError("not enough samples for requested naive folds")

    by_index = {interval.index: interval for interval in intervals}
    if len(by_index) != len(intervals):
        raise ValueError("interval indices must be unique")

    shuffled_indices = [interval.index for interval in intervals]
    random.Random(seed).shuffle(shuffled_indices)

    folds: list[WalkForwardFold] = []
    for fold_id in range(n_folds):
        start = fold_id * test_window
        test_indices = tuple(shuffled_indices[start : start + test_window])
        train_indices = tuple(index for index in shuffled_indices if index not in test_indices)
        test_start, test_end = _test_bounds(by_index, test_indices)
        folds.append(
            WalkForwardFold(
                fold_id=fold_id,
                train_indices=train_indices,
                test_indices=test_indices,
                test_start=test_start,
                test_end=test_end,
            )
        )
    return tuple(folds)


def _test_bounds(
    by_index: dict[int, LabelInterval],
    test_indices: tuple[int, ...],
) -> tuple[datetime, datetime]:
    test_intervals = [by_index[index] for index in test_indices]
    return (
        min(interval.start for interval in test_intervals),
        max(interval.end for interval in test_intervals),
    )
