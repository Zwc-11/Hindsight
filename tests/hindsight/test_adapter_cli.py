from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from hindsight.cli import default_exec_config, main, run_hindsight
from hindsight.data.hyperliquid_adapter import HyperliquidLakeAdapter
from hindsight.data.lake import HyperliquidLakeLayout, write_parquet_records
from hindsight.strategy.base import NoopStrategy

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def write_silver_fills(root: Path) -> None:
    layout = HyperliquidLakeLayout(root)
    write_parquet_records(
        layout.silver_fills_path("SOL", "20260101"),
        [
            {
                "coin": "SOL",
                "ts_ms": int((NOW + timedelta(seconds=index * 20)).timestamp() * 1000),
                "px": 100.0 + index,
                "sz": 1.0,
                "side": "B" if index % 2 == 0 else "A",
                "crossed": False,
                "maker_side": 1 if index % 2 == 0 else -1,
                "fee": 0.01,
                "fee_token": "USDC",
                "tid": index,
            }
            for index in range(3)
        ],
    )


def test_hyperliquid_adapter_streams_fill_events(tmp_path: Path) -> None:
    write_silver_fills(tmp_path)
    events = list(
        HyperliquidLakeAdapter(tmp_path).stream_events(
            symbol="SOL-PERP",
            date="20260101",
            limit=10,
        )
    )

    assert len(events) == 3
    assert [str(event.event_type) for event in events] == ["hyperliquid_fill"] * 3
    assert all(event.timestamp.tzinfo is not None for event in events)


def test_run_hindsight_writes_schema_valid_outputs(tmp_path: Path) -> None:
    lake = tmp_path / "lake"
    out = tmp_path / "out"
    write_silver_fills(lake)
    artifacts = run_hindsight(
        lake_root=lake,
        output_dir=out,
        symbol="SOL-PERP",
        date="20260101",
        limit=10,
        config=default_exec_config(),
        strategy=NoopStrategy(),
        repo_root=tmp_path,
    )
    payload = json.loads(artifacts.json_path.read_text(encoding="utf-8"))
    manifest = json.loads(artifacts.manifest_path.read_text(encoding="utf-8"))
    assert payload["events_processed"] == 3
    assert payload["orders_emitted"] == 0
    assert payload["event_types"] == {"hyperliquid_fill": 3}
    assert payload["manifest"]["run_id"] == manifest["run_id"]
    assert artifacts.markdown_path.read_text(encoding="utf-8").startswith("# Hindsight")


def test_run_hindsight_fails_loudly_when_no_market_events_load(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="No market events loaded"):
        run_hindsight(
            lake_root=tmp_path / "missing-lake",
            output_dir=tmp_path / "out",
            symbol="SOL-PERP",
            date="20260101",
            limit=10,
            config=default_exec_config(),
            strategy=NoopStrategy(),
            repo_root=tmp_path,
        )


def test_cli_run_command_uses_hyperliquid_defaults(tmp_path: Path) -> None:
    out = tmp_path / "out"
    exit_code = main(["run", "--output-dir", str(out)])

    assert exit_code == 0
    assert (out / "hindsight-report.json").exists()
