from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from hindsight.cli import main
from hindsight.demo.naive import naive_random_folds
from hindsight.demo.runner import run_demo
from hindsight.evaluation.walk_forward import LabelInterval

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def intervals(count: int) -> list[LabelInterval]:
    return [
        LabelInterval(
            index=index,
            start=NOW + timedelta(seconds=index * 20),
            end=NOW + timedelta(seconds=index * 20 + 10),
        )
        for index in range(count)
    ]


def test_naive_random_folds_are_deterministic_and_temporally_unsafe() -> None:
    folds = naive_random_folds(intervals(8), n_folds=2, test_window=2, seed=0)

    assert folds == naive_random_folds(intervals(8), n_folds=2, test_window=2, seed=0)
    assert folds[0].test_indices == (4, 1)
    assert max(folds[0].train_indices) > max(folds[0].test_indices)


def test_run_demo_writes_artifacts_and_blocks_leaky_policy(tmp_path: Path) -> None:
    artifacts = run_demo(
        sample_root=Path("examples/sample_data/hyperliquid"),
        output_dir=tmp_path,
        repo_root=Path.cwd(),
    )

    payload = json.loads(artifacts.json_path.read_text(encoding="utf-8"))
    assert payload["sample_data"]["label"] == "synthetic"
    assert payload["naive_control"]["warning"] == "deliberately unsafe control - do not use"
    assert payload["naive_control"]["winner_policy"] == "leaky"
    assert payload["hindsight_audit"]["leaky_policy_status"] == "blocked"
    assert payload["hindsight_audit"]["blocked_policy"] == "leaky"
    assert payload["naive_control"]["pbo"] == "n/a (requires >=4 trials)"
    assert payload["honest_benchmark"]["pbo"] == "n/a (requires >=4 trials)"
    assert payload["honest_benchmark"]["summary"][0]["deflated_sharpe_probability"] == (
        "n/a (requires >=3 trials)"
    )
    leakage = json.loads(artifacts.leakage_path.read_text(encoding="utf-8"))
    assert leakage["verdict"] == "fail"
    leaky_audit = next(audit for audit in leakage["audits"] if audit["policy_name"] == "leaky")
    clean_audit = next(
        audit for audit in leakage["audits"] if audit["policy_name"] == "ofi_quote"
    )
    assert leaky_audit["violations"][0]["probe"] == "target_leakage"
    assert leaky_audit["violations"][0]["severity"] == "hard"
    assert leaky_audit["violations"][0]["offending_features"] == ["markout_bps_10s"]
    assert clean_audit["violations"] == []
    assert artifacts.markdown_path.read_text(encoding="utf-8").startswith("# Hindsight Demo")
    assert artifacts.naive_csv_path.exists()
    assert artifacts.honest_csv_path.exists()
    assert artifacts.manifest_path.exists()
    assert artifacts.report_json_path.exists()
    assert artifacts.tearsheet_path.exists()


def test_demo_cli_writes_outputs(tmp_path: Path) -> None:
    assert main(["demo", "--output-dir", str(tmp_path)]) == 0
    assert (tmp_path / "demo.json").exists()
    assert (tmp_path / "demo.md").exists()
    assert (tmp_path / "manifest.json").exists()
    assert (tmp_path / "leakage.json").exists()
    assert (tmp_path / "report.json").exists()
    assert (tmp_path / "tearsheet.html").exists()
