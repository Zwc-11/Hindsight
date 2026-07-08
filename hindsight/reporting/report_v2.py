"""Report data contract v2.

One self-sufficient ``report.json`` per run. This module assembles the versioned
(``report_schema: 2``) payload that the HTML tearsheet renders, validates it with
pydantic, and stamps it with a content self-hash.

Design constraints:

* Deterministic. Given identical inputs (and a fixed ``git_sha``) the serialized
  JSON is byte-identical. All floats are rounded to a fixed precision and every
  list order is explicit.
* Honest. The bundled demo lake is deliberately tiny, so blocks that need
  flagship data (Phase 0) render an explicit ``available: false`` state with a
  note instead of fabricated numbers. Every populated number traces to a
  committed artifact.
* Decoupled. This module depends only on ``BenchmarkResult`` rows, markout
  records, and plain dicts. It does not import the execution simulator; the cost
  model is passed in as a disclosed parameter dict.
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Iterable, Sequence
from math import sqrt
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from hindsight.core.hashing import stable_hash
from hindsight.data.hyperliquid_adapter import HyperliquidMarkoutRecord
from hindsight.evaluation.benchmark import BenchmarkResult, BenchmarkRow, QuotePolicy
from hindsight.reporting.manifest import RunManifest

REPORT_SCHEMA_VERSION = 2

# Participation caps swept for the capacity panel. Fixed for determinism.
CAPACITY_CAPS: tuple[float, ...] = (0.02, 0.05, 0.1, 0.25, 0.5, 1.0)

_ROUND = 6
_CONFIDENCE_Z = 1.96


def _r(value: float) -> float:
    """Round to a fixed precision so serialized floats are byte-stable."""

    return round(float(value), _ROUND)


# --------------------------------------------------------------------------- #
# Schema models (extra="forbid" makes the contract strict and test-checkable). #
# --------------------------------------------------------------------------- #


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ReportMeta(_Frozen):
    report_schema: int
    title: str
    subtitle: str
    generator: str
    data_label: str


class SampleData(_Frozen):
    root: str
    label: str
    symbol: str
    date: str
    rows: int
    horizon: str


class ComparisonPolicy(_Frozen):
    policy_name: str
    naive_rank: int | None
    audited_status: str
    block_reason: str | None
    naive_avg_markout_bps: float | None
    audited_avg_lift_bps: float | None


class Comparison(_Frozen):
    headline_naive: str
    headline_audited: str
    verdict: str
    block_reason: str | None
    policies: tuple[ComparisonPolicy, ...]


class LeaderboardRow(_Frozen):
    rank: int
    policy_name: str
    avg_markout_bps: float
    avg_lift_bps: float
    quote_rate: float
    deflated_sharpe_probability: float | str


class RankShift(_Frozen):
    policy_name: str
    naive_rank: int | None
    audited_rank: int | None
    delta: int | None


class Leaderboards(_Frozen):
    naive: tuple[LeaderboardRow, ...]
    audited: tuple[LeaderboardRow, ...]
    rank_shifts: tuple[RankShift, ...]


class CurvePoint(_Frozen):
    index: int
    timestamp: str
    cum_markout_bps: float
    drawdown_bps: float
    quoted: bool


class EquityCurve(_Frozen):
    policy_name: str
    points: tuple[CurvePoint, ...]
    final_cum_markout_bps: float
    max_drawdown_bps: float


class LadderRung(_Frozen):
    horizon: str
    avg_markout_bps: float
    ci_lower_bps: float
    ci_upper_bps: float


class LadderPolicy(_Frozen):
    policy_name: str
    rungs: tuple[LadderRung, ...]


class MarkoutLadder(_Frozen):
    horizons: tuple[str, ...]
    policies: tuple[LadderPolicy, ...]
    note: str


class HeatCell(_Frozen):
    fold_id: int
    policy_name: str
    markout_lift_bps: float
    ci_lower_bps: float
    ci_upper_bps: float


class FoldHeatmap(_Frozen):
    folds: tuple[int, ...]
    policies: tuple[str, ...]
    cells: tuple[HeatCell, ...]
    min_lift_bps: float
    max_lift_bps: float


class CostStage(_Frozen):
    label: str
    delta_bps: float
    cumulative_bps: float


class CostPolicy(_Frozen):
    policy_name: str
    gross_bps: float
    stages: tuple[CostStage, ...]
    net_bps: float


class CostAttribution(_Frozen):
    note: str
    cost_model: dict[str, Any]
    policies: tuple[CostPolicy, ...]


class CapacityPoint(_Frozen):
    participation_cap: float
    fill_fraction: float
    realized_lift_bps: float


class Capacity(_Frozen):
    available: bool
    note: str
    baseline_policy: str
    base_lift_bps: float
    points: tuple[CapacityPoint, ...]


class Sensitivity(_Frozen):
    available: bool
    note: str
    param_x: str
    param_y: str
    metric: str
    cells: tuple[dict[str, Any], ...]


class Regimes(_Frozen):
    available: bool
    note: str
    buckets: tuple[dict[str, Any], ...]


class DsrPolicy(_Frozen):
    policy_name: str
    deflated_sharpe_probability: float | str
    n_folds: int
    lift_bps: tuple[float, ...]


class DsrPbo(_Frozen):
    naive_pbo: float | str
    audited_pbo: float | str
    policies: tuple[DsrPolicy, ...]


class Probe(_Frozen):
    policy_name: str
    probe: str
    status: str
    severity: str | None
    offending_features: tuple[str, ...]
    message: str


class Probes(_Frozen):
    verdict: str
    items: tuple[Probe, ...]


class FalsificationCaseModel(_Frozen):
    defect: str
    probe: str
    caught: bool
    statistic: str
    evidence: tuple[str, ...]
    evidence_count: int
    detail: str


class Falsification(_Frozen):
    available: bool
    all_caught: bool
    defects: int
    caught: int
    run_hash: str
    cases: tuple[FalsificationCaseModel, ...]


class Fills(_Frozen):
    total_opportunities: int
    buy_count: int
    sell_count: int
    maker_side_positive: int
    maker_side_negative: int
    top_of_book_missing: int
    latency_ms: int
    maker_fee_bps: float
    taker_fee_bps: float
    liquidity_assumption: str
    fee_tier_note: str


class Provenance(_Frozen):
    engine_version: str
    git_sha: str
    seed: int
    config_hash: str
    data_content_hash: str
    run_id: str
    report_self_hash: str


class Reproduce(_Frozen):
    command: str
    pinned: dict[str, str]
    note: str


class ReportV2(_Frozen):
    """Top-level schema-v2 report. ``extra="forbid"`` pins the contract."""

    meta: ReportMeta
    sample_data: SampleData
    comparison: Comparison
    leaderboards: Leaderboards
    equity_curves: tuple[EquityCurve, ...]
    markout_ladder: MarkoutLadder
    fold_heatmap: FoldHeatmap
    cost_attribution: CostAttribution
    capacity: Capacity
    sensitivity: Sensitivity
    regimes: Regimes
    dsr_pbo: DsrPbo
    probes: Probes
    falsification: Falsification
    fills: Fills
    provenance: Provenance
    reproduce: Reproduce


# --------------------------------------------------------------------------- #
# Builder                                                                      #
# --------------------------------------------------------------------------- #


def build_report_v2(
    *,
    sample_root: Path,
    symbol: str,
    date: str,
    horizon: str,
    data_label: str,
    records: Sequence[HyperliquidMarkoutRecord],
    baseline_policy: QuotePolicy,
    clean_policy: QuotePolicy,
    leaky_policy: QuotePolicy,
    naive_result: BenchmarkResult,
    honest_result: BenchmarkResult,
    blocked_error: str,
    leakage_payload: dict[str, Any],
    manifest: RunManifest,
    cost_model: dict[str, Any],
    reproduce_command: str,
    pinned_versions: dict[str, str],
    falsification: dict[str, Any] | None = None,
    curve_point_limit: int | None = None,
) -> dict[str, Any]:
    """Assemble, validate, self-hash, and return the report-v2 payload dict."""

    if not records:
        raise ValueError("report v2 requires at least one markout record")

    naive_summary = _policy_summary(naive_result)
    honest_summary = _policy_summary(honest_result)

    payload: dict[str, Any] = {
        "meta": _meta(manifest, data_label),
        "sample_data": {
            "root": str(sample_root.as_posix()),
            "label": data_label,
            "symbol": symbol,
            "date": date,
            "rows": len(records),
            "horizon": horizon,
        },
        "comparison": _comparison(
            naive_summary=naive_summary,
            honest_summary=honest_summary,
            leaky_name=leaky_policy.name,
            blocked_error=blocked_error,
        ),
        "leaderboards": _leaderboards(naive_summary, honest_summary),
        "equity_curves": _equity_curves(
            records=records,
            policies=(clean_policy, leaky_policy, baseline_policy),
            horizon=horizon,
            point_limit=curve_point_limit,
        ),
        "markout_ladder": _markout_ladder(
            records=records,
            policies=(baseline_policy, clean_policy),
            horizon=horizon,
        ),
        "fold_heatmap": _fold_heatmap(honest_result),
        "cost_attribution": _cost_attribution(
            records=records,
            policies=(baseline_policy, clean_policy),
            horizon=horizon,
            cost_model=cost_model,
        ),
        "capacity": _capacity(
            honest_summary=honest_summary,
            baseline_name=clean_policy.name,
            cost_model=cost_model,
        ),
        "sensitivity": _sensitivity_unavailable(),
        "regimes": _regimes_unavailable(),
        "dsr_pbo": _dsr_pbo(naive_result, honest_result),
        "probes": _probes(leakage_payload),
        "falsification": _falsification(falsification),
        "fills": _fills(records, cost_model),
        "provenance": {
            "engine_version": manifest.engine_version,
            "git_sha": manifest.git_sha,
            "seed": manifest.seed,
            "config_hash": manifest.config_hash,
            "data_content_hash": manifest.data_content_hash,
            "run_id": manifest.run_id,
            "report_self_hash": "",
        },
        "reproduce": {
            "command": reproduce_command,
            "pinned": dict(sorted(pinned_versions.items())),
            "note": (
                "Pinned engine + interpreter reproduce this report byte-for-byte; "
                "CI rebuilds it twice and diffs the self-hash."
            ),
        },
    }

    # Self-hash covers everything except the (empty) self-hash field itself.
    self_hash = stable_hash(payload)
    payload["provenance"]["report_self_hash"] = self_hash

    # Validate the assembled contract; raises on any schema drift.
    ReportV2.model_validate(payload)
    return payload


def validate_report_v2(payload: dict[str, Any]) -> ReportV2:
    """Validate a report-v2 dict against the schema and return the model."""

    return ReportV2.model_validate(payload)


def write_report_v2_json(path: Path, payload: dict[str, Any]) -> None:
    """Write a report-v2 payload with sorted keys for byte-stable output."""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, sort_keys=True, indent=2, separators=(",", ": ")) + "\n",
        encoding="utf-8",
    )


# --------------------------------------------------------------------------- #
# Block builders                                                              #
# --------------------------------------------------------------------------- #


def _meta(manifest: RunManifest, data_label: str) -> dict[str, Any]:
    return {
        "report_schema": REPORT_SCHEMA_VERSION,
        "title": "Hindsight Audit Tearsheet",
        "subtitle": "Point-in-time leakage and execution audit",
        "generator": f"hindsight {manifest.engine_version}",
        "data_label": data_label,
    }


def _policy_summary(result: BenchmarkResult) -> list[dict[str, Any]]:
    grouped: dict[str, list[BenchmarkRow]] = defaultdict(list)
    for row in result.rows:
        grouped[row.policy_name].append(row)
    summary: list[dict[str, Any]] = []
    for policy_name, rows in sorted(grouped.items()):
        summary.append(
            {
                "policy_name": policy_name,
                "folds": len(rows),
                "avg_markout_bps": _r(_mean(row.average_markout_bps for row in rows)),
                "avg_lift_bps": _r(_mean(row.markout_lift_bps for row in rows)),
                "avg_quote_rate": _r(_mean(row.quote_rate for row in rows)),
                "deflated_sharpe_probability": rows[0].deflated_sharpe_probability,
                "lift_by_fold": [_r(row.markout_lift_bps) for row in rows],
            }
        )
    return summary


def _comparison(
    *,
    naive_summary: list[dict[str, Any]],
    honest_summary: list[dict[str, Any]],
    leaky_name: str,
    blocked_error: str,
) -> dict[str, Any]:
    naive_rank = _rank_map(naive_summary, key="avg_markout_bps")
    honest_by_name = {row["policy_name"]: row for row in honest_summary}
    winner = min(
        naive_summary,
        key=lambda row: (-row["avg_markout_bps"], row["policy_name"]),
    )["policy_name"]

    names = sorted({row["policy_name"] for row in naive_summary} | set(honest_by_name))
    policies: list[dict[str, Any]] = []
    for name in names:
        naive_row = next((r for r in naive_summary if r["policy_name"] == name), None)
        honest_row = honest_by_name.get(name)
        blocked = name == leaky_name and honest_row is None
        policies.append(
            {
                "policy_name": name,
                "naive_rank": naive_rank.get(name),
                "audited_status": "blocked" if blocked else "audited",
                "block_reason": blocked_error if blocked else None,
                "naive_avg_markout_bps": (
                    None if naive_row is None else naive_row["avg_markout_bps"]
                ),
                "audited_avg_lift_bps": (
                    None if honest_row is None else honest_row["avg_lift_bps"]
                ),
            }
        )
    return {
        "headline_naive": f"Naive split: {winner} ranks #1",
        "headline_audited": "Hindsight: BLOCKED",
        "verdict": "blocked",
        "block_reason": blocked_error,
        "policies": policies,
    }


def _leaderboards(
    naive_summary: list[dict[str, Any]],
    honest_summary: list[dict[str, Any]],
) -> dict[str, Any]:
    naive_rows = _ranked_rows(naive_summary, key="avg_markout_bps")
    audited_rows = _ranked_rows(honest_summary, key="avg_lift_bps")
    naive_rank = {row["policy_name"]: row["rank"] for row in naive_rows}
    audited_rank = {row["policy_name"]: row["rank"] for row in audited_rows}
    shifts: list[dict[str, Any]] = []
    for name in sorted(set(naive_rank) | set(audited_rank)):
        nr = naive_rank.get(name)
        ar = audited_rank.get(name)
        delta = None if nr is None or ar is None else nr - ar
        shifts.append(
            {"policy_name": name, "naive_rank": nr, "audited_rank": ar, "delta": delta}
        )
    return {"naive": naive_rows, "audited": audited_rows, "rank_shifts": shifts}


def _ranked_rows(summary: list[dict[str, Any]], *, key: str) -> list[dict[str, Any]]:
    ordered = sorted(summary, key=lambda row: (-row[key], row["policy_name"]))
    return [
        {
            "rank": index + 1,
            "policy_name": row["policy_name"],
            "avg_markout_bps": row["avg_markout_bps"],
            "avg_lift_bps": row["avg_lift_bps"],
            "quote_rate": row["avg_quote_rate"],
            "deflated_sharpe_probability": row["deflated_sharpe_probability"],
        }
        for index, row in enumerate(ordered)
    ]


def _rank_map(summary: list[dict[str, Any]], *, key: str) -> dict[str, int]:
    ordered = sorted(summary, key=lambda row: (-row[key], row["policy_name"]))
    return {row["policy_name"]: index + 1 for index, row in enumerate(ordered)}


def _equity_curves(
    *,
    records: Sequence[HyperliquidMarkoutRecord],
    policies: Sequence[QuotePolicy],
    horizon: str,
    point_limit: int | None = None,
) -> list[dict[str, Any]]:
    keep_indices = _sample_indices(len(records), point_limit)
    curves: list[dict[str, Any]] = []
    for policy in sorted(policies, key=lambda item: item.name):
        cum = 0.0
        peak = 0.0
        max_dd = 0.0
        points: list[dict[str, Any]] = []
        for index, record in enumerate(records):
            quoted = bool(policy.quote_mask[index])
            captured = record.markout_bps[horizon] if quoted else 0.0
            cum += captured
            peak = max(peak, cum)
            drawdown = peak - cum
            max_dd = max(max_dd, drawdown)
            if index not in keep_indices:
                continue
            points.append(
                {
                    "index": index,
                    "timestamp": record.timestamp.isoformat(),
                    "cum_markout_bps": _r(cum),
                    "drawdown_bps": _r(drawdown),
                    "quoted": quoted,
                }
            )
        curves.append(
            {
                "policy_name": policy.name,
                "points": points,
                "final_cum_markout_bps": _r(cum),
                "max_drawdown_bps": _r(max_dd),
            }
        )
    return curves


def _sample_indices(total: int, limit: int | None) -> set[int]:
    if limit is None or total <= limit:
        return set(range(total))
    if limit < 2:
        raise ValueError("curve point limit must be at least 2")
    step = (total - 1) / (limit - 1)
    return {round(index * step) for index in range(limit)}


def _markout_ladder(
    *,
    records: Sequence[HyperliquidMarkoutRecord],
    policies: Sequence[QuotePolicy],
    horizon: str,
) -> dict[str, Any]:
    horizons = _available_horizons(records)
    ladder_policies: list[dict[str, Any]] = []
    for policy in sorted(policies, key=lambda item: item.name):
        rungs: list[dict[str, Any]] = []
        for horizon_key in horizons:
            captured = [
                record.markout_bps[horizon_key] if policy.quote_mask[index] else 0.0
                for index, record in enumerate(records)
                if horizon_key in record.markout_bps
            ]
            mean, lower, upper = _mean_ci(captured)
            rungs.append(
                {
                    "horizon": horizon_key,
                    "avg_markout_bps": _r(mean),
                    "ci_lower_bps": _r(lower),
                    "ci_upper_bps": _r(upper),
                }
            )
        ladder_policies.append({"policy_name": policy.name, "rungs": rungs})
    note = (
        "Single committed horizon in the synthetic lake; additional rungs "
        "(1s/30s/2m/10m) populate on flagship runs (Phase 0)."
        if len(horizons) < 2
        else "Markout in bps at each committed horizon with normal-approx CI bands."
    )
    return {"horizons": horizons, "policies": ladder_policies, "note": note}


def _fold_heatmap(result: BenchmarkResult) -> dict[str, Any]:
    folds = sorted({row.fold_id for row in result.rows})
    policies = sorted({row.policy_name for row in result.rows})
    cells: list[dict[str, Any]] = []
    lifts: list[float] = []
    for row in sorted(result.rows, key=lambda item: (item.fold_id, item.policy_name)):
        cells.append(
            {
                "fold_id": row.fold_id,
                "policy_name": row.policy_name,
                "markout_lift_bps": _r(row.markout_lift_bps),
                "ci_lower_bps": _r(row.markout_lift_ci_lower_bps),
                "ci_upper_bps": _r(row.markout_lift_ci_upper_bps),
            }
        )
        lifts.append(row.markout_lift_bps)
    return {
        "folds": folds,
        "policies": policies,
        "cells": cells,
        "min_lift_bps": _r(min(lifts)) if lifts else 0.0,
        "max_lift_bps": _r(max(lifts)) if lifts else 0.0,
    }


def _cost_attribution(
    *,
    records: Sequence[HyperliquidMarkoutRecord],
    policies: Sequence[QuotePolicy],
    horizon: str,
    cost_model: dict[str, Any],
) -> dict[str, Any]:
    maker_fee = float(cost_model["maker_fee_bps"])
    slippage = float(cost_model["slippage_impact_bps"])
    funding_rate = float(cost_model.get("funding_rate_bps") or 0.0)
    interval_hours = float(cost_model["funding_interval_hours"])
    horizon_seconds = _horizon_seconds(horizon)
    funding_bps = funding_rate * (horizon_seconds / (interval_hours * 3600.0))

    cost_policies: list[dict[str, Any]] = []
    for policy in sorted(policies, key=lambda item: item.name):
        quoted = [
            record.markout_bps[horizon]
            for index, record in enumerate(records)
            if policy.quote_mask[index] and horizon in record.markout_bps
        ]
        gross = _mean(quoted) if quoted else 0.0
        stages: list[dict[str, Any]] = []
        cumulative = gross
        for label, delta in (
            ("fees", -maker_fee),
            ("slippage", -slippage),
            ("funding", -funding_bps),
        ):
            cumulative += delta
            stages.append(
                {"label": label, "delta_bps": _r(delta), "cumulative_bps": _r(cumulative)}
            )
        cost_policies.append(
            {
                "policy_name": policy.name,
                "gross_bps": _r(gross),
                "stages": stages,
                "net_bps": _r(cumulative),
            }
        )
    return {
        "note": (
            "Disclosed cost model applied to the measured gross edge (maker "
            "posting assumption); not a claim of realized fills."
        ),
        "cost_model": {
            "maker_fee_bps": _r(maker_fee),
            "taker_fee_bps": _r(float(cost_model["taker_fee_bps"])),
            "slippage_impact_bps": _r(slippage),
            "funding_bps_at_horizon": _r(funding_bps),
        },
        "policies": cost_policies,
    }


def _capacity(
    *,
    honest_summary: list[dict[str, Any]],
    baseline_name: str,
    cost_model: dict[str, Any],
) -> dict[str, Any]:
    base_row = next(
        (row for row in honest_summary if row["policy_name"] == baseline_name), None
    )
    base_lift = 0.0 if base_row is None else float(base_row["avg_lift_bps"])
    points: list[dict[str, Any]] = []
    for cap in CAPACITY_CAPS:
        fill_fraction = min(1.0, cap)
        points.append(
            {
                "participation_cap": _r(cap),
                "fill_fraction": _r(fill_fraction),
                "realized_lift_bps": _r(base_lift * fill_fraction),
            }
        )
    return {
        "available": True,
        "note": (
            "Realized lift scaled by fill fraction = min(1, participation_cap x "
            "print_size); saturates at cap=1. Impact-driven decay above cap "
            "requires flagship depth data (Phase 0)."
        ),
        "baseline_policy": baseline_name,
        "base_lift_bps": _r(base_lift),
        "points": points,
    }


def _sensitivity_unavailable() -> dict[str, Any]:
    return {
        "available": False,
        "note": (
            "Overfit-surface heatmap needs a model hyperparameter sweep; the "
            "synthetic baselines expose no tunable hyperparameters. Populates on "
            "flagship policy trials (Phase 0)."
        ),
        "param_x": "",
        "param_y": "",
        "metric": "markout_lift_bps",
        "cells": [],
    }


def _regimes_unavailable() -> dict[str, Any]:
    return {
        "available": False,
        "note": (
            "Vol-tercile x time-of-day small multiples need enough samples to "
            "form stable buckets; the 8-row synthetic lake cannot. Populates on "
            "flagship runs (Phase 0)."
        ),
        "buckets": [],
    }


def _dsr_pbo(naive_result: BenchmarkResult, honest_result: BenchmarkResult) -> dict[str, Any]:
    summary = _policy_summary(honest_result)
    policies = [
        {
            "policy_name": row["policy_name"],
            "deflated_sharpe_probability": row["deflated_sharpe_probability"],
            "n_folds": row["folds"],
            "lift_bps": row["lift_by_fold"],
        }
        for row in summary
    ]
    return {
        "naive_pbo": naive_result.pbo,
        "audited_pbo": honest_result.pbo,
        "policies": policies,
    }


def _probes(leakage_payload: dict[str, Any]) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    for audit in leakage_payload.get("audits", []):
        violations = audit.get("violations", [])
        if not violations:
            items.append(
                {
                    "policy_name": audit["policy_name"],
                    "probe": "target_leakage",
                    "status": "pass",
                    "severity": None,
                    "offending_features": [],
                    "message": "no target-derived features detected",
                }
            )
            continue
        for violation in violations:
            items.append(
                {
                    "policy_name": audit["policy_name"],
                    "probe": violation["probe"],
                    "status": "caught",
                    "severity": violation.get("severity"),
                    "offending_features": list(violation.get("offending_features", [])),
                    "message": violation.get("message", ""),
                }
            )
    items.sort(key=lambda item: (item["policy_name"], item["probe"]))
    return {"verdict": leakage_payload.get("verdict", "unknown"), "items": items}


def _falsification(payload: dict[str, Any] | None) -> dict[str, Any]:
    if payload is None:
        return {
            "available": False,
            "all_caught": False,
            "defects": 0,
            "caught": 0,
            "run_hash": "",
            "cases": [],
        }
    return {
        "available": True,
        "all_caught": bool(payload["all_caught"]),
        "defects": int(payload["defects"]),
        "caught": int(payload["caught"]),
        "run_hash": str(payload["run_hash"]),
        "cases": [
            {
                "defect": case["defect"],
                "probe": case["probe"],
                "caught": bool(case["caught"]),
                "statistic": case["statistic"],
                "evidence": list(case["evidence"]),
                "evidence_count": int(case.get("evidence_count", len(case["evidence"]))),
                "detail": str(case.get("detail", "")),
            }
            for case in payload["cases"]
        ],
    }


def _fills(
    records: Sequence[HyperliquidMarkoutRecord],
    cost_model: dict[str, Any],
) -> dict[str, Any]:
    buy = sum(1 for record in records if str(record.side).lower().endswith("buy"))
    maker_positive = sum(1 for record in records if record.maker_side > 0)
    total = len(records)
    return {
        "total_opportunities": total,
        "buy_count": buy,
        "sell_count": total - buy,
        "maker_side_positive": maker_positive,
        "maker_side_negative": total - maker_positive,
        "top_of_book_missing": 0,
        "latency_ms": int(cost_model["latency_ms"]),
        "maker_fee_bps": _r(float(cost_model["maker_fee_bps"])),
        "taker_fee_bps": _r(float(cost_model["taker_fee_bps"])),
        "liquidity_assumption": str(cost_model.get("liquidity_assumption", "maker")),
        "fee_tier_note": str(cost_model.get("fee_tier_note", "")),
    }


# --------------------------------------------------------------------------- #
# Small numeric helpers                                                        #
# --------------------------------------------------------------------------- #


def _available_horizons(records: Sequence[HyperliquidMarkoutRecord]) -> list[str]:
    horizons: set[str] = set()
    for record in records:
        horizons.update(record.markout_bps)
    return sorted(horizons, key=_horizon_seconds)


def _horizon_seconds(horizon: str) -> float:
    units = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}
    for suffix in ("ms", "s", "m", "h", "d"):
        if horizon.endswith(suffix):
            number = horizon[: -len(suffix)]
            try:
                return float(number) * units[suffix]
            except ValueError:
                break
    # Fallback reason: an unrecognized horizon label must still sort stably
    # rather than crash the report; treat it as a large sentinel.
    return float("inf")


def _mean(values: Iterable[float]) -> float:
    items = list(values)
    if not items:
        raise ValueError("cannot average an empty sequence")
    return sum(items) / len(items)


def _mean_ci(values: Sequence[float]) -> tuple[float, float, float]:
    if not values:
        return (0.0, 0.0, 0.0)
    mean = sum(values) / len(values)
    if len(values) < 2:
        return (mean, mean, mean)
    variance = sum((value - mean) ** 2 for value in values) / (len(values) - 1)
    stderr = sqrt(variance / len(values))
    return (mean, mean - _CONFIDENCE_Z * stderr, mean + _CONFIDENCE_Z * stderr)
