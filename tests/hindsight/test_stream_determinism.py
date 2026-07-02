from __future__ import annotations

from pathlib import Path

from hindsight.cli import default_exec_config, run_hindsight
from hindsight.strategy.base import NoopStrategy


def test_repeated_run_hindsight_invocations_are_stream_deterministic(tmp_path: Path) -> None:
    first = run_hindsight(
        lake_root=Path("examples/sample_data/hyperliquid"),
        output_dir=tmp_path / "first",
        symbol="SOL-PERP",
        date="20260101",
        limit=10,
        config=default_exec_config(),
        strategy=NoopStrategy(),
        repo_root=Path.cwd(),
    )
    second = run_hindsight(
        lake_root=Path("examples/sample_data/hyperliquid"),
        output_dir=tmp_path / "second",
        symbol="SOL-PERP",
        date="20260101",
        limit=10,
        config=default_exec_config(),
        strategy=NoopStrategy(),
        repo_root=Path.cwd(),
    )

    assert first.report.run_hash == second.report.run_hash
    assert first.report.manifest.data_content_hash == second.report.manifest.data_content_hash
    assert first.report.manifest.run_id == second.report.manifest.run_id
