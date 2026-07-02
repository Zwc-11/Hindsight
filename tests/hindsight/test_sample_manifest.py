from __future__ import annotations

import json
from pathlib import Path

import pyarrow.parquet as pq


def test_sample_manifest_row_counts_match_files() -> None:
    root = Path("examples/sample_data")
    manifest = json.loads((root / "data_manifest.json").read_text(encoding="utf-8"))

    for dataset in manifest["datasets"]:
        for file_record in dataset["files"]:
            path = root / file_record["path"]
            assert path.exists()
            assert pq.read_table(path).num_rows == file_record["rows"]


def test_committed_demo_manifest_uses_hyperliquid_sample_hash() -> None:
    sample_manifest = json.loads(
        Path("examples/sample_data/data_manifest.json").read_text(encoding="utf-8")
    )
    demo_manifest = json.loads(
        Path("examples/runs/demo/manifest.json").read_text(encoding="utf-8")
    )

    hyperliquid = next(
        dataset
        for dataset in sample_manifest["datasets"]
        if dataset["name"] == "hyperliquid_markout"
    )
    assert demo_manifest["data_content_hash"] == hyperliquid["content_hash"]
