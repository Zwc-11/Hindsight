"""Deterministic execution simulator."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import cast

from hindsight.core.clock import ReplayClock
from hindsight.core.cursor import CursorEvent, EventCursor
from hindsight.core.events import (
    AggTradeEvent,
    BookTickerEvent,
    CanonicalEvent,
    HyperliquidFillEvent,
    KlineEvent,
    Side,
    TradeEvent,
)
from hindsight.core.hashing import run_hash
from hindsight.core.types import (
    EquityPoint,
    Fill,
    Liquidity,
    OrderIntent,
    OrderType,
    PortfolioState,
)
from hindsight.execution.config import ExecConfig
from hindsight.execution.funding import funding_payment
from hindsight.execution.slippage import apply_slippage
from hindsight.pit.view import PointInTimeView
from hindsight.portfolio.accounting import Portfolio
from hindsight.strategy.base import Strategy


@dataclass(slots=True)
class _ActiveOrder:
    intent: OrderIntent
    active_at: datetime
    remaining_quantity: float


@dataclass(frozen=True, slots=True)
class SimulationResult:
    """Outputs from one simulated run."""

    events_processed: int
    orders_emitted: int
    fills: tuple[Fill, ...]
    equity_curve: tuple[EquityPoint, ...]
    final_state: PortfolioState
    warnings: tuple[str, ...]
    run_hash: str


class ExecutionSimulator:
    """Runs one strategy against a canonical event stream."""

    def __init__(self, config: ExecConfig) -> None:
        self._config = config

    def run(
        self,
        *,
        events: list[CanonicalEvent],
        strategy: Strategy,
        symbol: str,
    ) -> SimulationResult:
        if not events:
            raise ValueError("execution simulation requires at least one market event")
        ordered_events = cast(
            list[CanonicalEvent],
            list(EventCursor(cast(list[CursorEvent], events))),
        )
        clock = ReplayClock()
        view = PointInTimeView(clock, ordered_events)
        portfolio = Portfolio(initial_cash=self._config.initial_cash, symbol=symbol)
        active_orders: list[_ActiveOrder] = []
        fills: list[Fill] = []
        equity_curve: list[EquityPoint] = []
        warnings: list[str] = []
        orders_emitted = 0
        last_mark: float | None = None
        next_funding_at = ordered_events[0].timestamp + timedelta(
            hours=self._config.funding_interval_hours
        )

        strategy.on_start()
        for event in ordered_events:
            clock.advance(event.timestamp)
            mark_price = _mark_price(event)
            if mark_price is not None:
                last_mark = mark_price
            new_fills = self._fill_active_orders(
                event=event,
                active_orders=active_orders,
            )
            for fill in new_fills:
                portfolio.apply_fill(fill)
                strategy.on_fill(fill)
                fills.append(fill)
                warnings.extend(fill.flags)
            if mark_price is not None:
                next_funding_at = self._apply_due_funding(
                    portfolio=portfolio,
                    event_time=event.timestamp,
                    next_funding_at=next_funding_at,
                    mark_price=mark_price,
                    warnings=warnings,
                )
                equity_curve.append(
                    portfolio.equity_point(timestamp=event.timestamp, mark_price=mark_price)
                )
            intents = strategy.on_event(event, view)
            orders_emitted += len(intents)
            for intent in intents:
                active_orders.append(
                    _ActiveOrder(
                        intent=intent,
                        active_at=(
                            intent.timestamp + timedelta(milliseconds=self._config.latency_ms)
                        ),
                        remaining_quantity=intent.quantity,
                    )
                )
        strategy.on_finish()
        if last_mark is None:
            raise ValueError("execution simulation requires at least one price-bearing event")
        final_state = portfolio.mark(timestamp=ordered_events[-1].timestamp, mark_price=last_mark)
        trace: list[dict[str, object]] = [
            {
                "event_id": event.event_id,
                "timestamp": event.timestamp.isoformat(),
            }
            for event in ordered_events
        ]
        trace.extend(
            {
                "fill_id": fill.fill_id,
                "timestamp": fill.timestamp.isoformat(),
                "quantity": fill.quantity,
                "price": fill.price,
            }
            for fill in fills
        )
        return SimulationResult(
            events_processed=len(ordered_events),
            orders_emitted=orders_emitted,
            fills=tuple(fills),
            equity_curve=tuple(equity_curve),
            final_state=final_state,
            warnings=tuple(warnings),
            run_hash=run_hash(trace),
        )

    def _fill_active_orders(
        self,
        *,
        event: CanonicalEvent,
        active_orders: list[_ActiveOrder],
    ) -> list[Fill]:
        fills: list[Fill] = []
        remaining_orders: list[_ActiveOrder] = []
        for order in active_orders:
            if event.timestamp < order.active_at:
                remaining_orders.append(order)
                continue
            fill = self._fill_order(order=order, event=event)
            if fill is None:
                remaining_orders.append(order)
                continue
            fills.append(fill)
            order.remaining_quantity -= fill.quantity
            if order.remaining_quantity > 0:
                remaining_orders.append(order)
        active_orders[:] = remaining_orders
        return fills

    def _fill_order(self, *, order: _ActiveOrder, event: CanonicalEvent) -> Fill | None:
        if order.intent.order_type == OrderType.MARKET:
            return self._market_fill(order=order, event=event)
        return self._limit_fill(order=order, event=event)

    def _market_fill(self, *, order: _ActiveOrder, event: CanonicalEvent) -> Fill | None:
        flags: tuple[str, ...] = ()
        if isinstance(event, BookTickerEvent):
            touch_price = event.ask_price if order.intent.side == Side.BUY else event.bid_price
        elif isinstance(event, KlineEvent):
            # Fallback reason: M1 explicitly permits market fills at kline close
            # when bookTicker is absent, and requires the fill to be flagged.
            touch_price = event.close_price
            flags = ("top_of_book_missing",)
        else:
            return None
        price = apply_slippage(
            price=touch_price,
            side=order.intent.side,
            quantity=order.remaining_quantity,
            impact_bps_per_unit=self._config.slippage_impact_bps,
        )
        return self._fill(
            order=order,
            price=price,
            quantity=order.remaining_quantity,
            event_time=event.timestamp,
            liquidity="taker",
            flags=flags,
        )

    def _limit_fill(self, *, order: _ActiveOrder, event: CanonicalEvent) -> Fill | None:
        if not isinstance(event, AggTradeEvent | TradeEvent | HyperliquidFillEvent):
            return None
        limit_price = cast(float, order.intent.limit_price)
        if order.intent.side == Side.BUY and event.price > limit_price:
            return None
        if order.intent.side == Side.SELL and event.price < limit_price:
            return None
        available_quantity = event.quantity * self._config.participation_cap
        fill_quantity = min(order.remaining_quantity, available_quantity)
        return self._fill(
            order=order,
            price=limit_price,
            quantity=fill_quantity,
            event_time=event.timestamp,
            liquidity="maker",
            flags=(),
        )

    def _fill(
        self,
        *,
        order: _ActiveOrder,
        price: float,
        quantity: float,
        event_time: datetime,
        liquidity: Liquidity,
        flags: tuple[str, ...],
    ) -> Fill:
        fee_bps = self._config.maker_fee_bps if liquidity == "maker" else self._config.taker_fee_bps
        fee = price * quantity * fee_bps / 10_000
        return Fill(
            fill_id=f"{order.intent.order_id}:{event_time.isoformat()}:{quantity:.12g}",
            order_id=order.intent.order_id,
            symbol=order.intent.symbol,
            timestamp=event_time,
            side=order.intent.side,
            price=price,
            quantity=quantity,
            fee=fee,
            liquidity=liquidity,
            flags=flags,
        )

    def _apply_due_funding(
        self,
        *,
        portfolio: Portfolio,
        event_time: datetime,
        next_funding_at: datetime,
        mark_price: float,
        warnings: list[str],
    ) -> datetime:
        while event_time >= next_funding_at:
            accrual = funding_payment(
                position_quantity=portfolio.quantity,
                mark_price=mark_price,
                funding_rate_bps=self._config.funding_rate_bps,
            )
            portfolio.apply_funding(accrual.payment)
            if accrual.warning is not None:
                warnings.append(accrual.warning)
            next_funding_at += timedelta(hours=self._config.funding_interval_hours)
        return next_funding_at


def _mark_price(event: CanonicalEvent) -> float | None:
    if isinstance(event, KlineEvent):
        return event.close_price
    if isinstance(event, BookTickerEvent):
        return (event.bid_price + event.ask_price) / 2
    if isinstance(event, AggTradeEvent | TradeEvent | HyperliquidFillEvent):
        return event.price
    return None
