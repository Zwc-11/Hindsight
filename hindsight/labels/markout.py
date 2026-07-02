"""Realized markout — the adverse-selection (toxicity) label.

This is the credibility core of the current plan (AUDIT_AND_PLAN.md §1 bullet 3):
the learning target is *realized markout*,
not a synthetic "hostile/benign" flag.

For a maker fill at time ``t``, price ``p``, side ``s`` (``+1`` = a resting bid was
filled, i.e. the maker bought; ``-1`` = a resting ask was filled, i.e. the maker
sold), the markout over horizon ``h`` is::

    markout_bps(h) = s * (mid_{t+h} - p) / p * 10_000

A *negative* markout means the mid moved against the maker after the fill — the maker
was picked off. A fill is labelled toxic when the fee-adjusted markout is negative.

The label is *forward-looking* by construction (it reads ``mid_{t+h}``), which is
exactly why training on it requires purged/embargoed walk-forward CV,
never random splits.
"""

from __future__ import annotations

from bisect import bisect_left
from collections.abc import Sequence
from dataclasses import dataclass

BPS = 10_000.0


@dataclass(frozen=True, slots=True)
class MarkoutConfig:
    """Forward horizons (seconds) and the maker-fee threshold (bps) for toxicity."""

    horizons_s: tuple[float, ...] = (1.0, 10.0, 60.0)
    fee_bps: float = 1.0


@dataclass(frozen=True, slots=True)
class MakerFill:
    """One maker fill. ``side`` is +1 (bid filled / bought) or -1 (ask filled / sold)."""

    ts_s: float
    price: float
    side: int


def markout_bps(fill_price: float, future_mid: float, side: int) -> float:
    """Realized markout in basis points for a single maker fill.

    Negative = the maker was picked off (adverse selection / toxic).
    """
    if fill_price <= 0.0:
        raise ValueError("fill_price must be positive")
    if side not in (1, -1):
        raise ValueError("side must be +1 (maker buy) or -1 (maker sell)")
    return side * (future_mid - fill_price) / fill_price * BPS


def is_toxic(markout_value_bps: float, fee_bps: float = 1.0) -> bool:
    """True when fee-adjusted markout is negative (maker lost to adverse selection)."""
    return markout_value_bps < -fee_bps


def future_mid_at(
    fill_ts_s: float,
    horizon_s: float,
    mid_ts_s: Sequence[float],
    mid_price: Sequence[float],
) -> float | None:
    """Forward as-of join: the first mid at or after ``fill_ts_s + horizon_s``.

    Returns ``None`` when the horizon runs past the end of the mid series, so callers
    never fabricate a look-ahead value. ``mid_ts_s`` must be sorted ascending.
    """
    if len(mid_ts_s) != len(mid_price):
        raise ValueError("mid_ts_s and mid_price must have the same length")
    idx = bisect_left(mid_ts_s, fill_ts_s + horizon_s)
    if idx >= len(mid_ts_s):
        return None
    return mid_price[idx]


def realized_markout_bps(
    fill: MakerFill,
    mid_ts_s: Sequence[float],
    mid_price: Sequence[float],
    horizon_s: float,
) -> float | None:
    """Markout (bps) for ``fill`` ``horizon_s`` seconds forward, or ``None`` past data end."""
    future_mid = future_mid_at(fill.ts_s, horizon_s, mid_ts_s, mid_price)
    if future_mid is None:
        return None
    return markout_bps(fill.price, future_mid, fill.side)
