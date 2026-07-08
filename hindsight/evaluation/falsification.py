"""Adversarial falsification suite.

Deliberately injects known defects into the evaluation pipeline and proves that
a leakage / realism tripwire catches each one. If any injected defect escapes
detection, the suite fails. This is the harness proving it can catch itself
lying: nobody trusts a backtester's "no leakage" verdict unless it can also
demonstrate its alarms actually fire.

Five injected defects, each paired with the probe that must catch it:

* ``label_lookahead``      -> ``lookahead_perturbation``
* ``feature_from_future``  -> ``target_leakage``
* ``survivorship``         -> ``survivorship``
* ``fold_contamination``   -> ``temporal_overlap``
* ``fee_slippage_zeroing`` -> ``zero_cost_execution``
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

from hindsight.core.hashing import stable_hash
from hindsight.data.hyperliquid_adapter import HyperliquidLakeAdapter, HyperliquidMarkoutRecord
from hindsight.evaluation.leakage import (
    probe_lookahead_perturbation,
    probe_survivorship,
    probe_target_leakage,
    probe_temporal_overlap,
    probe_zero_cost_execution,
)
from hindsight.evaluation.walk_forward import LabelInterval, WalkForwardFold

FALSIFICATION_SCHEMA_VERSION = "1.1.0"
FALSIFICATION_CASES = (
    "label_lookahead",
    "feature_from_future",
    "survivorship",
    "fold_contamination",
    "fee_slippage_zeroing",
)
_EVIDENCE_LIMIT = 12

# A universe where one name (the classic blown-up perp) delisted mid-sample.
_FULL_UNIVERSE = ("BTC-PERP", "ETH-PERP", "LUNA-PERP", "SOL-PERP")
_SURVIVOR_UNIVERSE = ("BTC-PERP", "ETH-PERP", "SOL-PERP")


@dataclass(frozen=True, slots=True)
class FalsificationCase:
    """One injected defect and whether its tripwire caught it."""

    defect: str
    probe: str
    caught: bool
    statistic: str
    evidence: tuple[str, ...]
    detail: str
    evidence_count: int | None = None


@dataclass(frozen=True, slots=True)
class FalsificationResult:
    """Deterministic falsification matrix and content hash."""

    cases: tuple[FalsificationCase, ...]
    all_caught: bool
    run_hash: str
    sample_rows: int = 0
    target_name: str = ""
    suite_version: str = FALSIFICATION_SCHEMA_VERSION


def run_falsification(
    *,
    sample_root: Path,
    symbol: str = "SOL-PERP",
    date: str = "20260101",
    horizon: str = "10s",
    label_horizon_seconds: float = 10.0,
    limit: int = 8,
) -> FalsificationResult:
    """Inject every defect, run its tripwire, and return the caught/escaped matrix."""
    adapter = HyperliquidLakeAdapter(sample_root)
    try:
        records = adapter.load_markouts(symbol=symbol, date=date, limit=limit)
    except FileNotFoundError as exc:
        raise FileNotFoundError(
            f"No Hyperliquid markout rows loaded for {symbol.upper()}"
        ) from exc
    if len(records) < 6:
        raise ValueError("falsification requires at least six sample rows")

    return run_falsification_on_records(
        records,
        horizon=horizon,
        label_horizon_seconds=label_horizon_seconds,
        max_records=limit,
    )


def run_falsification_on_records(
    records: Sequence[HyperliquidMarkoutRecord],
    *,
    horizon: str = "10s",
    label_horizon_seconds: float = 10.0,
    max_records: int | None = 256,
) -> FalsificationResult:
    """Run the falsification matrix against already-loaded markout records."""
    if label_horizon_seconds <= 0:
        raise ValueError("label_horizon_seconds must be positive")
    if max_records is not None and max_records < 6:
        raise ValueError("max_records must be at least six")
    selected = tuple(records if max_records is None else records[:max_records])
    if len(selected) < 6:
        raise ValueError("falsification requires at least six sample rows")
    missing_horizon = [
        index
        for index, record in enumerate(selected)
        if horizon not in record.markout_bps
    ]
    if missing_horizon:
        raise ValueError(f"falsification records missing markout horizon {horizon}")

    cases = (
        _case_label_lookahead(selected, horizon),
        _case_feature_from_future(horizon),
        _case_survivorship(),
        _case_fold_contamination(selected, label_horizon_seconds=label_horizon_seconds),
        _case_fee_slippage_zeroing(),
    )
    all_caught = all(case.caught for case in cases)
    run_hash = stable_hash(
        [
            {
                "defect": case.defect,
                "probe": case.probe,
                "caught": case.caught,
                "statistic": case.statistic,
                "evidence": list(case.evidence),
            }
            for case in cases
        ]
    )
    return FalsificationResult(
        cases=cases,
        all_caught=all_caught,
        run_hash=run_hash,
        sample_rows=len(selected),
        target_name=f"markout_bps_{horizon}",
    )


def falsification_payload(result: FalsificationResult) -> dict[str, Any]:
    """Serialize a falsification result to a deterministic JSON-ready dict."""
    return {
        "schema_version": result.suite_version,
        "expected_defects": len(FALSIFICATION_CASES),
        "sample_rows": result.sample_rows,
        "target_name": result.target_name,
        "all_caught": result.all_caught,
        "defects": len(result.cases),
        "caught": sum(1 for case in result.cases if case.caught),
        "run_hash": result.run_hash,
        "cases": [
            {
                "defect": case.defect,
                "probe": case.probe,
                "caught": case.caught,
                "statistic": case.statistic,
                "evidence": list(case.evidence),
                "evidence_count": case.evidence_count
                if case.evidence_count is not None
                else len(case.evidence),
                "detail": case.detail,
            }
            for case in result.cases
        ],
    }


def write_falsification_json(path: Path, result: FalsificationResult) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(falsification_payload(result), sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


# --------------------------------------------------------------------------- #
# Injected defects                                                             #
# --------------------------------------------------------------------------- #


def _case_label_lookahead(
    records: Sequence[HyperliquidMarkoutRecord],
    horizon: str,
) -> FalsificationCase:
    """A predictor keyed on the realized label; shifting labels -1 bar moves it."""
    future = [record.markout_bps[horizon] for record in records]
    baseline = tuple(future)
    # Perturb only the future: each sample peeks at the next bar's label.
    shifted = [
        future[index + 1] if index + 1 < len(future) else 0.0 for index in range(len(future))
    ]
    perturbed = tuple(shifted)
    violations = probe_lookahead_perturbation(
        baseline_predictions=baseline,
        perturbed_predictions=perturbed,
        tolerance=0.0,
    )
    changed = tuple(
        index for index, (a, b) in enumerate(zip(baseline, perturbed, strict=True)) if a != b
    )
    return FalsificationCase(
        defect="label_lookahead",
        probe="lookahead_perturbation",
        caught=bool(violations),
        statistic=f"{len(changed)}/{len(future)} predictions moved under a -1 bar label shift",
        evidence=_limited_evidence(f"sample[{index}]" for index in changed),
        detail="Predictor reads the realized label; future-only perturbation must not move it.",
        evidence_count=len(changed),
    )


def _case_feature_from_future(horizon: str) -> FalsificationCase:
    """A t+k feature (the realized markout) is joined into the feature set."""
    target = f"markout_bps_{horizon}"
    features = ("maker_side", target)
    violations = probe_target_leakage(features, target)
    offending = tuple(features[index] for violation in violations for index in violation.indices)
    return FalsificationCase(
        defect="feature_from_future",
        probe="target_leakage",
        caught=bool(violations),
        statistic=f"{len(offending)} target-derived feature(s) in the design matrix",
        evidence=_limited_evidence(offending),
        detail="A feature equal to the future label leaks the answer into training.",
        evidence_count=len(offending),
    )


def _case_survivorship() -> FalsificationCase:
    """Evaluation universe silently drops a symbol that delisted mid-sample."""
    violations = probe_survivorship(
        full_universe=_FULL_UNIVERSE,
        evaluated_universe=_SURVIVOR_UNIVERSE,
    )
    dropped = tuple(
        _FULL_UNIVERSE[index] for violation in violations for index in violation.indices
    )
    return FalsificationCase(
        defect="survivorship",
        probe="survivorship",
        caught=bool(violations),
        statistic=f"{len(dropped)} symbol(s) present at start dropped from evaluation",
        evidence=_limited_evidence(dropped),
        detail="Restricting to survivors hides the names that blew up and inflates the edge.",
        evidence_count=len(dropped),
    )


def _case_fold_contamination(
    records: Sequence[HyperliquidMarkoutRecord],
    *,
    label_horizon_seconds: float,
) -> FalsificationCase:
    """A contiguous split with purging removed: a train label overlaps a test label."""
    contamination_horizon = timedelta(seconds=label_horizon_seconds * 3)
    intervals = [
        LabelInterval(
            index=index,
            start=record.timestamp,
            end=record.timestamp + contamination_horizon,
        )
        for index, record in enumerate(records)
    ]
    # Naive unpurged split: train is the block immediately before the test block,
    # so the last train label still runs into the first test label.
    contaminated = WalkForwardFold(
        fold_id=0,
        train_indices=tuple(range(0, 4)),
        test_indices=(4, 5),
        test_start=intervals[4].start,
        test_end=intervals[5].end,
    )
    violations = probe_temporal_overlap((contaminated,), intervals)
    offending = tuple(index for violation in violations for index in violation.indices)
    return FalsificationCase(
        defect="fold_contamination",
        probe="temporal_overlap",
        caught=bool(violations),
        statistic=f"{len(offending)} train label(s) overlap a test label with purging removed",
        evidence=_limited_evidence(f"train[{index}]" for index in offending),
        detail="Without a purge gap, a train sample's label window leaks into the test fold.",
        evidence_count=len(offending),
    )


def _case_fee_slippage_zeroing() -> FalsificationCase:
    """Execution charges no fees and crosses no spread: a costless fill."""
    violations = probe_zero_cost_execution(
        maker_fee_bps=0.0,
        taker_fee_bps=0.0,
        slippage_impact_bps=0.0,
    )
    fields = ("maker_fee_bps", "taker_fee_bps", "slippage_impact_bps")
    zeroed = tuple(fields[index] for violation in violations for index in violation.indices)
    return FalsificationCase(
        defect="fee_slippage_zeroing",
        probe="zero_cost_execution",
        caught=bool(violations),
        statistic="all execution frictions set to zero" if zeroed else "costs present",
        evidence=_limited_evidence(zeroed),
        detail="Zero fees and zero slippage book a fill no real account ever receives.",
        evidence_count=len(zeroed),
    )


def _limited_evidence(items: Sequence[str] | Any) -> tuple[str, ...]:
    values = tuple(str(item) for item in items)
    if len(values) <= _EVIDENCE_LIMIT:
        return values
    remaining = len(values) - _EVIDENCE_LIMIT
    return (*values[:_EVIDENCE_LIMIT], f"... {remaining} more")
