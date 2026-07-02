from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest

from hindsight.core.clock import ReplayClock
from hindsight.core.events import (
    AggTradeEvent,
    BookDepthEvent,
    BookTickerEvent,
    CanonicalEvent,
    KlineEvent,
    Side,
)
from hindsight.core.types import Fill, OrderIntent, OrderType, TimeInForce
from hindsight.execution.config import ExecConfig
from hindsight.execution.funding import funding_payment
from hindsight.execution.simulator import ExecutionSimulator
from hindsight.execution.slippage import apply_slippage
from hindsight.pit.view import PointInTimeView
from hindsight.portfolio.accounting import Portfolio
from hindsight.strategy.base import NoopStrategy
from hindsight.strategy.baselines.momentum import MomentumStrategy

NOW = datetime(2026, 1, 1, tzinfo=UTC)


@dataclass(slots=True)
class EmitOnceStrategy:
    order_type: OrderType
    side: Side
    quantity: float
    limit_price: float | None = None
    name: str = "emit_once"
    fills_seen: list[Fill] = field(default_factory=list)
    emitted: bool = False

    def on_start(self) -> None:
        self.emitted = False
        self.fills_seen.clear()

    def on_event(self, event: CanonicalEvent, view: PointInTimeView) -> Sequence[OrderIntent]:
        if self.emitted or not isinstance(event, KlineEvent):
            return ()
        self.emitted = True
        return (
            OrderIntent(
                order_id="order-1",
                symbol="BTCUSDT",
                timestamp=event.timestamp,
                side=self.side,
                quantity=self.quantity,
                order_type=self.order_type,
                time_in_force=TimeInForce.IOC,
                limit_price=self.limit_price,
                strategy_name=self.name,
            ),
        )

    def on_fill(self, fill: Fill) -> None:
        self.fills_seen.append(fill)

    def on_finish(self) -> None:
        return None


def config(
    *,
    latency_ms: int = 0,
    participation_cap: float = 1.0,
    funding_rate_bps: float | None = 0.0,
    funding_interval_hours: int = 8,
) -> ExecConfig:
    return ExecConfig(
        engine_version="0.1.0",
        initial_cash=100_000,
        maker_fee_bps=2.0,
        taker_fee_bps=10.0,
        slippage_impact_bps=1.0,
        latency_ms=latency_ms,
        funding_rate_bps=funding_rate_bps,
        funding_interval_hours=funding_interval_hours,
        participation_cap=participation_cap,
        seed=0,
    )


def kline(minutes: int, close: float) -> KlineEvent:
    timestamp = NOW + timedelta(minutes=minutes)
    return KlineEvent(
        symbol="BTCUSDT",
        timestamp=timestamp,
        sequence=minutes,
        interval="1m",
        open_time=timestamp,
        close_time=timestamp,
        open_price=close,
        high_price=close + 1,
        low_price=close - 1,
        close_price=close,
        volume=1,
        trade_count=1,
    )


def agg_trade(seconds: int, price: float, quantity: float, sequence: int) -> AggTradeEvent:
    return AggTradeEvent(
        symbol="BTCUSDT",
        timestamp=NOW + timedelta(seconds=seconds),
        sequence=sequence,
        aggregate_trade_id=sequence,
        price=price,
        quantity=quantity,
        first_trade_id=sequence,
        last_trade_id=sequence,
        is_buyer_maker=True,
    )


def book(minutes: int) -> BookTickerEvent:
    timestamp = NOW + timedelta(minutes=minutes)
    return BookTickerEvent(
        symbol="BTCUSDT",
        timestamp=timestamp,
        sequence=100 + minutes,
        update_id=minutes,
        bid_price=99,
        bid_quantity=1,
        ask_price=101,
        ask_quantity=1,
    )


def test_slippage_and_funding_helpers_validate_and_sign_values() -> None:
    assert apply_slippage(price=100, side=Side.BUY, quantity=2, impact_bps_per_unit=5) == 100.1
    assert apply_slippage(price=100, side=Side.SELL, quantity=2, impact_bps_per_unit=5) == 99.9
    assert funding_payment(position_quantity=2, mark_price=100, funding_rate_bps=10).payment == 0.2
    short_funding = funding_payment(position_quantity=-2, mark_price=100, funding_rate_bps=10)
    assert short_funding.payment == -0.2
    missing = funding_payment(position_quantity=2, mark_price=100, funding_rate_bps=None)
    assert missing.payment == 0
    assert missing.warning == "funding_missing"
    with pytest.raises(ValueError, match="price"):
        apply_slippage(price=0, side=Side.BUY, quantity=1, impact_bps_per_unit=1)
    with pytest.raises(ValueError, match="quantity"):
        apply_slippage(price=100, side=Side.BUY, quantity=0, impact_bps_per_unit=1)
    with pytest.raises(ValueError, match="impact"):
        apply_slippage(price=100, side=Side.BUY, quantity=1, impact_bps_per_unit=-1)
    with pytest.raises(ValueError, match="mark_price"):
        funding_payment(position_quantity=1, mark_price=0, funding_rate_bps=1)


def test_portfolio_accounts_for_fills_fees_realized_pnl_and_funding() -> None:
    portfolio = Portfolio(initial_cash=1_000, symbol="BTCUSDT")
    buy = Fill("f1", "o1", "BTCUSDT", NOW, Side.BUY, 100, 2, 1, "taker", ())
    sell = Fill("f2", "o2", "BTCUSDT", NOW, Side.SELL, 110, 1, 1, "maker", ())
    portfolio.apply_fill(buy)
    portfolio.apply_fill(sell)
    portfolio.apply_funding(0.5)
    state = portfolio.mark(timestamp=NOW, mark_price=120)
    assert state.position.quantity == 1
    assert state.realized_pnl == 10
    assert state.unrealized_pnl == 20
    assert state.fees_paid == 2
    assert state.funding_paid == 0.5
    assert portfolio.cash == pytest.approx(907.5)
    assert portfolio.quantity == 1
    point = portfolio.equity_point(timestamp=NOW, mark_price=120)
    assert point.equity == state.equity
    assert portfolio.state().equity == point.equity
    with pytest.raises(ValueError, match="initial_cash"):
        Portfolio(initial_cash=0, symbol="BTCUSDT")
    with pytest.raises(ValueError, match="uppercase"):
        Portfolio(initial_cash=1_000, symbol="btcusdt")
    with pytest.raises(ValueError, match="symbol"):
        portfolio.apply_fill(Fill("f3", "o3", "ETHUSDT", NOW, Side.BUY, 100, 1, 0, "taker", ()))
    with pytest.raises(ValueError, match="mark_price"):
        portfolio.mark(timestamp=NOW, mark_price=0)
    with pytest.raises(ValueError, match="marked"):
        Portfolio(initial_cash=1_000, symbol="BTCUSDT").state()


def test_portfolio_handles_short_cover_and_reversal() -> None:
    portfolio = Portfolio(initial_cash=1_000, symbol="BTCUSDT")
    portfolio.apply_fill(Fill("s1", "o1", "BTCUSDT", NOW, Side.SELL, 100, 2, 0, "maker", ()))
    portfolio.apply_fill(Fill("b1", "o2", "BTCUSDT", NOW, Side.BUY, 90, 1, 0, "taker", ()))
    state = portfolio.mark(timestamp=NOW, mark_price=95)
    assert state.realized_pnl == 10
    assert state.position.quantity == -1
    portfolio.apply_fill(Fill("b2", "o3", "BTCUSDT", NOW, Side.BUY, 80, 2, 0, "taker", ()))
    reversed_state = portfolio.mark(timestamp=NOW, mark_price=85)
    assert reversed_state.position.quantity == 1
    assert reversed_state.position.average_entry_price == 80


def test_market_order_fills_after_latency_using_kline_fallback_flag() -> None:
    strategy = EmitOnceStrategy(order_type=OrderType.MARKET, side=Side.BUY, quantity=1)
    result = ExecutionSimulator(config(latency_ms=30_000)).run(
        events=[kline(0, 100), kline(1, 101)],
        strategy=strategy,
        symbol="BTCUSDT",
    )
    assert len(result.fills) == 1
    fill = result.fills[0]
    assert fill.timestamp == NOW + timedelta(minutes=1)
    assert fill.price > 101
    assert fill.liquidity == "taker"
    assert fill.flags == ("top_of_book_missing",)
    assert strategy.fills_seen == [fill]
    assert "top_of_book_missing" in result.warnings


def test_order_does_not_fill_before_active_time() -> None:
    strategy = EmitOnceStrategy(order_type=OrderType.MARKET, side=Side.BUY, quantity=1)
    result = ExecutionSimulator(config(latency_ms=90_000)).run(
        events=[kline(0, 100), kline(1, 101)],
        strategy=strategy,
        symbol="BTCUSDT",
    )
    assert result.fills == ()
    assert result.orders_emitted == 1


def test_market_order_uses_book_ticker_touch_when_available() -> None:
    strategy = EmitOnceStrategy(order_type=OrderType.MARKET, side=Side.SELL, quantity=1)
    result = ExecutionSimulator(config(latency_ms=30_000)).run(
        events=[kline(0, 100), book(1)],
        strategy=strategy,
        symbol="BTCUSDT",
    )
    assert len(result.fills) == 1
    assert result.fills[0].price < 99
    assert result.fills[0].flags == ()


def test_market_order_ignores_trade_events_until_price_source_available() -> None:
    strategy = EmitOnceStrategy(order_type=OrderType.MARKET, side=Side.BUY, quantity=1)
    result = ExecutionSimulator(config(latency_ms=30_000)).run(
        events=[kline(0, 100), agg_trade(60, price=99, quantity=1, sequence=10), kline(2, 101)],
        strategy=strategy,
        symbol="BTCUSDT",
    )
    assert len(result.fills) == 1
    assert result.fills[0].timestamp == NOW + timedelta(minutes=2)


def test_limit_order_trade_through_partials_accumulate_with_maker_fee() -> None:
    strategy = EmitOnceStrategy(
        order_type=OrderType.LIMIT,
        side=Side.BUY,
        quantity=1,
        limit_price=100,
    )
    result = ExecutionSimulator(config(participation_cap=0.25)).run(
        events=[
            kline(0, 100),
            agg_trade(60, price=101, quantity=2, sequence=10),
            agg_trade(120, price=99, quantity=2, sequence=11),
            agg_trade(180, price=98, quantity=2, sequence=12),
        ],
        strategy=strategy,
        symbol="BTCUSDT",
    )
    assert [fill.quantity for fill in result.fills] == [0.5, 0.5]
    assert all(fill.price == 100 for fill in result.fills)
    assert all(fill.liquidity == "maker" for fill in result.fills)
    assert result.final_state.fees_paid == pytest.approx(0.02)


def test_limit_order_waits_for_print_and_sell_cross() -> None:
    strategy = EmitOnceStrategy(
        order_type=OrderType.LIMIT,
        side=Side.SELL,
        quantity=1,
        limit_price=100,
    )
    result = ExecutionSimulator(config()).run(
        events=[
            kline(0, 100),
            kline(1, 99),
            agg_trade(120, price=99, quantity=1, sequence=11),
            agg_trade(180, price=101, quantity=1, sequence=12),
        ],
        strategy=strategy,
        symbol="BTCUSDT",
    )
    assert len(result.fills) == 1
    assert result.fills[0].price == 100


def test_funding_missing_warning_is_reported_when_rate_is_absent() -> None:
    strategy = EmitOnceStrategy(order_type=OrderType.MARKET, side=Side.BUY, quantity=1)
    result = ExecutionSimulator(
        config(latency_ms=30_000, funding_rate_bps=None, funding_interval_hours=1)
    ).run(
        events=[kline(0, 100), kline(1, 101), kline(61, 102)],
        strategy=strategy,
        symbol="BTCUSDT",
    )
    assert "funding_missing" in result.warnings


def test_positive_funding_rate_accrues_without_warning() -> None:
    strategy = EmitOnceStrategy(order_type=OrderType.MARKET, side=Side.BUY, quantity=1)
    result = ExecutionSimulator(
        config(latency_ms=30_000, funding_rate_bps=10.0, funding_interval_hours=1)
    ).run(
        events=[kline(0, 100), kline(1, 101), kline(61, 102)],
        strategy=strategy,
        symbol="BTCUSDT",
    )
    assert result.final_state.funding_paid > 0
    assert "funding_missing" not in result.warnings


def test_simulator_requires_events_and_price_bearing_events() -> None:
    with pytest.raises(ValueError, match="at least one"):
        ExecutionSimulator(config()).run(
            events=[],
            strategy=EmitOnceStrategy(OrderType.MARKET, Side.BUY, 1),
            symbol="BTCUSDT",
        )
    with pytest.raises(ValueError, match="price-bearing"):
        ExecutionSimulator(config()).run(
            events=[
                BookDepthEvent(
                    symbol="BTCUSDT",
                    timestamp=NOW,
                    sequence=1,
                    percentage=0.1,
                    depth=1,
                    notional=100,
                )
            ],
            strategy=EmitOnceStrategy(OrderType.MARKET, Side.BUY, 1),
            symbol="BTCUSDT",
        )


def test_momentum_strategy_emits_on_kline_breakout() -> None:
    strategy = MomentumStrategy(symbol="BTCUSDT", quantity=1, lookback_bars=1, threshold_bps=1)
    events = [kline(0, 100), kline(1, 101)]
    clock = ReplayClock()
    view = PointInTimeView(clock, events)
    strategy.on_start()
    clock.advance(events[0].timestamp)
    assert strategy.on_event(events[0], view) == ()
    clock.advance(events[1].timestamp)
    orders = strategy.on_event(events[1], view)
    assert len(orders) == 1
    assert orders[0].side == Side.BUY
    sell_strategy = MomentumStrategy(symbol="BTCUSDT", quantity=1, lookback_bars=1, threshold_bps=1)
    sell_events = [kline(0, 100), kline(1, 99)]
    sell_clock = ReplayClock()
    sell_view = PointInTimeView(sell_clock, sell_events)
    sell_strategy.on_start()
    sell_clock.advance(sell_events[0].timestamp)
    assert sell_strategy.on_event(sell_events[0], sell_view) == ()
    sell_clock.advance(sell_events[1].timestamp)
    sell_orders = sell_strategy.on_event(sell_events[1], sell_view)
    assert sell_orders[0].side == Side.SELL
    quiet_strategy = MomentumStrategy(
        symbol="BTCUSDT",
        quantity=1,
        lookback_bars=1,
        threshold_bps=10_000,
    )
    quiet_strategy.on_start()
    clock = ReplayClock()
    quiet_view = PointInTimeView(clock, events)
    clock.advance(events[0].timestamp)
    quiet_strategy.on_event(events[0], quiet_view)
    clock.advance(events[1].timestamp)
    assert quiet_strategy.on_event(events[1], quiet_view) == ()
    assert quiet_strategy.on_event(book(2), quiet_view) == ()
    quiet_strategy.on_fill(Fill("f", "o", "BTCUSDT", NOW, Side.BUY, 1, 1, 0, "taker", ()))
    quiet_strategy.on_finish()
    noop = NoopStrategy()
    noop.on_fill(Fill("f", "o", "BTCUSDT", NOW, Side.BUY, 1, 1, 0, "taker", ()))
    with pytest.raises(ValueError, match="lookback"):
        MomentumStrategy(symbol="BTCUSDT", quantity=1, lookback_bars=0, threshold_bps=1)
    with pytest.raises(ValueError, match="uppercase"):
        MomentumStrategy(symbol="btcusdt", quantity=1, lookback_bars=1, threshold_bps=1)
    with pytest.raises(ValueError, match="quantity"):
        MomentumStrategy(symbol="BTCUSDT", quantity=0, lookback_bars=1, threshold_bps=1)
    with pytest.raises(ValueError, match="threshold"):
        MomentumStrategy(symbol="BTCUSDT", quantity=1, lookback_bars=1, threshold_bps=-1)
