"""Leakage probes for Hindsight benchmarks."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from hindsight.evaluation.walk_forward import LabelInterval, WalkForwardFold

Severity = Literal["hard", "soft"]


@dataclass(frozen=True, slots=True)
class LeakageViolation:
    """One leakage finding."""

    probe: str
    severity: Severity
    message: str
    indices: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class LeakageReport:
    """Leakage audit output."""

    violations: tuple[LeakageViolation, ...]

    @property
    def has_hard_violation(self) -> bool:
        return any(violation.severity == "hard" for violation in self.violations)

    def raise_for_hard_violations(self) -> None:
        hard = [violation for violation in self.violations if violation.severity == "hard"]
        if hard:
            names = ", ".join(violation.probe for violation in hard)
            raise LeakageError(f"hard leakage violation(s): {names}")


class LeakageError(RuntimeError):
    """Raised when a benchmark has hard leakage."""


def audit_leakage(
    *,
    folds: tuple[WalkForwardFold, ...],
    intervals: list[LabelInterval],
    feature_names: tuple[str, ...],
    target_name: str,
    normalizer_fit_indices: tuple[int, ...],
    baseline_predictions: tuple[float, ...],
    perturbed_predictions: tuple[float, ...],
    perturbation_tolerance: float,
) -> LeakageReport:
    violations: list[LeakageViolation] = []
    violations.extend(probe_target_leakage(feature_names, target_name))
    violations.extend(probe_full_sample_normalization(folds, normalizer_fit_indices))
    violations.extend(probe_temporal_overlap(folds, intervals))
    violations.extend(
        probe_lookahead_perturbation(
            baseline_predictions=baseline_predictions,
            perturbed_predictions=perturbed_predictions,
            tolerance=perturbation_tolerance,
        )
    )
    return LeakageReport(tuple(violations))


def probe_target_leakage(
    feature_names: tuple[str, ...],
    target_name: str,
) -> tuple[LeakageViolation, ...]:
    normalized_target = _normalize_name(target_name)
    bad_indices = tuple(
        index
        for index, name in enumerate(feature_names)
        if _normalize_name(name) == normalized_target
        or _normalize_name(name).startswith(f"{normalized_target}_")
    )
    if not bad_indices:
        return ()
    return (
        LeakageViolation(
            probe="target_leakage",
            severity="hard",
            message="feature set contains target-derived columns",
            indices=bad_indices,
        ),
    )


def probe_full_sample_normalization(
    folds: tuple[WalkForwardFold, ...],
    normalizer_fit_indices: tuple[int, ...],
) -> tuple[LeakageViolation, ...]:
    fit_set = set(normalizer_fit_indices)
    violations: list[LeakageViolation] = []
    for fold in folds:
        test_leaks = tuple(index for index in fold.test_indices if index in fit_set)
        if test_leaks:
            violations.append(
                LeakageViolation(
                    probe="full_sample_normalization",
                    severity="hard",
                    message="normalizer fit includes test samples",
                    indices=test_leaks,
                )
            )
    return tuple(violations)


def probe_temporal_overlap(
    folds: tuple[WalkForwardFold, ...],
    intervals: list[LabelInterval],
) -> tuple[LeakageViolation, ...]:
    by_index = {interval.index: interval for interval in intervals}
    violations: list[LeakageViolation] = []
    for fold in folds:
        test_intervals = [_required_interval(by_index, index) for index in fold.test_indices]
        leaked: list[int] = []
        for train_index in fold.train_indices:
            train_interval = _required_interval(by_index, train_index)
            if any(_overlaps(train_interval, test_interval) for test_interval in test_intervals):
                leaked.append(train_index)
        if leaked:
            violations.append(
                LeakageViolation(
                    probe="temporal_overlap",
                    severity="hard",
                    message="train label interval overlaps a test label interval",
                    indices=tuple(leaked),
                )
            )
    return tuple(violations)


def probe_lookahead_perturbation(
    *,
    baseline_predictions: tuple[float, ...],
    perturbed_predictions: tuple[float, ...],
    tolerance: float,
) -> tuple[LeakageViolation, ...]:
    if len(baseline_predictions) != len(perturbed_predictions):
        raise ValueError("prediction vectors must have the same length")
    if tolerance < 0:
        raise ValueError("tolerance cannot be negative")
    changed = tuple(
        index
        for index, (before, after) in enumerate(
            zip(baseline_predictions, perturbed_predictions, strict=True)
        )
        if abs(before - after) > tolerance
    )
    if not changed:
        return ()
    return (
        LeakageViolation(
            probe="lookahead_perturbation",
            severity="hard",
            message="predictions changed when only future information was perturbed",
            indices=changed,
        ),
    )


def _normalize_name(value: str) -> str:
    return value.strip().lower()


def _required_interval(by_index: dict[int, LabelInterval], index: int) -> LabelInterval:
    try:
        return by_index[index]
    except KeyError as exc:
        raise ValueError(f"fold references unknown sample index: {index}") from exc


def _overlaps(left: LabelInterval, right: LabelInterval) -> bool:
    return left.start <= right.end and right.start <= left.end
