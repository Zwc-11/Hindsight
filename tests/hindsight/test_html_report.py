from __future__ import annotations

import json
from pathlib import Path

from hindsight.demo.runner import run_demo
from hindsight.reporting import html_report
from hindsight.reporting.html_report import render_tearsheet, write_html_report


def _demo_report(tmp_path: Path) -> dict:
    run_demo(
        sample_root=Path("examples/sample_data/hyperliquid"),
        output_dir=tmp_path,
        repo_root=Path.cwd(),
    )
    return json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# Integration: the tearsheet is a valid, offline, deterministic single file.    #
# --------------------------------------------------------------------------- #


def test_tearsheet_is_well_formed_and_offline(tmp_path: Path) -> None:
    html = render_tearsheet(_demo_report(tmp_path))
    assert html.startswith("<!doctype html>")
    assert html.rstrip().endswith("</html>")
    # No external requests: no CDN, no web fonts, nothing over the network.
    assert "http://" not in html
    assert "https://" not in html
    assert "//cdn" not in html
    # Static SVG is present and balanced (renders with JS disabled).
    assert html.count("<svg") == html.count("</svg>") >= 6


def test_tearsheet_headlines_the_verdict_and_provenance(tmp_path: Path) -> None:
    report = _demo_report(tmp_path)
    html = render_tearsheet(report)
    assert "BLOCKED" in html
    assert 'class="v-audited blocked"' in html
    assert ".v-audited.blocked .v-line{color:var(--red)}" in html
    assert "leaky ranks #1" in html
    assert "Reproduce this report" in html
    assert "Leakage tripwires" in html
    # Every provenance hash is embedded and copyable.
    for key in ("run_id", "git_sha", "data_content_hash", "report_self_hash"):
        assert report["provenance"][key] in html
    # The exact reproduce command is present.
    assert report["reproduce"]["command"] in html


def test_panels_are_keyboard_operable_controls(tmp_path: Path) -> None:
    html = render_tearsheet(_demo_report(tmp_path))
    assert 'class="panel-h" role="button" tabindex="0" aria-expanded="true"' in html
    assert 'h.setAttribute("aria-expanded", expanded ? "true" : "false")' in html


def test_tearsheet_is_byte_deterministic(tmp_path: Path) -> None:
    report = _demo_report(tmp_path)
    assert render_tearsheet(report) == render_tearsheet(report)


def test_tearsheet_file_is_byte_deterministic_across_fresh_runs(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    run_demo(
        sample_root=Path("examples/sample_data/hyperliquid"),
        output_dir=first,
        repo_root=Path.cwd(),
    )
    run_demo(
        sample_root=Path("examples/sample_data/hyperliquid"),
        output_dir=second,
        repo_root=Path.cwd(),
    )

    assert (first / "tearsheet.html").read_bytes() == (
        second / "tearsheet.html"
    ).read_bytes()


def test_tearsheet_is_a_reasonable_single_file(tmp_path: Path) -> None:
    html = render_tearsheet(_demo_report(tmp_path))
    assert len(html.encode("utf-8")) < 2_000_000  # < 2 MB


def test_write_html_report_writes_file(tmp_path: Path) -> None:
    report = _demo_report(tmp_path)
    out = tmp_path / "nested" / "tearsheet.html"
    write_html_report(out, report)
    assert out.read_text(encoding="utf-8") == render_tearsheet(report)


def test_falsification_panel_is_rendered(tmp_path: Path) -> None:
    html = render_tearsheet(_demo_report(tmp_path))
    assert "Falsification suite" in html
    assert "injected defects caught" in html
    assert "label_lookahead" in html
    assert "Injected defect detail" in html
    assert "Predictor reads the realized label" in html
    assert "CAUGHT" in html


def test_unavailable_blocks_render_honest_placeholder(tmp_path: Path) -> None:
    html = render_tearsheet(_demo_report(tmp_path))
    assert "requires flagship data" in html
    assert "Sensitivity surface" in html
    assert "Regime panel" in html


# --------------------------------------------------------------------------- #
# Unit coverage of rendering helpers and branch edges                           #
# --------------------------------------------------------------------------- #


def test_falsification_panel_unavailable_state() -> None:
    html = html_report._falsification_panel(
        {
            "available": False,
            "all_caught": False,
            "defects": 0,
            "caught": 0,
            "run_hash": "",
            "cases": [],
        }
    )
    assert "requires flagship data" in html
    assert "Falsification suite" in html


def test_rank_arrow_branches() -> None:
    assert "up" in html_report._rank_arrow(2)
    assert "dn" in html_report._rank_arrow(-3)
    assert "flat" in html_report._rank_arrow(0)
    assert "mut" in html_report._rank_arrow(None)


def test_number_formatting() -> None:
    assert html_report._n(0.0) == "0"
    assert html_report._n(-0.0) == "0"
    assert html_report._n(12.5) == "12.5"
    assert html_report._n(12.0) == "12"
    assert html_report._fmt_bps(1.0) == "+1"
    assert html_report._fmt_bps(-2.5) == "-2.5"
    assert html_report._fmt_bps("n/a (requires >=3 trials)") == "n/a (requires &gt;=3 trials)"
    assert html_report._fmt_pct(0.5) == "50%"
    assert html_report._fmt_metric(0.123456) == "0.123"
    assert html_report._fmt_metric("n/a") == "n/a"


def test_bounds_and_diverging() -> None:
    lo, hi = html_report._bounds([1.0, 1.0])
    assert lo < 1.0 < hi
    lo, hi = html_report._bounds([1.0, 3.0])
    assert lo < 1.0 and hi > 3.0
    assert "31,107,70" in html_report._diverging(1.0, 1.0)  # green for positive
    assert "163,58,48" in html_report._diverging(-1.0, 1.0)  # red for negative


def test_series_colors_are_stable_and_ordered() -> None:
    colors = html_report._series_colors(["b", "a", "a"])
    assert set(colors) == {"a", "b"}
    assert colors == html_report._series_colors(["a", "b"])


def test_line_chart_empty_and_single_point() -> None:
    assert "requires flagship data" in html_report._line_chart([], 0, height=100)
    single = html_report._line_chart(
        [{"name": "s", "color": "var(--cyan)", "points": [(0, 1.0)], "tips": ["t"]}],
        1,
        height=120,
    )
    assert "<svg" in single and "<circle" in single


def test_heatmap_empty_and_missing_cell() -> None:
    assert "requires flagship data" in html_report._heatmap_svg(
        {"folds": [], "policies": [], "cells": [], "min_lift_bps": 0.0, "max_lift_bps": 0.0}
    )
    svg = html_report._heatmap_svg(
        {
            "folds": [0],
            "policies": ["p"],
            "cells": [],  # cell for (0, "p") is missing
            "min_lift_bps": 0.0,
            "max_lift_bps": 0.0,
        }
    )
    assert "no data" in svg


def test_waterfall_renders_positive_stage() -> None:
    svg = html_report._waterfall_svg(
        {
            "policy_name": "rebate",
            "gross_bps": 2.0,
            "stages": [
                {"label": "rebate", "delta_bps": 1.0, "cumulative_bps": 3.0},
                {"label": "fees", "delta_bps": -0.5, "cumulative_bps": 2.5},
            ],
            "net_bps": 2.5,
        }
    )
    assert "wf-pos" in svg  # positive delta branch
    assert "wf-neg" in svg
    assert "wf-tot" in svg


def test_dist_strip_handles_empty_values() -> None:
    svg = html_report._dist_strip(
        {"policy_name": "p", "deflated_sharpe_probability": "n/a", "lift_bps": []}
    )
    assert "<svg" in svg


def test_escaping_prevents_injection() -> None:
    assert html_report._esc("<script>&") == "&lt;script&gt;&amp;"
