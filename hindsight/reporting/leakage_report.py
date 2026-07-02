"""Machine-readable leakage audit reports."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from hindsight.evaluation.benchmark import QuotePolicy
from hindsight.evaluation.leakage import probe_target_leakage


def quote_policy_leakage_payload(
    *,
    policies: tuple[QuotePolicy, ...],
    target_name: str,
) -> dict[str, Any]:
    """Build a deterministic leakage report for benchmark quote policies."""

    audits: list[dict[str, Any]] = []
    for policy in policies:
        violations = [
            {
                "probe": violation.probe,
                "severity": violation.severity,
                "offending_features": [
                    policy.feature_names[index] for index in violation.indices
                ],
                "verdict": _violation_verdict(violation.severity),
                "message": violation.message,
            }
            for violation in probe_target_leakage(policy.feature_names, target_name)
        ]
        audits.append(
            {
                "policy_name": policy.name,
                "target_name": target_name,
                "verdict": "fail"
                if any(violation["severity"] == "hard" for violation in violations)
                else "pass",
                "violations": violations,
            }
        )
    return {
        "schema_version": "1.0.0",
        "verdict": "fail" if any(audit["verdict"] == "fail" for audit in audits) else "pass",
        "audits": audits,
    }


def write_leakage_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _violation_verdict(severity: str) -> str:
    return "fail" if severity == "hard" else "warn"
