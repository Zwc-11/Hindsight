from __future__ import annotations

import json
from pathlib import Path

import pytest

from hindsight import cli
from hindsight.cli import main
from hindsight.data.hyperliquid_adapter import HyperliquidLakeAdapter
from hindsight.evaluation.falsification import (
    FalsificationCase,
    FalsificationResult,
    falsification_payload,
    run_falsification,
    run_falsification_on_records,
    write_falsification_json,
)
from hindsight.evaluation.leakage import probe_survivorship, probe_zero_cost_execution

SAMPLE = Path("examples/sample_data/hyperliquid")

EXPECTED_PROBE = {
    "label_lookahead": "lookahead_perturbation",
    "feature_from_future": "target_leakage",
    "survivorship": "survivorship",
    "fold_contamination": "temporal_overlap",
    "fee_slippage_zeroing": "zero_cost_execution",
}


# --------------------------------------------------------------------------- #
# The suite: every injected defect is caught by its tripwire.                   #
# --------------------------------------------------------------------------- #


def test_all_five_defects_are_caught() -> None:
    result = run_falsification(sample_root=SAMPLE)
    assert len(result.cases) == 5
    assert result.all_caught is True
    assert all(case.caught for case in result.cases)
    assert {case.defect: case.probe for case in result.cases} == EXPECTED_PROBE
    for case in result.cases:
        assert case.evidence, f"{case.defect} produced no evidence"
        assert case.statistic


def test_cases_carry_the_offending_evidence() -> None:
    cases = {case.defect: case for case in run_falsification(sample_root=SAMPLE).cases}
    assert cases["feature_from_future"].evidence == ("markout_bps_10s",)
    assert cases["feature_from_future"].evidence_count == 1
    assert cases["survivorship"].evidence == ("LUNA-PERP",)
    assert cases["fold_contamination"].evidence == ("train[3]",)
    assert "maker_fee_bps" in cases["fee_slippage_zeroing"].evidence
    assert cases["label_lookahead"].evidence  # at least one prediction moved


def test_run_is_deterministic() -> None:
    first = run_falsification(sample_root=SAMPLE)
    second = run_falsification(sample_root=SAMPLE)
    assert first == second
    assert first.run_hash == second.run_hash


def test_requires_enough_rows() -> None:
    with pytest.raises(ValueError, match="at least six"):
        run_falsification(sample_root=SAMPLE, limit=5)


def test_record_backed_suite_validates_horizon() -> None:
    records = HyperliquidLakeAdapter(SAMPLE).load_markouts(
        symbol="SOL-PERP",
        date="20260101",
        limit=8,
    )
    with pytest.raises(ValueError, match="missing markout horizon"):
        run_falsification_on_records(records, horizon="1m")


def test_missing_lake_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        run_falsification(sample_root=tmp_path)


# --------------------------------------------------------------------------- #
# The two new tripwires, positive and negative.                                 #
# --------------------------------------------------------------------------- #


def test_probe_survivorship_flags_dropped_symbols() -> None:
    violations = probe_survivorship(
        full_universe=("A", "B", "C"), evaluated_universe=("A", "C")
    )
    assert len(violations) == 1
    assert violations[0].probe == "survivorship"
    assert violations[0].severity == "hard"
    assert violations[0].indices == (1,)  # "B" dropped
    assert probe_survivorship(full_universe=("A", "B"), evaluated_universe=("A", "B")) == ()


def test_probe_zero_cost_execution() -> None:
    caught = probe_zero_cost_execution(
        maker_fee_bps=0.0, taker_fee_bps=0.0, slippage_impact_bps=0.0
    )
    assert len(caught) == 1
    assert caught[0].probe == "zero_cost_execution"
    # Any positive friction clears the tripwire.
    assert (
        probe_zero_cost_execution(
            maker_fee_bps=1.5, taker_fee_bps=0.0, slippage_impact_bps=0.0
        )
        == ()
    )
    with pytest.raises(ValueError, match="cannot be negative"):
        probe_zero_cost_execution(
            maker_fee_bps=-1.0, taker_fee_bps=0.0, slippage_impact_bps=0.0
        )


# --------------------------------------------------------------------------- #
# Serialization + CLI (including the CI-enforcing exit code).                    #
# --------------------------------------------------------------------------- #


def test_payload_and_writer_roundtrip(tmp_path: Path) -> None:
    result = run_falsification(sample_root=SAMPLE)
    payload = falsification_payload(result)
    assert payload["schema_version"] == "1.1.0"
    assert payload["expected_defects"] == 5
    assert payload["sample_rows"] == 8
    assert payload["target_name"] == "markout_bps_10s"
    assert payload["all_caught"] is True
    assert payload["defects"] == 5
    assert payload["caught"] == 5
    assert all(case["detail"] for case in payload["cases"])
    assert all(case["evidence_count"] >= 1 for case in payload["cases"])
    out = tmp_path / "falsification.json"
    write_falsification_json(out, result)
    assert json.loads(out.read_text(encoding="utf-8")) == payload


def test_cli_falsify_succeeds_and_writes_matrix(tmp_path: Path) -> None:
    assert main(["falsify", "--output-dir", str(tmp_path), "--limit", "8"]) == 0
    data = json.loads((tmp_path / "falsification.json").read_text(encoding="utf-8"))
    assert data["all_caught"] is True
    assert data["caught"] == data["defects"] == 5


def test_cli_falsify_returns_nonzero_when_a_defect_escapes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    escaped = FalsificationResult(
        cases=(
            FalsificationCase(
                defect="x", probe="p", caught=False, statistic="s", evidence=(), detail="d"
            ),
        ),
        all_caught=False,
        run_hash="deadbeef",
    )
    monkeypatch.setattr(cli, "run_falsification", lambda **kwargs: escaped)
    assert main(["falsify", "--output-dir", str(tmp_path)]) == 1
