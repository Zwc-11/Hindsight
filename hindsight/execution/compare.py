"""Naive-vs-realistic backtest comparison."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from math import sqrt

from hindsight.core.events import CanonicalEvent
from hindsight.execution.config import ExecConfig
from hindsight.execution.simulator import ExecutionSimulator, SimulationResult
from hindsight.strategy.base import Strategy


@dataclass(frozen=True, slots=True)
class ComparisonResult:
    """Two runs of the same strategy under different execution assumptions."""

    naive: SimulationResult
    realistic: SimulationResult
    naive_sharpe: float
    realistic_sharpe: float
    sharpe_delta: float
    verdict: str


def compare_naive_vs_realistic(
    *,
    events: list[CanonicalEvent],
    strategy_factory: Callable[[], Strategy],
    symbol: str,
    naive_config: ExecConfig,
    realistic_config: ExecConfig,
) -> ComparisonResult:
    naive = ExecutionSimulator(naive_config).run(
        events=events,
        strategy=strategy_factory(),
        symbol=symbol,
    )
    realistic = ExecutionSimulator(realistic_config).run(
        events=events,
        strategy=strategy_factory(),
        symbol=symbol,
    )
    naive_sharpe = sharpe_ratio([point.equity for point in naive.equity_curve])
    realistic_sharpe = sharpe_ratio([point.equity for point in realistic.equity_curve])
    delta = realistic_sharpe - naive_sharpe
    equity_delta = realistic.final_state.equity - naive.final_state.equity
    verdict = (
        f"Realistic execution reduced final equity by {abs(equity_delta):.2f}."
        if equity_delta < 0
        else f"Realistic execution improved final equity by {equity_delta:.2f}."
    )
    return ComparisonResult(
        naive=naive,
        realistic=realistic,
        naive_sharpe=naive_sharpe,
        realistic_sharpe=realistic_sharpe,
        sharpe_delta=delta,
        verdict=verdict,
    )


def sharpe_ratio(equity_values: list[float]) -> float:
    if len(equity_values) < 3:
        raise ValueError("at least three equity points are required for Sharpe")
    returns = [
        (equity_values[index] - equity_values[index - 1]) / equity_values[index - 1]
        for index in range(1, len(equity_values))
    ]
    mean = sum(returns) / len(returns)
    variance = sum((value - mean) ** 2 for value in returns) / (len(returns) - 1)
    if variance == 0:
        raise ValueError("Sharpe is undefined for a zero-variance equity curve")
    return mean / sqrt(variance) * sqrt(len(returns))
