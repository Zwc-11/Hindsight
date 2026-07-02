"""Execution configuration value objects."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ExecConfig(BaseModel):
    """Immutable Hindsight execution config."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    engine_version: str = Field(min_length=1)
    initial_cash: float = Field(gt=0)
    maker_fee_bps: float = Field(ge=0)
    taker_fee_bps: float = Field(ge=0)
    slippage_impact_bps: float = Field(ge=0)
    latency_ms: int = Field(ge=0)
    funding_rate_bps: float | None
    funding_interval_hours: int = Field(gt=0)
    participation_cap: float = Field(gt=0, le=1)
    seed: int

    # The default CLI smoke config uses Hyperliquid's published base perps tier
    # as of 2026-07-02: 1.5 bps maker, 4.5 bps taker. Keep fees configurable
    # for account-specific tiers, staking discounts, quote assets, and rebates.
