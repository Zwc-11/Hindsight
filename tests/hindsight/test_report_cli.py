from __future__ import annotations

from pathlib import Path

import pytest

from hindsight.cli import main, run_report


def _make_demo_run(tmp_path: Path) -> Path:
    run_dir = tmp_path / "run"
    assert main(["demo", "--output-dir", str(run_dir)]) == 0
    assert (run_dir / "report.json").is_file()
    return run_dir


def test_report_cli_renders_tearsheet_from_saved_report(tmp_path: Path) -> None:
    run_dir = _make_demo_run(tmp_path)
    (run_dir / "tearsheet.html").unlink()  # ensure the command recreates it
    assert main(["report", str(run_dir)]) == 0
    tearsheet = (run_dir / "tearsheet.html").read_text(encoding="utf-8")
    assert tearsheet.startswith("<!doctype html>")
    assert "BLOCKED" in tearsheet


def test_report_cli_custom_output_path(tmp_path: Path) -> None:
    run_dir = _make_demo_run(tmp_path)
    out = tmp_path / "custom" / "sheet.html"
    tearsheet_path = run_report(run_dir=run_dir, output=out)
    assert tearsheet_path == out
    assert out.is_file()


def test_report_cli_missing_report_errors(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="No report.json"):
        run_report(run_dir=tmp_path, output=None)
