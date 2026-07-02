"""CSV writer for Hindsight benchmark leaderboards."""

from __future__ import annotations

import csv
from pathlib import Path

from hindsight.evaluation.benchmark import BenchmarkResult

LEADERBOARD_COLUMNS = (
    "fold_id",
    "policy_name",
    "rows",
    "quote_rate",
    "average_markout_bps",
    "baseline_average_markout_bps",
    "markout_lift_bps",
    "markout_lift_ci_lower_bps",
    "markout_lift_ci_upper_bps",
)


def write_leaderboard_csv(path: Path, result: BenchmarkResult) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=LEADERBOARD_COLUMNS)
        writer.writeheader()
        for row in result.rows:
            writer.writerow({
                "fold_id": row.fold_id,
                "policy_name": row.policy_name,
                "rows": row.rows,
                "quote_rate": f"{row.quote_rate:.12g}",
                "average_markout_bps": f"{row.average_markout_bps:.12g}",
                "baseline_average_markout_bps": f"{row.baseline_average_markout_bps:.12g}",
                "markout_lift_bps": f"{row.markout_lift_bps:.12g}",
                "markout_lift_ci_lower_bps": f"{row.markout_lift_ci_lower_bps:.12g}",
                "markout_lift_ci_upper_bps": f"{row.markout_lift_ci_upper_bps:.12g}",
            })
