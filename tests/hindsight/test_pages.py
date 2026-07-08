from __future__ import annotations

from pathlib import Path

import pytest

from hindsight.cli import main
from hindsight.reporting import pages
from hindsight.reporting.pages import build_pages_site


def test_build_pages_site_publishes_demo_and_benchmarks(tmp_path: Path) -> None:
    site = build_pages_site(repo_root=Path.cwd(), output_dir=tmp_path / "site")

    assert site.root == tmp_path / "site"
    assert site.index_path.is_file()
    assert site.demo_tearsheet.is_file()
    assert site.benchmark_tearsheets
    assert len(site.research_log_rows) >= 2
    assert (site.root / ".nojekyll").is_file()
    assert any(
        path.as_posix().endswith("2026-07-07-flagship/tearsheet.html")
        for path in site.benchmark_tearsheets
    )

    index = site.index_path.read_text(encoding="utf-8")
    assert "demo/tearsheet.html" in index
    assert "benchmarks/2026-07-07-flagship/tearsheet.html" in index
    assert "Report Ledger" in index
    assert 'id="report-log"' in index
    assert 'data-sort-column="0"' in index
    assert 'class="spark"' in index
    assert "SOL/ETH/BTC" in index
    assert "BLOCKED" in index
    assert "sort(compare(kind, column, direction))" in index
    assert "http://" not in index
    assert "https://" not in index


def test_pages_cli_builds_site(tmp_path: Path) -> None:
    out = tmp_path / "site"

    assert main(["pages", "--output-dir", str(out)]) == 0
    assert (out / "index.html").is_file()
    assert (out / "demo" / "tearsheet.html").is_file()
    assert (out / "demo" / "report.json").is_file()


def test_pages_builder_rejects_source_artifact_directory() -> None:
    with pytest.raises(ValueError, match="must not overwrite"):
        build_pages_site(
            repo_root=Path.cwd(),
            output_dir=Path.cwd() / "docs" / "benchmarks" / "_site",
        )


def test_report_copy_skips_raw_data_files(tmp_path: Path) -> None:
    source = tmp_path / "reports"
    run_dir = source / "run"
    run_dir.mkdir(parents=True)
    (run_dir / "report.json").write_text("{}", encoding="utf-8")
    (run_dir / "tearsheet.html").write_text("<!doctype html>", encoding="utf-8")
    (run_dir / "leaderboard.csv").write_text("policy,lift\n", encoding="utf-8")
    (run_dir / "raw.parquet").write_bytes(b"raw")

    target = tmp_path / "site" / "reports"
    pages._copy_report_artifact_dirs(source=source, target=target)

    assert (target / "run" / "report.json").is_file()
    assert (target / "run" / "tearsheet.html").is_file()
    assert (target / "run" / "leaderboard.csv").is_file()
    assert not (target / "run" / "raw.parquet").exists()
