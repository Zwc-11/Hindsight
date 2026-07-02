from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from hindsight.execution.config import ExecConfig
from hindsight.reporting.manifest import RunManifest, current_git_sha


def config() -> ExecConfig:
    return ExecConfig(
        engine_version="0.1.0",
        initial_cash=100_000,
        maker_fee_bps=1.5,
        taker_fee_bps=4.5,
        slippage_impact_bps=0.5,
        latency_ms=50,
        funding_rate_bps=0.0,
        funding_interval_hours=8,
        participation_cap=0.1,
        seed=7,
    )


def test_exec_config_is_frozen_and_forbids_extra_fields() -> None:
    cfg = config()
    with pytest.raises(ValidationError):
        cfg.initial_cash = 1  # type: ignore[misc]
    with pytest.raises(ValidationError):
        ExecConfig(
            engine_version="0.1.0",
            initial_cash=100_000,
            maker_fee_bps=1.5,
            taker_fee_bps=4.5,
            slippage_impact_bps=0.5,
            latency_ms=50,
            funding_rate_bps=0.0,
            funding_interval_hours=8,
            participation_cap=0.1,
            seed=7,
            extra_field=True,
        )


def test_manifest_run_id_is_deterministic() -> None:
    first = RunManifest.build(
        engine_version="0.1.0",
        git_sha="abc",
        data_content_hash="data",
        config_hash="config",
        seed=7,
    )
    second = RunManifest.build(
        engine_version="0.1.0",
        git_sha="abc",
        data_content_hash="data",
        config_hash="config",
        seed=7,
    )
    assert first.run_id == second.run_id


def test_current_git_sha_returns_unknown_outside_git_repo(tmp_path: Path) -> None:
    assert current_git_sha(tmp_path) == "unknown"
