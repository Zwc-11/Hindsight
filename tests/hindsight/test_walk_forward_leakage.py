from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from hindsight.evaluation.leakage import (
    LeakageError,
    audit_leakage,
    probe_lookahead_perturbation,
    probe_target_leakage,
)
from hindsight.evaluation.walk_forward import (
    LabelInterval,
    WalkForwardFold,
    purged_walk_forward_folds,
)


def interval(index: int, *, label_seconds: int = 90) -> LabelInterval:
    start = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=index)
    return LabelInterval(index=index, start=start, end=start + timedelta(seconds=label_seconds))


def test_purged_walk_forward_removes_nearby_training_samples() -> None:
    folds = purged_walk_forward_folds(
        [interval(index) for index in range(10)],
        n_folds=2,
        train_window=4,
        test_window=2,
        purge=timedelta(seconds=15),
        embargo=timedelta(seconds=30),
    )

    assert folds[0].test_indices == (4, 5)
    assert folds[0].train_indices == (0, 1, 2)
    assert folds[1].test_indices == (6, 7)
    assert folds[1].train_indices == (1, 2, 3, 4)


def test_purged_walk_forward_rejects_duplicate_indices() -> None:
    samples = [interval(0), interval(0)]

    with pytest.raises(ValueError, match="duplicate sample index"):
        purged_walk_forward_folds(
            samples,
            n_folds=1,
            train_window=1,
            test_window=1,
            purge=timedelta(0),
            embargo=timedelta(0),
        )


def test_leakage_audit_finds_hard_violations() -> None:
    intervals = [interval(0, label_seconds=120), interval(1, label_seconds=120)]
    folds = (
        WalkForwardFold(
            fold_id=0,
            train_indices=(0,),
            test_indices=(1,),
            test_start=intervals[1].start,
            test_end=intervals[1].end,
        ),
    )

    report = audit_leakage(
        folds=folds,
        intervals=intervals,
        feature_names=("ofi", "target_10s"),
        target_name="target",
        normalizer_fit_indices=(0, 1),
        baseline_predictions=(0.1, 0.2),
        perturbed_predictions=(0.1, 0.7),
        perturbation_tolerance=0.01,
    )

    assert report.has_hard_violation
    assert {violation.probe for violation in report.violations} == {
        "target_leakage",
        "full_sample_normalization",
        "temporal_overlap",
        "lookahead_perturbation",
    }
    with pytest.raises(LeakageError):
        report.raise_for_hard_violations()


def test_probe_target_leakage_allows_unrelated_features() -> None:
    assert probe_target_leakage(("ofi", "spread_bps"), "toxic") == ()


def test_probe_lookahead_perturbation_validates_lengths() -> None:
    with pytest.raises(ValueError, match="same length"):
        probe_lookahead_perturbation(
            baseline_predictions=(0.1,),
            perturbed_predictions=(0.1, 0.2),
            tolerance=0.0,
        )
