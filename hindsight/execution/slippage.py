"""Deterministic slippage functions."""

from __future__ import annotations

from hindsight.core.events import Side


def apply_slippage(
    *,
    price: float,
    side: Side,
    quantity: float,
    impact_bps_per_unit: float,
) -> float:
    """Move price against the order by a linear impact model."""

    if price <= 0:
        raise ValueError("price must be positive")
    if quantity <= 0:
        raise ValueError("quantity must be positive")
    if impact_bps_per_unit < 0:
        raise ValueError("impact_bps_per_unit cannot be negative")
    impact = quantity * impact_bps_per_unit / 10_000
    if side == Side.BUY:
        return price * (1 + impact)
    return price * (1 - impact)
