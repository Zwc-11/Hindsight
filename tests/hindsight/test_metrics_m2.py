from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from hindsight.core.events import Side
from hindsight.core.types import EquityPoint, Fill
from hindsight.evaluation.metrics import (
    deflated_sharpe_probability,
    equity_returns,
    markout_lift,
    max_drawdown,
    probability_of_backtest_overfit,
    realized_markouts_bps,
    reconcile_markouts,
    total_fees,
    turnover_notional,
)

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def equity_point(offset: int, equity: float) -> EquityPoint:
    return EquityPoint(
        timestamp=NOW + timedelta(seconds=offset),
        equity=equity,
        cash=equity,
        position_quantity=0,
        mark_price=100,
    )


def fill(fill_id: str, offset: int, side: Side, price: float) -> Fill:
    return Fill(
        fill_id=fill_id,
        order_id=fill_id,
        symbol="BTC-PERP",
        timestamp=NOW + timedelta(seconds=offset),
        side=side,
        price=price,
        quantity=2,
        fee=0.5,
        liquidity="maker",
        flags=(),
    )


def test_equity_return_drawdown_and_cost_metrics() -> None:
    points = [equity_point(0, 100), equity_point(1, 110), equity_point(2, 99)]
    fills = [fill("a", 0, Side.BUY, 100), fill("b", 1, Side.SELL, 101)]

    assert equity_returns(points) == pytest.approx((0.1, -0.1))
    assert max_drawdown(points) == pytest.approx(0.1)
    assert turnover_notional(fills) == pytest.approx(402)
    assert total_fees(fills) == pytest.approx(1.0)


def test_realized_markouts_use_first_mid_after_horizon() -> None:
    fills = [fill("buy", 0, Side.BUY, 100), fill("sell", 0, Side.SELL, 100)]
    mids = [
        (NOW + timedelta(seconds=5), 100),
        (NOW + timedelta(seconds=10), 101),
        (NOW + timedelta(seconds=11), 99),
    ]

    observations = realized_markouts_bps(
        fills,
        mids,
        horizon=timedelta(seconds=10),
    )

    assert [item.markout_bps for item in observations] == pytest.approx((100, -100))


def test_realized_markouts_raise_when_future_mid_is_missing() -> None:
    with pytest.raises(ValueError, match="missing future mid"):
        realized_markouts_bps(
            [fill("late", 0, Side.BUY, 100)],
            [(NOW + timedelta(seconds=5), 101)],
            horizon=timedelta(seconds=10),
        )


def test_markout_lift_returns_confidence_interval() -> None:
    lift = markout_lift([2, 4, 6], [1, 1, 1])

    assert lift.value == pytest.approx(3)
    assert lift.lower < lift.value < lift.upper


def test_deflated_sharpe_probability_is_bounded() -> None:
    probability = deflated_sharpe_probability([0.01, -0.02, 0.03, 0.01, 0.04], trials=4)

    assert 0 <= probability <= 1


def test_probability_of_backtest_overfit_detects_train_winner_losing_out_of_sample() -> None:
    pbo = probability_of_backtest_overfit(
        {
            "overfit": (10, 10, -10, -10),
            "stable": (1, 1, 1, 1),
        }
    )

    assert pbo > 0


def test_reconcile_markouts_raises_on_mismatch() -> None:
    observations = realized_markouts_bps(
        [fill("buy", 0, Side.BUY, 100)],
        [(NOW + timedelta(seconds=10), 101)],
        horizon=timedelta(seconds=10),
    )

    with pytest.raises(ValueError, match="markout mismatch"):
        reconcile_markouts(observations, {"buy": 99}, tolerance_bps=0.1)
