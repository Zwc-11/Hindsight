"""Static GitHub Pages artifact builder for Hindsight reports."""

from __future__ import annotations

import html
import json
import shutil
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from hindsight.demo import run_demo

_PUBLISHABLE_REPORT_SUFFIXES = {".csv", ".html", ".json", ".md", ".txt"}


@dataclass(frozen=True, slots=True)
class ResearchLogRow:
    """One report entry rendered into the static research log."""

    label: str
    source: str
    date: str
    date_sort: int
    symbol: str
    policies: str
    top_policy: str
    lift_bps: float | None
    dsr: float | None
    pbo: float | None
    verdict: str
    report_hash: str
    link: Path
    spark_values: tuple[float, ...]


@dataclass(frozen=True, slots=True)
class PagesSite:
    """Paths produced for the static Pages artifact."""

    root: Path
    index_path: Path
    demo_tearsheet: Path
    benchmark_tearsheets: tuple[Path, ...]
    research_log_rows: tuple[ResearchLogRow, ...]


def build_pages_site(*, repo_root: Path, output_dir: Path) -> PagesSite:
    """Build the static report site that GitHub Pages deploys."""

    repo_root = repo_root.resolve()
    output_dir = output_dir.resolve()
    docs_dir = repo_root / "docs"
    benchmarks_dir = docs_dir / "benchmarks"
    reports_dir = repo_root / "reports"
    protected_dirs = {docs_dir, benchmarks_dir, reports_dir}
    if output_dir == repo_root or output_dir in protected_dirs or any(
        source_dir in output_dir.parents for source_dir in protected_dirs
    ):
        raise ValueError("Pages output_dir must not overwrite source artifacts")

    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True)
    (output_dir / ".nojekyll").write_text("", encoding="utf-8")

    demo_dir = output_dir / "demo"
    demo = run_demo(
        sample_root=repo_root / "examples" / "sample_data" / "hyperliquid",
        output_dir=demo_dir,
        repo_root=repo_root,
    )

    published_benchmarks = output_dir / "benchmarks"
    if benchmarks_dir.is_dir():
        shutil.copytree(benchmarks_dir, published_benchmarks)
    else:
        published_benchmarks.mkdir()

    published_reports = output_dir / "reports"
    _copy_report_artifact_dirs(source=reports_dir, target=published_reports)

    benchmark_tearsheets = tuple(
        sorted(
            path
            for path in published_benchmarks.rglob("tearsheet.html")
            if path.is_file()
        )
    )
    research_log_rows = _research_log_rows(
        site_root=output_dir,
        roots=(
            (demo_dir, "demo"),
            (published_reports, "reports"),
            (published_benchmarks, "benchmarks"),
        ),
    )
    index_path = output_dir / "index.html"
    index_path.write_text(
        _index_html(
            demo_tearsheet=demo.tearsheet_path.relative_to(output_dir),
            benchmark_tearsheets=tuple(
                path.relative_to(output_dir) for path in benchmark_tearsheets
            ),
            research_log_rows=research_log_rows,
        ),
        encoding="utf-8",
    )

    return PagesSite(
        root=output_dir,
        index_path=index_path,
        demo_tearsheet=demo.tearsheet_path,
        benchmark_tearsheets=benchmark_tearsheets,
        research_log_rows=research_log_rows,
    )


def _copy_report_artifact_dirs(*, source: Path, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    if not source.is_dir():
        return
    for report_path in sorted(source.rglob("report.json")):
        artifact_dir = report_path.parent
        if not (artifact_dir / "tearsheet.html").is_file():
            continue
        relative_dir = artifact_dir.relative_to(source)
        destination = target / relative_dir
        if destination.exists():
            shutil.rmtree(destination)
        destination.mkdir(parents=True)
        for artifact_file in sorted(artifact_dir.iterdir()):
            if (
                artifact_file.is_file()
                and artifact_file.suffix.lower() in _PUBLISHABLE_REPORT_SUFFIXES
            ):
                shutil.copy2(artifact_file, destination / artifact_file.name)


def _research_log_rows(
    *,
    site_root: Path,
    roots: Iterable[tuple[Path, str]],
) -> tuple[ResearchLogRow, ...]:
    rows: list[ResearchLogRow] = []
    seen: set[Path] = set()
    for root, source in roots:
        if not root.is_dir():
            continue
        for report_path in sorted(root.rglob("report.json")):
            tearsheet_path = report_path.parent / "tearsheet.html"
            if report_path in seen or not tearsheet_path.is_file():
                continue
            seen.add(report_path)
            payload = _load_report_payload(report_path)
            if payload is None:
                continue
            rows.append(
                _research_log_row(
                    payload=payload,
                    report_path=report_path,
                    tearsheet_path=tearsheet_path,
                    site_root=site_root,
                    source=source,
                )
            )
    return tuple(
        sorted(
            rows,
            key=lambda row: (row.date_sort, row.source, row.label),
            reverse=True,
        )
    )


def _load_report_payload(path: Path) -> dict[str, Any] | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    payload = cast(dict[str, Any], raw)
    meta = _mapping(payload.get("meta"))
    if meta.get("report_schema") != 2:
        return None
    return payload


def _research_log_row(
    *,
    payload: dict[str, Any],
    report_path: Path,
    tearsheet_path: Path,
    site_root: Path,
    source: str,
) -> ResearchLogRow:
    sample = _mapping(payload.get("sample_data"))
    comparison = _mapping(payload.get("comparison"))
    leaderboards = _mapping(payload.get("leaderboards"))
    audited = _mapping_rows(leaderboards.get("audited"))
    top = min(audited, key=lambda row: _int_or_max(row.get("rank"))) if audited else {}
    dsr_pbo = _mapping(payload.get("dsr_pbo"))
    policies = _mapping_rows(comparison.get("policies"))
    audited_count = sum(1 for policy in policies if policy.get("audited_status") == "audited")
    blocked_count = sum(1 for policy in policies if policy.get("audited_status") == "blocked")
    if not policies:
        audited_count = len(audited)
    date = _text(sample.get("date"), report_path.parent.name)
    top_policy = _text(top.get("policy_name"), "n/a")

    return ResearchLogRow(
        label=_report_label(source=source, artifact_dir=report_path.parent),
        source=source,
        date=date,
        date_sort=_date_sort_key(date),
        symbol=_text(sample.get("symbol"), "n/a"),
        policies=_policy_count_text(audited=audited_count, blocked=blocked_count),
        top_policy=top_policy,
        lift_bps=_float_or_none(top.get("avg_lift_bps")),
        dsr=_float_or_none(top.get("deflated_sharpe_probability")),
        pbo=_float_or_none(dsr_pbo.get("audited_pbo")),
        verdict=_text(comparison.get("verdict"), "unknown"),
        report_hash=_hash_text(_mapping(payload.get("provenance")).get("report_self_hash")),
        link=tearsheet_path.relative_to(site_root),
        spark_values=_spark_values(payload, top_policy=top_policy),
    )


def _index_html(
    *,
    demo_tearsheet: Path,
    benchmark_tearsheets: tuple[Path, ...],
    research_log_rows: tuple[ResearchLogRow, ...],
) -> str:
    row_count = len(research_log_rows)
    benchmark_count = len(benchmark_tearsheets)
    summary = (
        f"{row_count} report{'s' if row_count != 1 else ''}; "
        f"{benchmark_count} benchmark tearsheet{'s' if benchmark_count != 1 else ''}."
    )
    return "\n".join(
        [
            "<!doctype html>",
            '<html lang="en">',
            "<head>",
            '<meta charset="utf-8">',
            '<meta name="viewport" content="width=device-width, initial-scale=1">',
            "<title>Hindsight Reports</title>",
            "<style>",
            _INDEX_CSS,
            "</style>",
            "</head>",
            "<body>",
            '<main class="wrap">',
            "<header>",
            '<div class="eyebrow">Hindsight research log</div>',
            "<h1>Report Ledger</h1>",
            "<p>Static, hash-stamped reports generated from committed artifacts.</p>",
            f'<p class="summary">{html.escape(summary)}</p>',
            "</header>",
            '<section class="panel log-panel">',
            "<h2>Runs</h2>",
            (
                "<p>Rows are read from <code>report.json</code> under "
                "<code>demo/</code>, <code>reports/</code>, and "
                "<code>benchmarks/</code>.</p>"
            ),
            '<div class="table-wrap">',
            '<table id="report-log">',
            "<thead>",
            "<tr>",
            _sort_header("Date", 0, "num"),
            _sort_header("Symbol", 1, "text"),
            _sort_header("Source", 2, "text"),
            _sort_header("Policies", 3, "num"),
            _sort_header("Top audited policy", 4, "text"),
            _sort_header("Lift bps", 5, "num"),
            _sort_header("DSR", 6, "num"),
            _sort_header("PBO", 7, "num"),
            _sort_header("Fold lift", 8, "text"),
            _sort_header("Verdict", 9, "text"),
            "<th>Report</th>",
            "</tr>",
            "</thead>",
            "<tbody>",
            _research_log_table(research_log_rows),
            "</tbody>",
            "</table>",
            "</div>",
            "</section>",
            '<p class="foot">',
            f'Demo artifact: <a href="{html.escape(demo_tearsheet.as_posix(), quote=True)}">',
            "tearsheet.html</a>. Raw market data is not copied into this archive.",
            "</p>",
            "</main>",
            "<script>",
            _INDEX_JS,
            "</script>",
            "</body>",
            "</html>",
            "",
        ]
    )


def _sort_header(label: str, column: int, kind: str) -> str:
    label_html = html.escape(label)
    return (
        "<th>"
        f'<button type="button" data-sort-column="{column}" data-sort-kind="{kind}">'
        f"{label_html}"
        "</button>"
        "</th>"
    )


def _research_log_table(rows: tuple[ResearchLogRow, ...]) -> str:
    if not rows:
        return '<tr><td colspan="11">No schema-v2 report rows were found.</td></tr>'
    return "\n".join(_research_log_tr(row) for row in rows)


def _research_log_tr(row: ResearchLogRow) -> str:
    link = html.escape(row.link.as_posix(), quote=True)
    hash_label = row.report_hash or "open"
    status_class = f"verdict verdict-{_css_token(row.verdict)}"
    return "\n".join(
        [
            '<tr data-row="report">',
            _td(row.date, sort=str(row.date_sort)),
            _td(row.symbol),
            _td(row.source),
            _td(row.policies, sort=str(_policy_sort_value(row.policies))),
            _td(row.top_policy),
            _td(_format_bps(row.lift_bps), sort=_number_sort_value(row.lift_bps), css="num"),
            _td(_format_probability(row.dsr), sort=_number_sort_value(row.dsr), css="num"),
            _td(_format_probability(row.pbo), sort=_number_sort_value(row.pbo), css="num"),
            _td(_sparkline(row.spark_values), sort=row.top_policy, raw=True),
            _td(row.verdict.upper(), css=status_class),
            (
                '<td class="report-link">'
                f'<a href="{link}">{html.escape(row.label)}</a>'
                f'<span>{html.escape(hash_label)}</span>'
                "</td>"
            ),
            "</tr>",
        ]
    )


def _td(text: str, *, sort: str | None = None, css: str | None = None, raw: bool = False) -> str:
    class_attr = f' class="{html.escape(css, quote=True)}"' if css else ""
    sort_attr = f' data-sort="{html.escape(sort, quote=True)}"' if sort is not None else ""
    cell = text if raw else html.escape(text)
    return f"<td{class_attr}{sort_attr}>{cell}</td>"


def _mapping(value: object) -> dict[str, Any]:
    if isinstance(value, dict):
        return cast(dict[str, Any], value)
    return {}


def _mapping_rows(value: object) -> tuple[dict[str, Any], ...]:
    if not isinstance(value, list):
        return ()
    return tuple(cast(dict[str, Any], item) for item in value if isinstance(item, dict))


def _text(value: object, fallback: str) -> str:
    if isinstance(value, str) and value:
        return value
    return fallback


def _float_or_none(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    return None


def _int_or_max(value: object) -> int:
    if isinstance(value, bool):
        return 999_999
    if isinstance(value, int):
        return value
    return 999_999


def _date_sort_key(date: str) -> int:
    digits = "".join(char for char in date if char.isdigit())
    if len(digits) >= 8:
        return int(digits[:8])
    return 0


def _policy_count_text(*, audited: int, blocked: int) -> str:
    if blocked:
        return f"{audited} audited / {blocked} blocked"
    return f"{audited} audited"


def _policy_sort_value(text: str) -> int:
    prefix = text.split(" ", maxsplit=1)[0]
    try:
        return int(prefix)
    except ValueError:
        return 0


def _report_label(*, source: str, artifact_dir: Path) -> str:
    if source == "demo":
        return "synthetic demo"
    return artifact_dir.name


def _hash_text(value: object) -> str:
    if isinstance(value, str) and value:
        return value[:12]
    return ""


def _spark_values(payload: dict[str, Any], *, top_policy: str) -> tuple[float, ...]:
    dsr_pbo = _mapping(payload.get("dsr_pbo"))
    for policy in _mapping_rows(dsr_pbo.get("policies")):
        if policy.get("policy_name") != top_policy:
            continue
        values = tuple(
            value
            for value in (_float_or_none(item) for item in _list(policy.get("lift_bps")))
            if value is not None
        )
        if values:
            return values
    return _equity_spark_values(payload, top_policy=top_policy)


def _equity_spark_values(payload: dict[str, Any], *, top_policy: str) -> tuple[float, ...]:
    for curve in _mapping_rows(payload.get("equity_curves")):
        if curve.get("policy_name") != top_policy:
            continue
        values = tuple(
            value
            for value in (
                _float_or_none(_mapping(point).get("cum_markout_bps"))
                for point in _list(curve.get("points"))
            )
            if value is not None
        )
        return _sample_values(values, max_points=18)
    return ()


def _list(value: object) -> tuple[object, ...]:
    if isinstance(value, list):
        return tuple(value)
    return ()


def _sample_values(values: tuple[float, ...], *, max_points: int) -> tuple[float, ...]:
    if len(values) <= max_points:
        return values
    last_index = len(values) - 1
    indexes = {
        round(index * last_index / (max_points - 1))
        for index in range(max_points)
    }
    return tuple(values[index] for index in sorted(indexes))


def _sparkline(values: tuple[float, ...]) -> str:
    if not values:
        return '<span class="muted">n/a</span>'
    width = 112
    height = 28
    pad = 2
    low = min(values)
    high = max(values)
    span = high - low
    if span == 0:
        span = 1.0
    count = max(len(values) - 1, 1)
    points = []
    for index, value in enumerate(values):
        x = pad + (width - (pad * 2)) * index / count
        y = height - pad - ((value - low) / span) * (height - (pad * 2))
        points.append(f"{x:.1f},{y:.1f}")
    zero_line = ""
    if low < 0 < high:
        zero_y = height - pad - ((0 - low) / span) * (height - (pad * 2))
        zero_line = f'<line class="zero" x1="0" y1="{zero_y:.1f}" x2="{width}" y2="{zero_y:.1f}"/>'
    return (
        '<svg class="spark" viewBox="0 0 112 28" role="img" '
        'aria-label="fold lift sparkline">'
        f"{zero_line}"
        f'<polyline points="{" ".join(points)}"/>'
        "</svg>"
    )


def _format_bps(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:+.3f}"


def _format_probability(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:.3f}"


def _number_sort_value(value: float | None) -> str:
    if value is None:
        return "-Infinity"
    return f"{value:.12f}"


def _css_token(value: str) -> str:
    token = "".join(char.lower() if char.isalnum() else "-" for char in value)
    return token.strip("-") or "unknown"


_INDEX_CSS = """
:root {
  --bg: #ffffff;
  --paper: #ffffff;
  --border: #c8c8c8;
  --text: #1b1b1b;
  --muted: #646464;
  --link: #0645ad;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  background: var(--bg);
  color: var(--text);
  font-family: Arial, "Helvetica Neue", Helvetica, sans-serif;
  font-size: 14px;
  line-height: 1.45;
}
.wrap {
  max-width: 920px;
  margin: 0 auto;
  padding: 36px 22px 64px;
}
header {
  border-bottom: 2px solid var(--text);
  margin-bottom: 24px;
  padding-bottom: 18px;
}
.eyebrow {
  color: var(--muted);
  font-family: "SFMono-Regular", Consolas, "Liberation Mono", Menlo, monospace;
  font-size: 11px;
  letter-spacing: .05em;
  text-transform: uppercase;
}
h1 {
  font-size: 32px;
  line-height: 1.1;
  margin: 8px 0 6px;
}
p {
  color: var(--muted);
  margin: 0;
}
code {
  font-family: "SFMono-Regular", Consolas, "Liberation Mono", Menlo, monospace;
  font-size: 12px;
}
.summary {
  margin-top: 7px;
}
.panel {
  background: var(--paper);
  border-bottom: 1px solid var(--border);
  border-top: 2px solid var(--text);
  margin-bottom: 22px;
  padding: 12px 0 18px;
}
h2 {
  font-size: 15px;
  margin: 0 0 10px;
}
.table-wrap {
  margin-top: 14px;
  overflow-x: auto;
}
table {
  border-collapse: collapse;
  font-variant-numeric: tabular-nums;
  min-width: 980px;
  width: 100%;
}
th,
td {
  border-top: 1px solid var(--border);
  padding: 8px 7px;
  text-align: left;
  vertical-align: middle;
}
th {
  border-top-color: var(--text);
  color: var(--muted);
  font-size: 11px;
  text-transform: uppercase;
}
th button {
  appearance: none;
  background: none;
  border: 0;
  color: inherit;
  cursor: pointer;
  font: inherit;
  padding: 0;
  text-align: left;
  text-transform: inherit;
}
th button:hover { color: var(--text); }
.num { text-align: right; }
a {
  color: var(--link);
  font-weight: 700;
  text-decoration: none;
}
a:hover { text-decoration: underline; }
.spark {
  display: block;
  height: 28px;
  width: 112px;
}
.spark polyline {
  fill: none;
  stroke: var(--text);
  stroke-linecap: round;
  stroke-linejoin: round;
  stroke-width: 1.8;
}
.spark .zero {
  stroke: var(--border);
  stroke-width: 1;
}
.muted,
.report-link span {
  color: var(--muted);
}
.report-link span {
  display: block;
  font-family: "SFMono-Regular", Consolas, "Liberation Mono", Menlo, monospace;
  font-size: 11px;
  margin-top: 2px;
}
.verdict {
  font-weight: 700;
  letter-spacing: .02em;
}
.verdict-blocked { color: #8a1f11; }
.verdict-pass,
.verdict-passed { color: #175c2f; }
.foot {
  border-top: 1px solid var(--border);
  padding-top: 12px;
}
@media (max-width: 640px) {
  .wrap { padding: 24px 14px 44px; }
  table { min-width: 900px; }
}
""".strip()


_INDEX_JS = """
(function () {
  var table = document.getElementById("report-log");
  if (!table) return;
  var tbody = table.tBodies[0];
  function cellValue(row, column) {
    var cell = row.cells[column];
    return cell ? (cell.getAttribute("data-sort") || cell.textContent || "") : "";
  }
  function compare(kind, column, direction) {
    return function (left, right) {
      var leftValue = cellValue(left, column);
      var rightValue = cellValue(right, column);
      var result;
      if (kind === "num") {
        result = Number(leftValue) - Number(rightValue);
      } else {
        result = leftValue.localeCompare(rightValue);
      }
      return direction === "asc" ? result : -result;
    };
  }
  table.querySelectorAll("button[data-sort-column]").forEach(function (button) {
    button.addEventListener("click", function () {
      var column = Number(button.getAttribute("data-sort-column"));
      var kind = button.getAttribute("data-sort-kind") || "text";
      var direction = button.getAttribute("data-direction") === "desc" ? "asc" : "desc";
      table.querySelectorAll("button[data-sort-column]").forEach(function (item) {
        item.removeAttribute("data-direction");
      });
      button.setAttribute("data-direction", direction);
      Array.prototype.slice.call(tbody.querySelectorAll("tr[data-row]"))
        .sort(compare(kind, column, direction))
        .forEach(function (row) { tbody.appendChild(row); });
    });
  });
}());
""".strip()
