"""Columnar export of native research artifacts; no strategy or accounting logic."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Sequence
from importlib import import_module
from pathlib import Path
from typing import Any


def export_warehouse(report_path: Path, output_dir: Path) -> dict[str, int]:
    raw = report_path.read_bytes()
    report: dict[str, Any] = json.loads(raw)
    if report.get("schema_version") != 1 or not isinstance(report.get("trials"), list):
        raise ValueError("Expected a schema-v1 native research report")
    if output_dir.exists():
        raise FileExistsError(f"Refusing to overwrite warehouse: {output_dir}")
    try:
        pa = import_module("pyarrow")
        parquet = import_module("pyarrow.parquet")
        duckdb = import_module("duckdb")
    except ImportError as exc:
        raise RuntimeError('Install the economic extra: pip install -e ".[economic]"') from exc
    decisions: list[dict[str, Any]] = []
    scores: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    for trial in report["trials"]:
        variant = trial["variant"]
        for row in trial["decisions"]:
            features = row.get("features") or {}
            decisions.append(
                {
                    "variant": variant,
                    "at_ms": row["at"],
                    "prediction": row["prediction"],
                    "target_shares": row["target_shares"],
                    "training_count": row["training_count"],
                    "trained_through_ms": row["trained_through"],
                    "model_hash": row["model_hash"],
                    "feature_values_json": json.dumps(features.get("values", [])),
                    "observation_ids_json": json.dumps(features.get("observation_ids", [])),
                }
            )
        for row in trial["scores"]:
            scores.append({"variant": variant, **row})
        for row in trial["account"]["events"]:
            events.append({"variant": variant, **row})
    schemas = {
        "decisions": pa.schema(
            [
                ("variant", pa.string()),
                ("at_ms", pa.int64()),
                ("prediction", pa.float64()),
                ("target_shares", pa.int64()),
                ("training_count", pa.int64()),
                ("trained_through_ms", pa.int64()),
                ("model_hash", pa.string()),
                ("feature_values_json", pa.string()),
                ("observation_ids_json", pa.string()),
            ]
        ),
        "scores": pa.schema(
            [
                ("variant", pa.string()),
                ("at", pa.int64()),
                ("target", pa.float64()),
                ("prediction", pa.float64()),
                ("squared_error", pa.float64()),
            ]
        ),
        "events": pa.schema(
            [
                ("variant", pa.string()),
                ("order_id", pa.uint64()),
                ("at", pa.int64()),
                ("phase", pa.string()),
                ("shares", pa.int64()),
                ("price_micros", pa.int64()),
                ("fee_micros", pa.int64()),
                ("cash_micros", pa.int64()),
                ("position", pa.int64()),
                ("reason", pa.string()),
            ]
        ),
    }
    rows = {"decisions": decisions, "scores": scores, "events": events}
    output_dir.mkdir(parents=True, exist_ok=False)
    connection = duckdb.connect(str(output_dir / "research.duckdb"))
    try:
        for name, records in rows.items():
            path = output_dir / f"{name}.parquet"
            table = pa.Table.from_pylist(records, schema=schemas[name])
            parquet.write_table(table, path, compression="zstd")
            # Identifiers come from the fixed table registry, never user-provided SQL.
            connection.execute(f"CREATE TABLE {name} AS SELECT * FROM read_parquet(?)", [str(path)])
        connection.execute("CREATE TABLE provenance (key VARCHAR, value VARCHAR)")
        connection.executemany(
            "INSERT INTO provenance VALUES (?, ?)",
            [
                ("report_sha256", hashlib.sha256(raw).hexdigest()),
                ("input_sha256", str(report["input_hash"])),
                ("synthetic", str(report["synthetic"])),
                ("schema_version", "1"),
            ],
        )
        connection.execute("CHECKPOINT")
    finally:
        connection.close()
    counts = {name: len(records) for name, records in rows.items()}
    (output_dir / "_SUCCESS.json").write_text(json.dumps(counts, indent=2) + "\n")
    return counts


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args(argv)
    counts = export_warehouse(args.report, args.output)
    print(json.dumps(counts, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
