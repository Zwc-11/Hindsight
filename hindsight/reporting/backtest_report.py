"""Naive-vs-realistic report writers."""

from __future__ import annotations

import json
from pathlib import Path

from hindsight.execution.compare import ComparisonResult
from hindsight.reporting.curves import ascii_sparkline, equity_curve_json


def comparison_payload(result: ComparisonResult) -> dict[str, object]:
    return {
        "verdict": result.verdict,
        "naive": {
            "events_processed": result.naive.events_processed,
            "orders_emitted": result.naive.orders_emitted,
            "fills": len(result.naive.fills),
            "final_equity": result.naive.final_state.equity,
            "sharpe": result.naive_sharpe,
            "equity_curve": equity_curve_json(result.naive.equity_curve),
        },
        "realistic": {
            "events_processed": result.realistic.events_processed,
            "orders_emitted": result.realistic.orders_emitted,
            "fills": len(result.realistic.fills),
            "final_equity": result.realistic.final_state.equity,
            "sharpe": result.realistic_sharpe,
            "warnings": list(result.realistic.warnings),
            "equity_curve": equity_curve_json(result.realistic.equity_curve),
        },
        "sharpe_delta": result.sharpe_delta,
    }


def write_comparison_json(path: Path, result: ComparisonResult) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(comparison_payload(result), sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def write_comparison_markdown(path: Path, result: ComparisonResult) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    naive_spark = ascii_sparkline([point.equity for point in result.naive.equity_curve])
    realistic_spark = ascii_sparkline([point.equity for point in result.realistic.equity_curve])
    lines = [
        "# Hindsight Naive vs Realistic",
        "",
        "## Verdict",
        "",
        result.verdict,
        "",
        "## Curves",
        "",
        f"- Naive: `{naive_spark}`",
        f"- Realistic: `{realistic_spark}`",
        "",
        "## Metrics",
        "",
        f"- Naive final equity: `{result.naive.final_state.equity:.2f}`",
        f"- Realistic final equity: `{result.realistic.final_state.equity:.2f}`",
        f"- Naive Sharpe: `{result.naive_sharpe:.4f}`",
        f"- Realistic Sharpe: `{result.realistic_sharpe:.4f}`",
        f"- Sharpe delta: `{result.sharpe_delta:.4f}`",
        f"- Fees paid: `{result.realistic.final_state.fees_paid:.6f}`",
        f"- Funding paid: `{result.realistic.final_state.funding_paid:.6f}`",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")
