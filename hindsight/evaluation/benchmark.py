"""Benchmark quote policies over markout-labelled opportunities."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from hindsight.core.hashing import run_hash
from hindsight.data.hyperliquid_adapter import HyperliquidMarkoutRecord
from hindsight.evaluation.leakage import LeakageError, probe_target_leakage
from hindsight.evaluation.metrics import markout_lift
from hindsight.evaluation.walk_forward import LabelInterval, WalkForwardFold


@dataclass(frozen=True, slots=True)
class QuotePolicy:
    """Deterministic quote/skip decisions for one benchmark policy."""

    name: str
    quote_mask: tuple[bool, ...]
    feature_names: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("policy name cannot be empty")


@dataclass(frozen=True, slots=True)
class BenchmarkRow:
    """One policy's fold-level benchmark result."""

    fold_id: int
    policy_name: str
    rows: int
    quote_rate: float
    average_markout_bps: float
    baseline_average_markout_bps: float
    markout_lift_bps: float
    markout_lift_ci_lower_bps: float
    markout_lift_ci_upper_bps: float


@dataclass(frozen=True, slots=True)
class BenchmarkResult:
    """Deterministic benchmark result and content hash."""

    rows: tuple[BenchmarkRow, ...]
    run_hash: str


def label_intervals(
    records: list[HyperliquidMarkoutRecord],
    *,
    horizon: timedelta,
) -> list[LabelInterval]:
    if horizon <= timedelta(0):
        raise ValueError("horizon must be positive")
    return [
        LabelInterval(
            index=index,
            start=record.timestamp,
            end=record.timestamp + horizon,
        )
        for index, record in enumerate(records)
    ]


def benchmark_quote_policies(
    *,
    records: list[HyperliquidMarkoutRecord],
    folds: tuple[WalkForwardFold, ...],
    policies: tuple[QuotePolicy, ...],
    baseline_policy_name: str,
    horizon_key: str,
    target_name: str,
    fail_on_leakage: bool,
) -> BenchmarkResult:
    if not records:
        raise ValueError("benchmark requires at least one markout record")
    if not folds:
        raise ValueError("benchmark requires at least one fold")
    policy_by_name = {policy.name: policy for policy in policies}
    if len(policy_by_name) != len(policies):
        raise ValueError("policy names must be unique")
    if baseline_policy_name not in policy_by_name:
        raise ValueError("baseline policy is missing")
    for policy in policies:
        if len(policy.quote_mask) != len(records):
            raise ValueError(f"policy {policy.name} mask length does not match records")
        violations = probe_target_leakage(policy.feature_names, target_name)
        if fail_on_leakage and violations:
            raise LeakageError(f"policy {policy.name} failed leakage probe")

    baseline = policy_by_name[baseline_policy_name]
    rows: list[BenchmarkRow] = []
    for fold in folds:
        test_indices = _validated_test_indices(fold, len(records))
        baseline_values = _policy_values(
            records,
            baseline,
            test_indices=test_indices,
            horizon_key=horizon_key,
        )
        baseline_average = sum(baseline_values) / len(baseline_values)
        for policy in policies:
            values = _policy_values(
                records,
                policy,
                test_indices=test_indices,
                horizon_key=horizon_key,
            )
            lift = markout_lift(values, baseline_values)
            quoted = sum(1 for index in test_indices if policy.quote_mask[index])
            rows.append(
                BenchmarkRow(
                    fold_id=fold.fold_id,
                    policy_name=policy.name,
                    rows=len(test_indices),
                    quote_rate=quoted / len(test_indices),
                    average_markout_bps=sum(values) / len(values),
                    baseline_average_markout_bps=baseline_average,
                    markout_lift_bps=lift.value,
                    markout_lift_ci_lower_bps=lift.lower,
                    markout_lift_ci_upper_bps=lift.upper,
                )
            )
    ordered_rows = tuple(sorted(rows, key=lambda row: (row.fold_id, row.policy_name)))
    return BenchmarkResult(rows=ordered_rows, run_hash=_benchmark_hash(ordered_rows))


def quote_all_policy(record_count: int, *, name: str = "ofi_quote") -> QuotePolicy:
    return QuotePolicy(
        name=name,
        quote_mask=tuple(True for _ in range(record_count)),
        feature_names=("maker_side",),
    )


def maker_side_policy(records: list[HyperliquidMarkoutRecord]) -> QuotePolicy:
    return QuotePolicy(
        name="maker_side_filter",
        quote_mask=tuple(record.maker_side > 0 for record in records),
        feature_names=("maker_side",),
    )


def leaky_markout_policy(
    records: list[HyperliquidMarkoutRecord],
    *,
    horizon_key: str,
) -> QuotePolicy:
    return QuotePolicy(
        name="leaky",
        quote_mask=tuple(record.markout_bps[horizon_key] >= 0 for record in records),
        feature_names=(f"markout_bps_{horizon_key}",),
    )


def _validated_test_indices(fold: WalkForwardFold, record_count: int) -> tuple[int, ...]:
    if not fold.test_indices:
        raise ValueError(f"fold {fold.fold_id} has no test rows")
    for index in fold.test_indices:
        if index < 0 or index >= record_count:
            raise ValueError(f"fold {fold.fold_id} references unknown row {index}")
    return fold.test_indices


def _policy_values(
    records: list[HyperliquidMarkoutRecord],
    policy: QuotePolicy,
    *,
    test_indices: tuple[int, ...],
    horizon_key: str,
) -> list[float]:
    values: list[float] = []
    for index in test_indices:
        record = records[index]
        if horizon_key not in record.markout_bps:
            raise ValueError(f"record {index} missing markout horizon {horizon_key}")
        values.append(record.markout_bps[horizon_key] if policy.quote_mask[index] else 0.0)
    return values


def _benchmark_hash(rows: tuple[BenchmarkRow, ...]) -> str:
    return run_hash([
        {
            "fold_id": row.fold_id,
            "policy_name": row.policy_name,
            "rows": row.rows,
            "quote_rate": row.quote_rate,
            "average_markout_bps": row.average_markout_bps,
            "baseline_average_markout_bps": row.baseline_average_markout_bps,
            "markout_lift_bps": row.markout_lift_bps,
            "markout_lift_ci_lower_bps": row.markout_lift_ci_lower_bps,
            "markout_lift_ci_upper_bps": row.markout_lift_ci_upper_bps,
        }
        for row in rows
    ])
