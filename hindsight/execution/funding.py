"""Funding accrual helpers."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class FundingAccrual:
    """One funding calculation."""

    payment: float
    warning: str | None


def funding_payment(
    *,
    position_quantity: float,
    mark_price: float,
    funding_rate_bps: float | None,
) -> FundingAccrual:
    """Return signed funding payment; positive means paid by the account."""

    if mark_price <= 0:
        raise ValueError("mark_price must be positive")
    # Fallback reason: the M1 spec explicitly requires a funding_missing WARN
    # path. Missing funding rates accrue zero and surface a warning in the run.
    if funding_rate_bps is None:
        return FundingAccrual(payment=0.0, warning="funding_missing")
    return FundingAccrual(
        payment=position_quantity * mark_price * funding_rate_bps / 10_000,
        warning=None,
    )
