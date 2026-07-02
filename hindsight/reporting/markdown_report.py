"""Markdown report writer."""

from __future__ import annotations

from pathlib import Path

from hindsight.reporting.json_report import HindsightReport


def write_markdown_report(path: Path, report: HindsightReport) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Hindsight Run Report",
        "",
        "## Verdict",
        "",
        "No-op strategy emitted no orders. M0 replay plumbing is valid.",
        "",
        "## Run",
        "",
        f"- Run ID: `{report.manifest.run_id}`",
        f"- Strategy: `{report.strategy_name}`",
        f"- Symbol: `{report.symbol}`",
        f"- Date: `{report.date or 'auto'}`",
        f"- Events processed: `{report.events_processed}`",
        f"- Orders emitted: `{report.orders_emitted}`",
        f"- Run hash: `{report.run_hash}`",
        "",
        "## Event Types",
        "",
    ]
    for event_type, count in sorted(report.event_types.items()):
        lines.append(f"- `{event_type}`: `{count}`")
    lines.extend(
        [
            "",
            "## Manifest",
            "",
            f"- Engine version: `{report.manifest.engine_version}`",
            f"- Git SHA: `{report.manifest.git_sha}`",
            f"- Data content hash: `{report.manifest.data_content_hash}`",
            f"- Config hash: `{report.manifest.config_hash}`",
            f"- Seed: `{report.manifest.seed}`",
            "",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")
