from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from hindsight.core.events import Side
from hindsight.core.hashing import stable_hash
from hindsight.data.hyperliquid_adapter import HyperliquidMarkoutRecord
from hindsight.demo.runner import run_demo
from hindsight.evaluation.benchmark import QuotePolicy
from hindsight.reporting import report_v2
from hindsight.reporting.report_v2 import (
    build_report_v2,
    validate_report_v2,
    write_report_v2_json,
)

NOW = datetime(2026, 1, 1, tzinfo=UTC)

TOP_LEVEL_BLOCKS = {
    "meta",
    "sample_data",
    "comparison",
    "leaderboards",
    "equity_curves",
    "markout_ladder",
    "fold_heatmap",
    "cost_attribution",
    "capacity",
    "sensitivity",
    "regimes",
    "dsr_pbo",
    "probes",
    "falsification",
    "fills",
    "provenance",
    "reproduce",
}


def _record(
    index: int, markouts: dict[str, float], maker_side: int = 1
) -> HyperliquidMarkoutRecord:
    return HyperliquidMarkoutRecord(
        symbol="SOL-PERP",
        timestamp=NOW + timedelta(seconds=index * 20),
        price=100.0 + index,
        quantity=1.0,
        side=Side.BUY if index % 2 == 0 else Side.SELL,
        maker_side=maker_side,
        trade_id=index,
        markout_bps=markouts,
    )


def _demo_report(tmp_path: Path) -> dict:
    run_demo(
        sample_root=Path("examples/sample_data/hyperliquid"),
        output_dir=tmp_path,
        repo_root=Path.cwd(),
    )
    return json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# Integration: the demo emits a valid, self-sufficient schema-v2 report.        #
# --------------------------------------------------------------------------- #


def test_demo_emits_all_blocks_and_validates(tmp_path: Path) -> None:
    report = _demo_report(tmp_path)
    assert set(report) == TOP_LEVEL_BLOCKS
    assert report["meta"]["report_schema"] == 2
    # Re-validation of the on-disk payload passes.
    validate_report_v2(report)


def test_comparison_headlines_the_blocked_leak(tmp_path: Path) -> None:
    comparison = _demo_report(tmp_path)["comparison"]
    assert comparison["verdict"] == "blocked"
    assert comparison["headline_naive"] == "Naive split: leaky ranks #1"
    assert comparison["headline_audited"] == "Hindsight: BLOCKED"
    leaky = next(p for p in comparison["policies"] if p["policy_name"] == "leaky")
    assert leaky["naive_rank"] == 1
    assert leaky["audited_status"] == "blocked"
    assert leaky["block_reason"]
    clean = next(p for p in comparison["policies"] if p["policy_name"] == "maker_side_filter")
    assert clean["audited_status"] == "audited"
    assert clean["block_reason"] is None


def test_probes_report_the_caught_feature(tmp_path: Path) -> None:
    probes = _demo_report(tmp_path)["probes"]
    assert probes["verdict"] == "fail"
    leaky = next(item for item in probes["items"] if item["policy_name"] == "leaky")
    assert leaky["status"] == "caught"
    assert leaky["offending_features"] == ["markout_bps_10s"]
    clean = next(item for item in probes["items"] if item["policy_name"] == "ofi_quote")
    assert clean["status"] == "pass"
    assert clean["offending_features"] == []


def test_cost_attribution_nets_out(tmp_path: Path) -> None:
    cost = _demo_report(tmp_path)["cost_attribution"]
    for policy in cost["policies"]:
        expected = policy["gross_bps"] + sum(stage["delta_bps"] for stage in policy["stages"])
        assert policy["net_bps"] == pytest.approx(expected)
        # Stage cumulatives are monotone in insertion order.
        running = policy["gross_bps"]
        for stage in policy["stages"]:
            running += stage["delta_bps"]
            assert stage["cumulative_bps"] == pytest.approx(running)


def test_capacity_saturates_at_full_participation(tmp_path: Path) -> None:
    capacity = _demo_report(tmp_path)["capacity"]
    assert capacity["available"] is True
    caps = [point["participation_cap"] for point in capacity["points"]]
    assert caps == sorted(caps)
    last = capacity["points"][-1]
    assert last["participation_cap"] == 1.0
    assert last["fill_fraction"] == 1.0
    assert last["realized_lift_bps"] == pytest.approx(capacity["base_lift_bps"])


def test_thin_synthetic_blocks_are_honestly_unavailable(tmp_path: Path) -> None:
    report = _demo_report(tmp_path)
    assert report["sensitivity"]["available"] is False
    assert report["regimes"]["available"] is False
    assert "Phase 0" in report["sensitivity"]["note"]
    # Single committed horizon in the synthetic lake.
    assert report["markout_ladder"]["horizons"] == ["10s"]


def test_falsification_block_is_present_and_all_caught(tmp_path: Path) -> None:
    fals = _demo_report(tmp_path)["falsification"]
    assert fals["available"] is True
    assert fals["all_caught"] is True
    assert fals["defects"] == fals["caught"] == 5
    defects = {case["defect"] for case in fals["cases"]}
    assert "label_lookahead" in defects
    assert "survivorship" in defects
    assert all(case["detail"] for case in fals["cases"])
    assert all(case["evidence_count"] >= len(case["evidence"]) for case in fals["cases"])


def test_falsification_absent_renders_unavailable() -> None:
    block = report_v2._falsification(None)
    assert block["available"] is False
    assert block["cases"] == []


def test_provenance_self_hash_covers_payload(tmp_path: Path) -> None:
    report = _demo_report(tmp_path)
    prov = report["provenance"]
    for key in (
        "run_id",
        "git_sha",
        "config_hash",
        "data_content_hash",
        "report_self_hash",
        "engine_version",
    ):
        assert prov[key]
    # The self-hash is stable_hash of the payload with the hash field blanked.
    stored = prov["report_self_hash"]
    probe = json.loads(json.dumps(report))
    probe["provenance"]["report_self_hash"] = ""
    assert stable_hash(probe) == stored


def test_report_json_is_byte_deterministic(tmp_path: Path) -> None:
    first = (tmp_path / "a")
    second = (tmp_path / "b")
    run_demo(sample_root=Path("examples/sample_data/hyperliquid"), output_dir=first,
             repo_root=Path.cwd())
    run_demo(sample_root=Path("examples/sample_data/hyperliquid"), output_dir=second,
             repo_root=Path.cwd())
    assert (first / "report.json").read_bytes() == (second / "report.json").read_bytes()


# --------------------------------------------------------------------------- #
# Contract enforcement                                                          #
# --------------------------------------------------------------------------- #


def test_validate_rejects_unknown_top_level_key(tmp_path: Path) -> None:
    report = _demo_report(tmp_path)
    report["surprise"] = 1
    with pytest.raises(ValidationError):
        validate_report_v2(report)


def test_build_requires_records() -> None:
    with pytest.raises(ValueError, match="at least one markout record"):
        build_report_v2(
            sample_root=Path("x"),
            symbol="SOL-PERP",
            date="20260101",
            horizon="10s",
            data_label="synthetic",
            records=[],
            baseline_policy=QuotePolicy(name="b", quote_mask=(), feature_names=()),
            clean_policy=QuotePolicy(name="c", quote_mask=(), feature_names=()),
            leaky_policy=QuotePolicy(name="leaky", quote_mask=(), feature_names=()),
            naive_result=None,  # type: ignore[arg-type]
            honest_result=None,  # type: ignore[arg-type]
            blocked_error="x",
            leakage_payload={},
            manifest=None,  # type: ignore[arg-type]
            cost_model={},
            reproduce_command="cmd",
            pinned_versions={},
        )


def test_write_report_v2_json_roundtrips(tmp_path: Path) -> None:
    report = _demo_report(tmp_path)
    out = tmp_path / "again.json"
    write_report_v2_json(out, report)
    assert json.loads(out.read_text(encoding="utf-8")) == report


# --------------------------------------------------------------------------- #
# Unit coverage of block helpers and numeric edges                              #
# --------------------------------------------------------------------------- #


def test_markout_ladder_multi_horizon_note() -> None:
    records = [
        _record(0, {"1s": -1.0, "10s": -3.0}, maker_side=1),
        _record(1, {"1s": 0.5, "10s": 2.0}, maker_side=1),
        _record(2, {"1s": 1.5, "10s": 4.0}, maker_side=1),
    ]
    policy = QuotePolicy(name="all", quote_mask=(True, True, True), feature_names=("maker_side",))
    ladder = report_v2._markout_ladder(records=records, policies=[policy], horizon="10s")
    assert ladder["horizons"] == ["1s", "10s"]
    assert "CI bands" in ladder["note"]
    rungs = ladder["policies"][0]["rungs"]
    assert [rung["horizon"] for rung in rungs] == ["1s", "10s"]


def test_cost_attribution_zero_gross_when_nothing_quoted() -> None:
    records = [_record(0, {"10s": 5.0})]
    policy = QuotePolicy(name="none", quote_mask=(False,), feature_names=("maker_side",))
    cost = report_v2._cost_attribution(
        records=records,
        policies=[policy],
        horizon="10s",
        cost_model={
            "maker_fee_bps": 1.5,
            "taker_fee_bps": 4.5,
            "slippage_impact_bps": 0.5,
            "funding_rate_bps": 0.0,
            "funding_interval_hours": 8,
        },
    )
    assert cost["policies"][0]["gross_bps"] == 0.0


def test_capacity_handles_missing_baseline() -> None:
    capacity = report_v2._capacity(
        honest_summary=[{"policy_name": "other", "avg_lift_bps": 3.0}],
        baseline_name="absent",
        cost_model={},
    )
    assert capacity["base_lift_bps"] == 0.0
    assert all(point["realized_lift_bps"] == 0.0 for point in capacity["points"])


@pytest.mark.parametrize(
    ("horizon", "seconds"),
    [("10s", 10.0), ("2m", 120.0), ("1h", 3600.0), ("500ms", 0.5), ("1d", 86400.0)],
)
def test_horizon_seconds(horizon: str, seconds: float) -> None:
    assert report_v2._horizon_seconds(horizon) == seconds


def test_horizon_seconds_unknown_is_infinite() -> None:
    assert report_v2._horizon_seconds("weird") == float("inf")
    assert report_v2._horizon_seconds("xs") == float("inf")


def test_mean_ci_edges() -> None:
    assert report_v2._mean_ci([]) == (0.0, 0.0, 0.0)
    assert report_v2._mean_ci([5.0]) == (5.0, 5.0, 5.0)
    mean, lower, upper = report_v2._mean_ci([1.0, 3.0])
    assert mean == 2.0
    assert lower < mean < upper


def test_mean_rejects_empty() -> None:
    with pytest.raises(ValueError, match="empty sequence"):
        report_v2._mean([])
