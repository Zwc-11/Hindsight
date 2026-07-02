"""JSON report model and writer."""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from hindsight.reporting.manifest import RunManifest


class HindsightReport(BaseModel):
    """Schema-valid M0 report for a no-op strategy run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    strategy_name: str = Field(min_length=1)
    symbol: str = Field(min_length=1)
    date: str | None
    events_processed: int = Field(ge=0)
    orders_emitted: int = Field(ge=0)
    first_timestamp: str
    last_timestamp: str
    event_types: dict[str, int]
    run_hash: str = Field(min_length=1)
    manifest: RunManifest


def write_json_report(path: Path, report: HindsightReport) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = report.model_dump(mode="json")
    path.write_text(
        json.dumps(payload, sort_keys=True, indent=2, separators=(",", ": ")) + "\n",
        encoding="utf-8",
    )
