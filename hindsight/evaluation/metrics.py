"""Benchmark metrics for Hindsight."""

from __future__ import annotations

from bisect import bisect_left
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from itertools import combinations
from math import erf, log, sqrt

from hindsight.core.events import Side
from hindsight.core.types import EquityPoint, Fill
from hindsight.labels.markout import markout_bps


@dataclass(frozen=True, slots=True)
class MarkoutObservation:
    """Realized markout for one fill."""

    fill_id: str
    timestamp: datetime
    markout_bps: float


@dataclass(frozen=True, slots=True)
class MetricWithCI:
    """A metric with a normal-approximation confidence interval."""

    value: float
    lower: float
    upper: float


def equity_returns(points: Sequence[EquityPoint]) -> tuple[float, ...]:
    if len(points) < 2:
        raise ValueError("at least two equity points are required")
    returns: list[float] = []
    for previous, current in zip(points, points[1:], strict=False):
        if previous.equity == 0:
            raise ValueError("cannot compute return from zero equity")
        returns.append((current.equity - previous.equity) / previous.equity)
    return tuple(returns)


def sharpe_ratio(returns: Sequence[float]) -> float:
    if len(returns) < 2:
        raise ValueError("at least two returns are required")
    mean = sum(returns) / len(returns)
    variance = sum((value - mean) ** 2 for value in returns) / (len(returns) - 1)
    if variance == 0:
        raise ValueError("Sharpe is undefined for zero-variance returns")
    return mean / sqrt(variance)


def max_drawdown(points: Sequence[EquityPoint]) -> float:
    if not points:
        raise ValueError("at least one equity point is required")
    peak = points[0].equity
    worst = 0.0
    for point in points:
        peak = max(peak, point.equity)
        if peak == 0:
            raise ValueError("cannot compute drawdown from zero equity")
        worst = min(worst, (point.equity - peak) / peak)
    return abs(worst)


def turnover_notional(fills: Sequence[Fill]) -> float:
    return sum(abs(fill.price * fill.quantity) for fill in fills)


def total_fees(fills: Sequence[Fill]) -> float:
    return sum(fill.fee for fill in fills)


def realized_markouts_bps(
    fills: Sequence[Fill],
    mid_series: Sequence[tuple[datetime, float]],
    *,
    horizon: timedelta,
) -> tuple[MarkoutObservation, ...]:
    if horizon <= timedelta(0):
        raise ValueError("horizon must be positive")
    if not fills:
        raise ValueError("at least one fill is required")
    if not mid_series:
        raise ValueError("mid series cannot be empty")
    ordered_mids = tuple(sorted(mid_series, key=lambda item: item[0]))
    mid_times = tuple(item[0] for item in ordered_mids)
    observations: list[MarkoutObservation] = []
    for fill in fills:
        target_time = fill.timestamp + horizon
        mid_index = bisect_left(mid_times, target_time)
        if mid_index >= len(ordered_mids):
            raise ValueError(f"missing future mid for fill {fill.fill_id}")
        future_mid = ordered_mids[mid_index][1]
        side_sign = 1 if fill.side == Side.BUY else -1
        observations.append(
            MarkoutObservation(
                fill_id=fill.fill_id,
                timestamp=fill.timestamp,
                markout_bps=markout_bps(fill.price, future_mid, side_sign),
            )
        )
    return tuple(observations)


def markout_lift(
    strategy_markouts: Sequence[float],
    baseline_markouts: Sequence[float],
    *,
    confidence_z: float = 1.96,
) -> MetricWithCI:
    if len(strategy_markouts) != len(baseline_markouts):
        raise ValueError("strategy and baseline markouts must be paired")
    if not strategy_markouts:
        raise ValueError("at least one paired markout is required")
    deltas = [
        strategy - baseline
        for strategy, baseline in zip(strategy_markouts, baseline_markouts, strict=True)
    ]
    mean = sum(deltas) / len(deltas)
    if len(deltas) == 1:
        return MetricWithCI(value=mean, lower=mean, upper=mean)
    variance = sum((value - mean) ** 2 for value in deltas) / (len(deltas) - 1)
    stderr = sqrt(variance / len(deltas))
    return MetricWithCI(
        value=mean,
        lower=mean - confidence_z * stderr,
        upper=mean + confidence_z * stderr,
    )


def deflated_sharpe_probability(returns: Sequence[float], *, trials: int) -> float:
    if trials < 1:
        raise ValueError("trials must be positive")
    sr = sharpe_ratio(returns)
    count = len(returns)
    if count < 3:
        raise ValueError("at least three returns are required for deflated Sharpe")
    skew = _central_moment(returns, 3) / (_central_moment(returns, 2) ** 1.5)
    kurtosis = _central_moment(returns, 4) / (_central_moment(returns, 2) ** 2)
    expected_max_sr = sqrt(2 * log(trials) / count) if trials > 1 else 0.0
    variance_term = 1 - skew * sr + ((kurtosis - 1) / 4) * sr**2
    if variance_term <= 0:
        raise ValueError("deflated Sharpe variance term must be positive")
    z_score = (sr - expected_max_sr) * sqrt((count - 1) / variance_term)
    return _normal_cdf(z_score)


def probability_of_backtest_overfit(
    strategy_fold_scores: Mapping[str, Sequence[float]],
) -> float:
    if len(strategy_fold_scores) < 2:
        raise ValueError("at least two strategies are required")
    strategy_names = tuple(strategy_fold_scores.keys())
    fold_count = _consistent_fold_count(strategy_fold_scores)
    if fold_count < 4 or fold_count % 2 != 0:
        raise ValueError("CSCV requires an even fold count of at least four")
    half = fold_count // 2
    overfit_count = 0
    split_count = 0
    all_fold_indices = tuple(range(fold_count))
    for train_indices in combinations(all_fold_indices, half):
        train_set = set(train_indices)
        test_indices = tuple(index for index in all_fold_indices if index not in train_set)
        best_name = max(
            strategy_names,
            key=lambda name: _mean_at_indices(strategy_fold_scores[name], train_indices),
        )
        test_scores = {
            name: _mean_at_indices(strategy_fold_scores[name], test_indices)
            for name in strategy_names
        }
        rank = 1 + sum(score < test_scores[best_name] for score in test_scores.values())
        percentile = rank / (len(strategy_names) + 1)
        if percentile < 0.5:
            overfit_count += 1
        split_count += 1
    return overfit_count / split_count


def reconcile_markouts(
    computed: Sequence[MarkoutObservation],
    expected_by_fill_id: Mapping[str, float],
    *,
    tolerance_bps: float,
) -> None:
    if tolerance_bps < 0:
        raise ValueError("tolerance_bps cannot be negative")
    for observation in computed:
        if observation.fill_id not in expected_by_fill_id:
            raise ValueError(f"missing expected markout for fill {observation.fill_id}")
        expected = expected_by_fill_id[observation.fill_id]
        if abs(observation.markout_bps - expected) > tolerance_bps:
            raise ValueError(
                f"markout mismatch for fill {observation.fill_id}: "
                f"{observation.markout_bps} != {expected}"
            )


def _central_moment(values: Sequence[float], power: int) -> float:
    mean = sum(values) / len(values)
    return sum((value - mean) ** power for value in values) / len(values)


def _normal_cdf(value: float) -> float:
    return 0.5 * (1 + erf(value / sqrt(2)))


def _consistent_fold_count(strategy_fold_scores: Mapping[str, Sequence[float]]) -> int:
    counts = {len(scores) for scores in strategy_fold_scores.values()}
    if len(counts) != 1:
        raise ValueError("all strategies must have the same number of fold scores")
    return counts.pop()


def _mean_at_indices(values: Sequence[float], indices: Sequence[int]) -> float:
    return sum(values[index] for index in indices) / len(indices)
