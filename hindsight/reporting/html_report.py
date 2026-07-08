"""Self-contained HTML tearsheet renderer.

Renders a report-v2 payload (see :mod:`hindsight.reporting.report_v2`) into a
single offline HTML file: inline CSS, hand-rolled SVG charts, and a small amount
of vanilla JS for crosshair tooltips, panel collapse, and click-to-copy.

Guarantees:

* **Deterministic.** Output is a pure function of the report dict. No generation
  timestamps, no randomness; every SVG coordinate is rounded to two decimals.
  Two renders of the same report are byte-identical.
* **Offline.** No CDN, no web fonts, no external requests. One file.
* **Degrades without JS.** All data lives in static SVG and text; JS only adds
  hover tooltips, collapsing, and copy affordances.
"""

from __future__ import annotations

from html import escape
from pathlib import Path
from typing import Any

# Fixed chart geometry (viewBox units; charts scale to container width).
_CW = 860
_MARGIN_L = 52
_MARGIN_R = 18
_MARGIN_T = 16
_MARGIN_B = 30


def render_tearsheet(report: dict[str, Any]) -> str:
    """Render a report-v2 dict to a complete, self-contained HTML document."""

    meta = report["meta"]
    body = "\n".join(
        [
            _verdict_banner(report),
            _leaderboards_panel(report["leaderboards"]),
            _equity_panel(report["equity_curves"]),
            _ladder_panel(report["markout_ladder"]),
            _heatmap_panel(report["fold_heatmap"]),
            _cost_panel(report["cost_attribution"]),
            _capacity_panel(report["capacity"]),
            _sensitivity_panel(report["sensitivity"]),
            _regimes_panel(report["regimes"]),
            _probes_panel(report["probes"]),
            _falsification_panel(report["falsification"]),
            _dsr_pbo_panel(report["dsr_pbo"]),
            _fills_panel(report["fills"]),
            _provenance_panel(report["provenance"]),
            _reproduce_panel(report["reproduce"]),
        ]
    )
    title = _esc(meta["title"])
    masthead = _masthead(report)
    return (
        "<!doctype html>\n"
        '<html lang="en">\n<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{title}</title>\n"
        f"<style>\n{_CSS}\n</style>\n"
        "</head>\n<body>\n"
        '<div class="wrap">\n'
        f"{masthead}\n"
        f"{body}\n"
        '<div class="tooltip" id="tt"></div>\n'
        "</div>\n"
        f"<script>\n{_JS}\n</script>\n"
        "</body>\n</html>\n"
    )


def write_html_report(path: Path, report: dict[str, Any]) -> None:
    """Render and write the tearsheet HTML to ``path``."""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_tearsheet(report), encoding="utf-8")


# --------------------------------------------------------------------------- #
# Panels                                                                       #
# --------------------------------------------------------------------------- #


def _masthead(report: dict[str, Any]) -> str:
    meta = report["meta"]
    sample = report["sample_data"]
    provenance = report["provenance"]
    run_short = str(provenance["run_id"])[:12]
    facts = (
        ("symbol", sample["symbol"]),
        ("date", sample["date"]),
        ("horizon", sample["horizon"]),
        ("rows", sample["rows"]),
        ("data", sample["label"]),
        ("schema", f'v{meta["report_schema"]}'),
        ("run", run_short),
    )
    fact_cells = "".join(
        f'<div class="meta-cell"><span>{_esc(key)}</span><b>{_esc(value)}</b></div>'
        for key, value in facts
    )
    return (
        '<header class="masthead">\n'
        '  <div class="mast-copy">\n'
        f'    <div class="eyebrow">{_esc(meta["generator"])}</div>\n'
        f'    <h1>{_esc(meta["title"])}</h1>\n'
        f'    <p>{_esc(meta["subtitle"])}</p>\n'
        "  </div>\n"
        f'  <div class="run-meta">{fact_cells}</div>\n'
        "</header>"
    )


def _verdict_banner(report: dict[str, Any]) -> str:
    comparison = report["comparison"]
    reason = _esc(comparison["block_reason"] or "")
    naive = _esc(comparison["headline_naive"])
    audited = _esc(comparison["headline_audited"])
    audited_state = _css_token(comparison["verdict"])
    return (
        '<section class="verdict">\n'
        '  <div class="v-naive">\n'
        '    <div class="v-tag">NAIVE SPLIT</div>\n'
        f'    <div class="v-line">{naive}</div>\n'
        '    <div class="v-sub">leaky policy ranks #1; selection invalid</div>\n'
        "  </div>\n"
        '  <div class="v-arrow">AUDIT</div>\n'
        f'  <div class="v-audited {audited_state}">\n'
        '    <div class="v-tag">HINDSIGHT AUDIT</div>\n'
        f'    <div class="v-line">{audited}</div>\n'
        f'    <div class="v-sub">block reason: <code>{reason}</code></div>\n'
        "  </div>\n"
        "</section>\n"
    )


def _panel(title: str, subtitle: str, inner: str, *, tag: str = "") -> str:
    chip = f'<span class="p-chip">{_esc(tag)}</span>' if tag else ""
    sub = f'<div class="p-sub">{_esc(subtitle)}</div>' if subtitle else ""
    return (
        '<section class="panel">\n'
        '  <div class="panel-h" role="button" tabindex="0" aria-expanded="true">\n'
        f'    <h2>{_esc(title)}</h2>{chip}\n'
        '    <span class="p-toggle">hide</span>\n'
        "  </div>\n"
        f'  <div class="panel-b">\n{sub}\n{inner}\n  </div>\n'
        "</section>\n"
    )


def _leaderboards_panel(lb: dict[str, Any]) -> str:
    shift_by_name = {row["policy_name"]: row for row in lb["rank_shifts"]}
    naive_tbl = _leaderboard_table(lb["naive"], "Naive control", shift_by_name, "naive_rank")
    audited_tbl = _leaderboard_table(
        lb["audited"], "Hindsight audit", shift_by_name, "audited_rank"
    )
    inner = f'<div class="cols2">\n{naive_tbl}\n{audited_tbl}\n</div>'
    return _panel(
        "Dual leaderboards",
        "Same policies, two evaluation regimes. Arrows show the rank shift once the "
        "audit removes the leak.",
        inner,
        tag="rank-shift",
    )


def _leaderboard_table(
    rows: list[dict[str, Any]],
    heading: str,
    shift_by_name: dict[str, Any],
    rank_key: str,
) -> str:
    body: list[str] = []
    for row in rows:
        name = row["policy_name"]
        shift = shift_by_name.get(name, {})
        arrow = _rank_arrow(shift.get("delta"))
        leaky = " leaky" if name == "leaky" else ""
        body.append(
            f'<tr class="lb-row{leaky}">'
            f'<td class="rk">#{row["rank"]}{arrow}</td>'
            f'<td class="pol">{_esc(name)}</td>'
            f'<td class="num">{_fmt_bps(row["avg_markout_bps"])}</td>'
            f'<td class="num">{_fmt_bps(row["avg_lift_bps"])}</td>'
            f'<td class="num">{_fmt_pct(row["quote_rate"])}</td>'
            "</tr>"
        )
    return (
        '<div class="lb">\n'
        f"  <h3>{_esc(heading)}</h3>\n"
        '  <table class="grid">\n'
        "    <thead><tr><th>Rank</th><th>Policy</th><th>Markout</th>"
        "<th>Lift</th><th>Quote%</th></tr></thead>\n"
        f'    <tbody>{"".join(body)}</tbody>\n'
        "  </table>\n"
        "</div>"
    )


def _rank_arrow(delta: Any) -> str:
    if delta is None:
        return '<span class="arw mut">&middot;</span>'
    if delta > 0:
        return f'<span class="arw up">&uarr;{delta}</span>'
    if delta < 0:
        return f'<span class="arw dn">&darr;{abs(delta)}</span>'
    return '<span class="arw flat">0</span>'


def _css_token(value: Any) -> str:
    token = "".join(char.lower() if char.isalnum() else "-" for char in str(value))
    return token.strip("-") or "unknown"


def _equity_panel(curves: list[dict[str, Any]]) -> str:
    colors = _series_colors([c["policy_name"] for c in curves])
    cum_series = [
        {
            "name": c["policy_name"],
            "color": colors[c["policy_name"]],
            "points": [(p["index"], p["cum_markout_bps"]) for p in c["points"]],
            "tips": [
                f'{c["policy_name"]} @ t{p["index"]}: cum {_fmt_bps(p["cum_markout_bps"])} bps'
                + ("" if p["quoted"] else " (skip)")
                for p in c["points"]
            ],
        }
        for c in curves
    ]
    dd_series = [
        {
            "name": c["policy_name"],
            "color": colors[c["policy_name"]],
            "points": [(p["index"], -p["drawdown_bps"]) for p in c["points"]],
            "tips": [
                f'{c["policy_name"]} @ t{p["index"]}: drawdown {_fmt_bps(p["drawdown_bps"])} bps'
                for p in c["points"]
            ],
        }
        for c in curves
    ]
    n = max((len(c["points"]) for c in curves), default=0)
    legend = _legend(colors)
    cum_chart = _line_chart(cum_series, n, height=250, y_suffix=" bps")
    dd_chart = _line_chart(dd_series, n, height=150, y_suffix=" bps")
    inner = (
        f"{legend}\n"
        '<div class="chart-label">Cumulative captured markout (bps); skips '
        "contribute 0</div>\n"
        f"{cum_chart}\n"
        '<div class="chart-label">Drawdown from running peak (bps)</div>\n'
        f"{dd_chart}"
    )
    return _panel(
        "Equity & drawdown",
        "Per-policy cumulative markout over the committed opportunities, with the "
        "drawdown envelope below.",
        inner,
        tag="per-policy",
    )


def _ladder_panel(ladder: dict[str, Any]) -> str:
    horizons = ladder["horizons"]
    colors = _series_colors([p["policy_name"] for p in ladder["policies"]])
    rows: list[str] = []
    for policy in ladder["policies"]:
        cells = [f'<td class="pol">{_esc(policy["policy_name"])}</td>']
        for rung in policy["rungs"]:
            cells.append(
                '<td class="num">'
                f'{_fmt_bps(rung["avg_markout_bps"])}'
                f'<span class="ci">[{_fmt_bps(rung["ci_lower_bps"])}, '
                f'{_fmt_bps(rung["ci_upper_bps"])}]</span>'
                "</td>"
            )
        rows.append(f'<tr>{"".join(cells)}</tr>')
    head = "".join(f"<th>{_esc(h)}</th>" for h in horizons)
    table = (
        '<table class="grid ladder">\n'
        f"  <thead><tr><th>Policy</th>{head}</tr></thead>\n"
        f'  <tbody>{"".join(rows)}</tbody>\n'
        "</table>"
    )
    inner = f"{_legend(colors)}\n{table}"
    return _panel(
        "Markout ladder",
        ladder["note"],
        inner,
        tag=f'{len(horizons)} horizon' + ("s" if len(horizons) != 1 else ""),
    )


def _heatmap_panel(heat: dict[str, Any]) -> str:
    return _panel(
        "Fold / policy heatmap",
        "Markout lift (bps) of each policy on each purged walk-forward test fold. "
        "Cell detail includes the confidence interval.",
        _heatmap_svg(heat),
        tag=f'{len(heat["folds"])}x{len(heat["policies"])}',
    )


def _cost_panel(cost: dict[str, Any]) -> str:
    charts = "".join(_waterfall_svg(policy) for policy in cost["policies"])
    model = cost["cost_model"]
    model_line = (
        f'maker {_fmt_bps(model["maker_fee_bps"])} &middot; '
        f'taker {_fmt_bps(model["taker_fee_bps"])} &middot; '
        f'slippage {_fmt_bps(model["slippage_impact_bps"])} &middot; '
        f'funding@h {_fmt_bps(model["funding_bps_at_horizon"])}'
    )
    inner = (
        f'<div class="cost-model">cost model: {model_line}</div>\n'
        f'<div class="cols2">{charts}</div>'
    )
    return _panel("Cost attribution", cost["note"], inner, tag="gross/net")


def _capacity_panel(cap: dict[str, Any]) -> str:
    series = [
        {
            "name": cap["baseline_policy"],
            "color": "var(--cyan)",
            "points": [(i, p["realized_lift_bps"]) for i, p in enumerate(cap["points"])],
            "tips": [
                f'cap {_fmt_pct(p["participation_cap"])}: '
                f'lift {_fmt_bps(p["realized_lift_bps"])} bps '
                f'(fill {_fmt_pct(p["fill_fraction"])})'
                for p in cap["points"]
            ],
        }
    ]
    x_labels = [_fmt_pct(p["participation_cap"]) for p in cap["points"]]
    chart = _line_chart(series, len(cap["points"]), height=220, x_labels=x_labels, y_suffix=" bps")
    inner = (
        f'<div class="chart-label">Realized lift vs participation cap; '
        f'baseline <code>{_esc(cap["baseline_policy"])}</code></div>\n{chart}'
    )
    return _panel("Capacity curve", cap["note"], inner, tag="participation")


def _sensitivity_panel(sens: dict[str, Any]) -> str:
    return _panel(
        "Sensitivity surface",
        sens["note"],
        _unavailable("Overfit-surface heatmap"),
        tag="n/a on synthetic",
    )


def _regimes_panel(reg: dict[str, Any]) -> str:
    return _panel(
        "Regime panel",
        reg["note"],
        _unavailable("Vol-tercile by time-of-day small multiples"),
        tag="n/a on synthetic",
    )


def _falsification_panel(fals: dict[str, Any]) -> str:
    if not fals["available"]:
        return _panel(
            "Falsification suite",
            "Adversarial self-test: inject known leaks and prove each tripwire fires.",
            _unavailable("Falsification matrix"),
            tag="self-test",
        )
    rows: list[str] = []
    for case in fals["cases"]:
        caught = case["caught"]
        cls = "ok" if caught else "bad"
        badge = "CAUGHT" if caught else "ESCAPED"
        evidence = ", ".join(case["evidence"]) or "-"
        detail = case.get("detail") or case["statistic"]
        evidence_count = case.get("evidence_count", len(case["evidence"]))
        rows.append(
            f'<tr><td class="pol">{_esc(case["defect"])}</td>'
            f'<td>{_esc(case["probe"])}</td>'
            f'<td class="st {cls}">{badge}</td>'
            f'<td class="mono">{_esc(evidence)}'
            f'<span class="ci">{_esc(evidence_count)} finding(s)</span></td>'
            f'<td class="msg">{_esc(detail)}'
            f'<span class="ci">{_esc(case["statistic"])}</span></td></tr>'
        )
    vcls = "ok" if fals["all_caught"] else "bad"
    headline = f'{fals["caught"]}/{fals["defects"]} injected defects caught'
    table = (
        '<table class="grid probes">\n'
        "  <thead><tr><th>Injected defect</th><th>Tripwire</th><th>Result</th>"
        "<th>Evidence</th><th>Injected defect detail</th></tr></thead>\n"
        f'  <tbody>{"".join(rows)}</tbody>\n'
        "</table>"
    )
    inner = f'<div class="verdict-chip {vcls}">{_esc(headline)}</div>\n{table}'
    return _panel(
        "Falsification suite",
        "Five injected defects. The expected tripwire must fire for each one.",
        inner,
        tag="self-test",
    )


def _probes_panel(probes: dict[str, Any]) -> str:
    rows: list[str] = []
    for item in probes["items"]:
        status = item["status"]
        cls = {"caught": "ok", "pass": "ok", "fail": "bad"}.get(status, "mut")
        badge = {"caught": "CAUGHT", "pass": "PASS", "fail": "FAIL"}.get(status, status.upper())
        features = ", ".join(item["offending_features"]) or "-"
        rows.append(
            f'<tr><td class="pol">{_esc(item["policy_name"])}</td>'
            f'<td>{_esc(item["probe"])}</td>'
            f'<td class="st {cls}">{badge}</td>'
            f'<td class="mono">{_esc(features)}</td>'
            f'<td class="msg">{_esc(item["message"])}</td></tr>'
        )
    verdict = probes["verdict"]
    vcls = "bad" if verdict == "fail" else "ok"
    table = (
        '<table class="grid probes">\n'
        "  <thead><tr><th>Policy</th><th>Probe</th><th>Status</th>"
        "<th>Offending feature</th><th>Detail</th></tr></thead>\n"
        f'  <tbody>{"".join(rows)}</tbody>\n'
        "</table>"
    )
    inner = (
        f'<div class="verdict-chip {vcls}">audit verdict: {_esc(verdict).upper()}</div>\n{table}'
    )
    return _panel(
        "Leakage tripwires",
        "Each probe, whether it fired, and the exact feature it caught.",
        inner,
        tag="audit",
    )


def _dsr_pbo_panel(dp: dict[str, Any]) -> str:
    naive = _fmt_metric(dp["naive_pbo"])
    audited = _fmt_metric(dp["audited_pbo"])
    gauges = (
        '<div class="gauges">\n'
        f'  <div class="gauge"><div class="g-k">PBO (naive)</div>'
        f'<div class="g-v mut">{_esc(naive)}</div></div>\n'
        f'  <div class="gauge"><div class="g-k">PBO (audited)</div>'
        f'<div class="g-v mut">{_esc(audited)}</div></div>\n'
        "</div>"
    )
    strips = "".join(_dist_strip(policy) for policy in dp["policies"])
    inner = f"{gauges}\n<div class=\"dists\">{strips}</div>"
    return _panel(
        "Deflated Sharpe & PBO",
        "Probability of backtest overfit and per-policy deflated Sharpe. Gauges read "
        "n/a until a flagship run supplies at least 8 folds and at least 3 trials.",
        inner,
        tag="overfit",
    )


def _fills_panel(fills: dict[str, Any]) -> str:
    total = fills["total_opportunities"]
    maker = fills["maker_side_positive"]
    items = [
        ("opportunities", str(total)),
        ("buy / sell", f'{fills["buy_count"]} / {fills["sell_count"]}'),
        ("maker-side +/-", f'{maker} / {fills["maker_side_negative"]}'),
        ("top-of-book missing", str(fills["top_of_book_missing"])),
        ("latency", f'{fills["latency_ms"]} ms'),
        (
            "maker / taker fee",
            f'{_fmt_bps(fills["maker_fee_bps"])} / {_fmt_bps(fills["taker_fee_bps"])} bps',
        ),
        ("liquidity assumption", _esc(fills["liquidity_assumption"])),
    ]
    cells = "".join(
        f'<div class="kv"><div class="k">{_esc(k)}</div><div class="v">{v}</div></div>'
        for k, v in items
    )
    note = f'<div class="p-note">{_esc(fills["fee_tier_note"])}</div>'
    return _panel(
        "Fill-model disclosure",
        "Simulator assumptions used in this report.",
        f'<div class="kvs">{cells}</div>\n{note}',
        tag="disclosure",
    )


def _provenance_panel(prov: dict[str, Any]) -> str:
    fields = [
        ("run_id", prov["run_id"]),
        ("git_sha", prov["git_sha"]),
        ("data_content_hash", prov["data_content_hash"]),
        ("config_hash", prov["config_hash"]),
        ("report_self_hash", prov["report_self_hash"]),
        ("seed", str(prov["seed"])),
        ("engine_version", prov["engine_version"]),
    ]
    chips = "".join(
        f'<div class="prov-row"><span class="prov-k">{_esc(k)}</span>'
        f'<code class="chip copy" data-copy="{_esc(str(v))}" title="click to copy">'
        f"{_esc(str(v))}</code></div>"
        for k, v in fields
    )
    return _panel(
        "Provenance",
        "Artifact identifiers and content hashes for this run.",
        f'<div class="prov">{chips}</div>',
        tag="hash-stamped",
    )


def _reproduce_panel(repro: dict[str, Any]) -> str:
    pinned = " &middot; ".join(f"{_esc(k)} {_esc(v)}" for k, v in repro["pinned"].items())
    cmd = _esc(repro["command"])
    inner = (
        '<div class="repro-cmd"><code class="chip copy block" '
        f'data-copy="{cmd}" title="click to copy">{cmd}</code></div>\n'
        f'<div class="pinned">pinned: {pinned}</div>\n'
        f'<div class="p-note">{_esc(repro["note"])}</div>'
    )
    return _panel("Reproduce this report", "", inner, tag="clone & run")


# --------------------------------------------------------------------------- #
# SVG chart primitives                                                         #
# --------------------------------------------------------------------------- #


def _line_chart(
    series: list[dict[str, Any]],
    n_points: int,
    *,
    height: int,
    x_labels: list[str] | None = None,
    y_suffix: str = "",
) -> str:
    if n_points <= 0:
        return _unavailable("chart")
    values = [pt[1] for s in series for pt in s["points"]]
    values.append(0.0)
    y_min, y_max = _bounds(values)
    plot_w = _CW - _MARGIN_L - _MARGIN_R
    plot_h = height - _MARGIN_T - _MARGIN_B

    def sx(i: int) -> float:
        if n_points == 1:
            return _MARGIN_L + plot_w / 2
        return _MARGIN_L + (i / (n_points - 1)) * plot_w

    def sy(v: float) -> float:
        return _MARGIN_T + (y_max - v) / (y_max - y_min) * plot_h

    parts: list[str] = [f'<svg class="chart" viewBox="0 0 {_CW} {height}" '
                        'preserveAspectRatio="xMidYMid meet" role="img">']
    # y gridlines + labels
    for frac in (0.0, 0.25, 0.5, 0.75, 1.0):
        val = y_max - frac * (y_max - y_min)
        y = _MARGIN_T + frac * plot_h
        parts.append(
            f'<line class="grid" x1="{_n(_MARGIN_L)}" y1="{_n(y)}" '
            f'x2="{_n(_CW - _MARGIN_R)}" y2="{_n(y)}"/>'
        )
        parts.append(
            f'<text class="axl" x="{_n(_MARGIN_L - 6)}" y="{_n(y + 3)}" '
            f'text-anchor="end">{_esc(_fmt_num(val) + y_suffix)}</text>'
        )
    # zero line emphasis
    if y_min < 0 < y_max:
        yz = sy(0.0)
        parts.append(
            f'<line class="zero" x1="{_n(_MARGIN_L)}" y1="{_n(yz)}" '
            f'x2="{_n(_CW - _MARGIN_R)}" y2="{_n(yz)}"/>'
        )
    # x labels
    labels = x_labels if x_labels is not None else [str(i) for i in range(n_points)]
    step = max(1, (n_points + 7) // 8)
    for i in range(0, n_points, step):
        if i < len(labels):
            parts.append(
                f'<text class="axl" x="{_n(sx(i))}" y="{_n(height - 10)}" '
                f'text-anchor="middle">{_esc(labels[i])}</text>'
            )
    # series
    for s in series:
        pts = s["points"]
        poly = " ".join(f"{_n(sx(x))},{_n(sy(v))}" for x, v in pts)
        parts.append(
            f'<g class="series" data-policy="{_esc(s["name"])}">'
            f'<polyline class="ln" fill="none" stroke="{s["color"]}" points="{poly}"/>'
        )
        tips = s.get("tips", [])
        for idx, (x, v) in enumerate(pts):
            tip = tips[idx] if idx < len(tips) else ""
            parts.append(
                f'<circle class="pt" cx="{_n(sx(x))}" cy="{_n(sy(v))}" r="3" '
                f'fill="{s["color"]}" data-tip="{_esc(tip)}"/>'
            )
        parts.append("</g>")
    parts.append("</svg>")
    return "".join(parts)


def _heatmap_svg(heat: dict[str, Any]) -> str:
    folds = heat["folds"]
    policies = heat["policies"]
    if not folds or not policies:
        return _unavailable("heatmap")
    cell_by = {(c["fold_id"], c["policy_name"]): c for c in heat["cells"]}
    scale = max(abs(heat["min_lift_bps"]), abs(heat["max_lift_bps"]), 1e-9)
    left = 92
    top = 26
    cw = min(150, (_CW - left - 12) // max(1, len(policies)))
    ch = 46
    width = left + cw * len(policies) + 12
    height = top + ch * len(folds) + 10
    parts = [f'<svg class="chart heat" viewBox="0 0 {width} {height}" '
             'preserveAspectRatio="xMidYMid meet" role="img">']
    for col, policy in enumerate(policies):
        x = left + col * cw + cw / 2
        parts.append(
            f'<text class="axl" x="{_n(x)}" y="16" text-anchor="middle">{_esc(policy)}</text>'
        )
    for rowi, fold in enumerate(folds):
        y = top + rowi * ch
        parts.append(
            f'<text class="axl" x="{_n(left - 8)}" y="{_n(y + ch / 2 + 3)}" '
            f'text-anchor="end">fold {fold}</text>'
        )
        for col, policy in enumerate(policies):
            cell = cell_by.get((fold, policy))
            x = left + col * cw
            lift = 0.0 if cell is None else cell["markout_lift_bps"]
            fill = _diverging(lift, scale)
            tip = (
                f"fold {fold} / {policy}: lift {_fmt_bps(lift)} bps "
                f'[{_fmt_bps(cell["ci_lower_bps"])}, {_fmt_bps(cell["ci_upper_bps"])}]'
                if cell
                else "no data"
            )
            parts.append(
                f'<rect class="cell" x="{_n(x + 2)}" y="{_n(y + 2)}" '
                f'width="{_n(cw - 4)}" height="{_n(ch - 4)}" rx="3" fill="{fill}" '
                f'data-tip="{_esc(tip)}"/>'
            )
            parts.append(
                f'<text class="cellv" x="{_n(x + cw / 2)}" y="{_n(y + ch / 2 + 4)}" '
                f'text-anchor="middle">{_esc(_fmt_bps(lift))}</text>'
            )
    parts.append("</svg>")
    return "".join(parts)


def _waterfall_svg(policy: dict[str, Any]) -> str:
    gross = policy["gross_bps"]
    net = policy["net_bps"]
    steps: list[tuple[str, float, float]] = [("gross", gross, gross)]
    for stage in policy["stages"]:
        steps.append((stage["label"], stage["delta_bps"], stage["cumulative_bps"]))
    steps.append(("net", net, net))
    width = 420
    height = 200
    ml, mr, mt, mb = 40, 12, 14, 26
    plot_h = height - mt - mb
    values = [0.0, gross, net] + [s["cumulative_bps"] for s in policy["stages"]]
    y_min, y_max = _bounds(values)

    def sy(v: float) -> float:
        return mt + (y_max - v) / (y_max - y_min) * plot_h

    n = len(steps)
    bw = (width - ml - mr) / n * 0.62
    gap = (width - ml - mr) / n
    parts = [f'<svg class="chart wf" viewBox="0 0 {width} {height}" '
             'preserveAspectRatio="xMidYMid meet" role="img">']
    parts.append(f'<text class="wf-t" x="{ml}" y="11">{_esc(policy["policy_name"])}</text>')
    if y_min < 0 < y_max:
        yz = sy(0.0)
        parts.append(
            f'<line class="zero" x1="{ml}" y1="{_n(yz)}" x2="{width - mr}" y2="{_n(yz)}"/>'
        )
    prev_cum = 0.0
    for i, (label, delta, cum) in enumerate(steps):
        cx = ml + gap * i + (gap - bw) / 2
        if label in ("gross", "net"):
            top = sy(max(0.0, cum))
            bot = sy(min(0.0, cum))
            cls = "wf-tot"
        else:
            top = sy(max(prev_cum, cum))
            bot = sy(min(prev_cum, cum))
            cls = "wf-neg" if delta < 0 else "wf-pos"
        h = max(1.0, bot - top)
        tip = f"{label}: {_fmt_bps(delta)} bps (cum {_fmt_bps(cum)})"
        parts.append(
            f'<rect class="{cls}" x="{_n(cx)}" y="{_n(top)}" width="{_n(bw)}" '
            f'height="{_n(h)}" data-tip="{_esc(tip)}"/>'
        )
        parts.append(
            f'<text class="wf-l" x="{_n(cx + bw / 2)}" y="{height - 14}" '
            f'text-anchor="middle">{_esc(label)}</text>'
        )
        parts.append(
            f'<text class="wf-v" x="{_n(cx + bw / 2)}" y="{height - 3}" '
            f'text-anchor="middle">{_esc(_fmt_bps(cum))}</text>'
        )
        prev_cum = cum
    parts.append("</svg>")
    return f'<div class="wf-wrap">{"".join(parts)}</div>'


def _dist_strip(policy: dict[str, Any]) -> str:
    values = policy["lift_bps"]
    name = policy["policy_name"]
    dsr = _fmt_metric(policy["deflated_sharpe_probability"])
    width = _CW
    height = 58
    ml, mr = 150, 24
    y = height / 2
    if values:
        lo, hi = _bounds(list(values))
    else:
        lo, hi = -1.0, 1.0
    parts = [f'<svg class="chart strip" viewBox="0 0 {width} {height}" '
             'preserveAspectRatio="xMidYMid meet" role="img">']
    parts.append(
        f'<text class="axl" x="10" y="{_n(y - 4)}" text-anchor="start">{_esc(name)}</text>'
    )
    parts.append(
        f'<text class="axl mut" x="10" y="{_n(y + 13)}" text-anchor="start">'
        f"DSR {_esc(dsr)}</text>"
    )
    parts.append(f'<line class="grid" x1="{ml}" y1="{_n(y)}" x2="{width - mr}" y2="{_n(y)}"/>')

    def sx(v: float) -> float:
        if hi == lo:
            return (ml + width - mr) / 2
        return ml + (v - lo) / (hi - lo) * (width - mr - ml)

    if lo < 0 < hi:
        parts.append(
            f'<line class="zero" x1="{_n(sx(0.0))}" y1="{_n(y - 14)}" '
            f'x2="{_n(sx(0.0))}" y2="{_n(y + 14)}"/>'
        )
    for i, v in enumerate(values):
        parts.append(
            f'<circle class="pt" cx="{_n(sx(v))}" cy="{_n(y)}" r="5" fill="var(--cyan)" '
            f'data-tip="{_esc(name)} fold {i}: lift {_fmt_bps(v)} bps"/>'
        )
    parts.append("</svg>")
    return "".join(parts)


def _legend(colors: dict[str, str]) -> str:
    chips = "".join(
        f'<span class="lg"><span class="sw" style="background:{color}"></span>'
        f"{_esc(name)}</span>"
        for name, color in colors.items()
    )
    return f'<div class="legend">{chips}</div>'


def _unavailable(what: str) -> str:
    return (
        f'<div class="unavail">'
        f'<span class="ua-badge">requires flagship data</span>'
        f"<span class=\"ua-txt\">{what} populates on a Phase&nbsp;0 run.</span>"
        "</div>"
    )


# --------------------------------------------------------------------------- #
# Formatting helpers                                                           #
# --------------------------------------------------------------------------- #


def _esc(value: Any) -> str:
    return escape(str(value), quote=True)


def _n(value: float) -> str:
    text = f"{float(value):.2f}".rstrip("0").rstrip(".")
    return "0" if text in ("-0", "") else text


def _fmt_num(value: float) -> str:
    text = f"{float(value):.2f}".rstrip("0").rstrip(".")
    return "0" if text in ("-0", "") else text


def _fmt_bps(value: Any) -> str:
    if isinstance(value, str):
        return _esc(value)
    return f"{float(value):+.2f}".rstrip("0").rstrip(".") or "0"


def _fmt_pct(value: float) -> str:
    return f"{float(value) * 100:.0f}%"


def _fmt_metric(value: Any) -> str:
    if isinstance(value, str):
        return value
    return f"{float(value):.3f}"


def _bounds(values: list[float]) -> tuple[float, float]:
    lo = min(values)
    hi = max(values)
    if hi == lo:
        return (lo - 1.0, hi + 1.0)
    pad = (hi - lo) * 0.08
    return (lo - pad, hi + pad)


def _diverging(value: float, scale: float) -> str:
    ratio = max(-1.0, min(1.0, value / scale))
    if ratio >= 0:
        alpha = 0.12 + 0.62 * ratio
        return f"rgba(31,107,70,{alpha:.3f})"
    alpha = 0.12 + 0.62 * (-ratio)
    return f"rgba(163,58,48,{alpha:.3f})"


_PALETTE = ["var(--cyan)", "var(--blue)", "var(--amber)", "var(--violet)", "var(--green)"]


def _series_colors(names: list[str]) -> dict[str, str]:
    ordered = sorted(dict.fromkeys(names))
    return {name: _PALETTE[i % len(_PALETTE)] for i, name in enumerate(ordered)}


# --------------------------------------------------------------------------- #
# Static CSS / JS                                                              #
# --------------------------------------------------------------------------- #

_CSS = """
:root{
  --bg:#f3f2ed; --paper:#ffffff; --panel:#ffffff; --panel2:#f8f7f3;
  --border:#cfcac0; --border-soft:#e5e1d8; --grid:#e8e4dc;
  --text:#1f1f1d; --mut:#6f6a61; --green:#1f6b46; --red:#a33a30;
  --amber:#7b5d18; --cyan:#2d6f73; --blue:#285f94; --violet:#62577a;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);
  font-family:Arial,"Helvetica Neue",Helvetica,sans-serif;font-size:14px;line-height:1.42}
.wrap{max-width:1120px;margin:0 auto;padding:28px 22px 64px}
code,.mono,.chip,.rk,.p-chip,.eyebrow{
  font-family:"SFMono-Regular",Consolas,"Liberation Mono",Menlo,monospace}
h1,h2,h3,p{margin:0}
h2,h3{font-weight:700}
.masthead{display:grid;grid-template-columns:minmax(280px,1fr) minmax(360px,470px);
  gap:28px;align-items:end;padding:0 0 18px;margin-bottom:16px;
  border-bottom:2px solid var(--text)}
.eyebrow{color:var(--mut);font-size:11px;letter-spacing:.04em;text-transform:uppercase}
.masthead h1{font-size:30px;line-height:1.08;font-weight:700;margin-top:8px;letter-spacing:-.01em}
.masthead p{color:var(--mut);font-size:14px;margin-top:6px}
.run-meta{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));
  border-top:1px solid var(--border);border-left:1px solid var(--border)}
.meta-cell{min-width:0;padding:7px 8px;border-right:1px solid var(--border);
  border-bottom:1px solid var(--border);background:var(--panel2)}
.meta-cell span{display:block;color:var(--mut);font-size:10px;text-transform:uppercase;
  letter-spacing:.05em}
.meta-cell b{display:block;margin-top:2px;font-size:12px;font-weight:700;
  overflow-wrap:anywhere}
.verdict{display:grid;grid-template-columns:1fr 74px 1fr;gap:16px;align-items:stretch;
  margin-bottom:18px;padding:14px 0;border-top:1px solid var(--border);
  border-bottom:1px solid var(--border)}
.v-naive,.v-audited{background:var(--paper);padding:12px 14px;border-left:3px solid var(--border)}
.v-naive,.v-audited.blocked{border-left-color:var(--red)}
.v-audited.pass,.v-audited.passed{border-left-color:var(--green)}
.v-tag{font-size:10px;letter-spacing:.08em;color:var(--mut);text-transform:uppercase}
.v-line{font-size:18px;font-weight:700;margin-top:5px}
.v-naive .v-line,.v-audited.blocked .v-line{color:var(--red)}
.v-audited.pass .v-line,.v-audited.passed .v-line{color:var(--green)}
.v-sub{color:var(--mut);font-size:12px;margin-top:5px}
.v-sub code{color:var(--amber);background:#fbf5dc;padding:1px 3px}
.v-arrow{display:flex;align-items:center;justify-content:center;color:var(--mut);
  font-size:10px;letter-spacing:.12em;border-left:1px solid var(--border);
  border-right:1px solid var(--border)}
.panel{background:var(--panel);border-top:2px solid var(--text);
  border-bottom:1px solid var(--border);margin-bottom:18px;overflow:hidden}
.panel-h{display:flex;align-items:baseline;gap:10px;padding:10px 0 9px;
  cursor:pointer;border-bottom:1px solid var(--border);user-select:none}
.panel-h h2{font-size:15px;letter-spacing:.01em}
.p-chip{font-size:10px;color:var(--mut);letter-spacing:.05em;text-transform:uppercase}
.p-toggle{margin-left:auto;color:var(--mut);font-size:11px;text-transform:uppercase}
.panel-b{padding:14px 0 16px}
.panel.collapsed .panel-b{display:none}
.p-sub{color:var(--mut);font-size:12.5px;margin-bottom:12px;max-width:86ch}
.p-note{color:var(--mut);font-size:12px;margin-top:9px;max-width:86ch}
.cols2{display:grid;grid-template-columns:1fr 1fr;gap:22px}
.chart{width:100%;height:auto;display:block;background:var(--paper);
  border:1px solid var(--border);margin:6px 0 10px}
.chart-label{color:var(--mut);font-size:12px;margin:10px 0 3px}
.grid line.grid,.chart .grid{stroke:var(--grid);stroke-width:1}
.chart .zero{stroke:#827b70;stroke-width:1;stroke-dasharray:3 3}
.chart .ln{stroke-width:2;vector-effect:non-scaling-stroke}
.chart .pt{cursor:pointer}
.chart .pt:hover{r:5}
.axl{fill:var(--mut);font-size:11px}
.axl.mut{fill:#8d867c}
.cell{stroke:#ffffff;stroke-width:1;cursor:pointer}
.cellv{fill:#151515;font-size:12px;font-weight:700}
.wf-wrap{padding:0}
.wf-t{fill:var(--text);font-size:12px;font-weight:700}
.wf-tot{fill:var(--blue)} .wf-neg{fill:var(--red)} .wf-pos{fill:var(--green)}
.wf rect{cursor:pointer}
.wf-l{fill:var(--mut);font-size:10px} .wf-v{fill:var(--text);font-size:10px}
.cost-model{color:var(--mut);font-size:12px;margin-bottom:10px}
table.grid{width:100%;border-collapse:collapse;font-size:13px;background:var(--paper)}
table.grid th{text-align:left;color:var(--mut);font-weight:700;font-size:10px;
  letter-spacing:.06em;text-transform:uppercase;padding:7px 8px;
  border-bottom:1px solid var(--text)}
table.grid td{padding:7px 8px;border-bottom:1px solid var(--border-soft)}
table.grid tr:last-child td{border-bottom:1px solid var(--border)}
table.grid td.num{text-align:right;font-variant-numeric:tabular-nums}
.lb h3{font-size:12px;color:var(--mut);margin-bottom:6px;font-weight:700;text-transform:uppercase}
.lb-row.leaky .pol{color:var(--red);font-weight:700}
.rk{white-space:nowrap;color:var(--mut);font-size:12px}
.arw{margin-left:6px;font-size:11px}
.arw.up{color:var(--green)} .arw.dn{color:var(--red)} .arw.flat{color:var(--mut)}
.arw.mut{color:#aaa39a}
.ci{display:block;color:var(--mut);font-size:10px}
.st{font-weight:700;font-size:11px}
.st.ok{color:var(--green)} .st.bad{color:var(--red)} .st.mut{color:var(--mut)}
.mono{color:var(--amber);font-size:12px}
.msg{color:var(--mut);font-size:12px}
.verdict-chip{display:inline-block;padding:4px 9px;font-size:11px;font-weight:700;
  margin-bottom:10px;border:1px solid;text-transform:uppercase}
.verdict-chip.bad{color:var(--red);border-color:var(--red);background:#fff4f2}
.verdict-chip.ok{color:var(--green);border-color:var(--green);background:#f1f8f3}
.legend{display:flex;flex-wrap:wrap;gap:13px;margin-bottom:4px}
.lg{color:var(--mut);font-size:12px;display:flex;align-items:center;gap:6px}
.sw{width:10px;height:10px;display:inline-block;border:1px solid #ffffff}
.unavail{display:flex;align-items:center;gap:10px;padding:14px;border:1px dashed var(--border);
  background:var(--panel2)}
.ua-badge{font-size:10px;letter-spacing:.06em;color:var(--amber);
  border:1px solid var(--border);padding:3px 7px;background:var(--paper);
  white-space:nowrap;text-transform:uppercase}
.ua-txt{color:var(--mut);font-size:12.5px}
.gauges{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:14px}
.gauge{border-top:1px solid var(--border);border-bottom:1px solid var(--border);
  padding:10px 0;background:var(--paper)}
.g-k{color:var(--mut);font-size:10px;letter-spacing:.06em;text-transform:uppercase}
.g-v{font-size:15px;margin-top:4px} .g-v.mut{color:var(--amber);font-size:12px}
.kvs{display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:0;
  border-top:1px solid var(--border);border-left:1px solid var(--border)}
.kv{padding:9px 10px;border-right:1px solid var(--border);
  border-bottom:1px solid var(--border);background:var(--panel2)}
.kv .k{color:var(--mut);font-size:10px;text-transform:uppercase;letter-spacing:.05em}
.kv .v{font-size:14px;margin-top:3px}
.prov{display:flex;flex-direction:column;gap:6px}
.prov-row{display:grid;grid-template-columns:170px 1fr;gap:10px;align-items:start}
.prov-k{color:var(--mut);font-size:12px}
.chip{background:var(--panel2);border:1px solid var(--border);padding:4px 7px;
  font-size:12px;color:var(--text);cursor:pointer;word-break:break-all}
.chip.copy:hover{background:#eee9df;border-color:#9f988d}
.chip.copied{color:var(--green);border-color:var(--green)}
.chip.block{display:inline-block;color:var(--text)}
.repro-cmd{margin-bottom:8px}
.pinned{color:var(--mut);font-size:12px}
.tooltip{position:fixed;pointer-events:none;z-index:50;background:#1f1f1d;
  border:1px solid #000;padding:6px 8px;font-size:12px;color:#fff;max-width:330px;
  opacity:0;transition:opacity .08s;box-shadow:0 4px 18px rgba(0,0,0,.22)}
.tooltip.on{opacity:1}
@media(max-width:760px){
  .wrap{padding:18px 14px 44px}
  .masthead{grid-template-columns:1fr;gap:14px}
  .run-meta{grid-template-columns:repeat(2,minmax(0,1fr))}
  .cols2,.gauges{grid-template-columns:1fr}
  .verdict{grid-template-columns:1fr;gap:8px}
  .v-arrow{min-height:30px;border:1px solid var(--border)}
  .prov-row{grid-template-columns:1fr}
  table.grid{display:block;max-width:100%;overflow-x:auto;
    -webkit-overflow-scrolling:touch;white-space:nowrap}
  table.grid th,table.grid td{padding:7px 9px}
  table.grid .msg{min-width:220px;white-space:normal}
}
@media print{
  :root{--bg:#fff;--panel:#fff;--panel2:#fff;--text:#111;--mut:#555;--border:#bbb;--grid:#eee}
  body{font-size:11px}.wrap{max-width:100%;padding:0}
  .panel-h{cursor:default}.p-toggle,.tooltip{display:none}
  .panel,.verdict,.masthead{break-inside:avoid}
  .panel.collapsed .panel-b{display:block}
}
"""

_JS = """
(function(){
  "use strict";
  var tt = document.getElementById("tt");

  // Crosshair tooltips over any element carrying data-tip.
  function showTip(e, text){
    if(!text) return;
    tt.innerHTML = text;
    tt.classList.add("on");
    moveTip(e);
  }
  function moveTip(e){
    var x = e.clientX + 14, y = e.clientY + 14;
    var w = tt.offsetWidth, h = tt.offsetHeight;
    if(x + w > window.innerWidth) x = e.clientX - w - 14;
    if(y + h > window.innerHeight) y = e.clientY - h - 14;
    tt.style.left = x + "px";
    tt.style.top = y + "px";
  }
  function hideTip(){ tt.classList.remove("on"); }

  document.addEventListener("mouseover", function(e){
    var el = e.target.closest("[data-tip]");
    if(el) showTip(e, el.getAttribute("data-tip"));
  });
  document.addEventListener("mousemove", function(e){
    if(tt.classList.contains("on")) moveTip(e);
  });
  document.addEventListener("mouseout", function(e){
    if(e.target.closest("[data-tip]")) hideTip();
  });

  // Panel collapse.
  document.querySelectorAll(".panel-h").forEach(function(h){
    function toggle(){
      var panel = h.parentElement;
      panel.classList.toggle("collapsed");
      var expanded = !panel.classList.contains("collapsed");
      h.setAttribute("aria-expanded", expanded ? "true" : "false");
      var t = h.querySelector(".p-toggle");
      if(t) t.textContent = expanded ? "hide" : "show";
    }
    h.addEventListener("click", toggle);
    h.addEventListener("keydown", function(e){
      if(e.key === "Enter" || e.key === " "){
        e.preventDefault();
        toggle();
      }
    });
  });

  // Click-to-copy chips.
  function copyText(text){
    if(navigator.clipboard && navigator.clipboard.writeText){
      return navigator.clipboard.writeText(text);
    }
    return new Promise(function(resolve){
      var ta = document.createElement("textarea");
      ta.value = text; ta.style.position = "fixed"; ta.style.opacity = "0";
      document.body.appendChild(ta); ta.select();
      try{ document.execCommand("copy"); }catch(err){}
      document.body.removeChild(ta); resolve();
    });
  }
  document.querySelectorAll(".chip.copy").forEach(function(chip){
    chip.addEventListener("click", function(){
      var text = chip.getAttribute("data-copy") || chip.textContent;
      copyText(text).then(function(){
        var prev = chip.textContent;
        chip.classList.add("copied");
        chip.textContent = "copied \\u2713";
        setTimeout(function(){
          chip.classList.remove("copied");
          chip.textContent = prev;
        }, 900);
      });
    });
  });

  // Legend toggles series visibility (affordance only; all visible by default).
  document.querySelectorAll(".panel").forEach(function(panel){
    var legend = panel.querySelector(".legend");
    if(!legend) return;
    legend.querySelectorAll(".lg").forEach(function(item){
      item.style.cursor = "pointer";
      item.addEventListener("click", function(){
        var name = item.textContent.trim();
        var off = item.classList.toggle("off");
        item.style.opacity = off ? "0.4" : "1";
        panel.querySelectorAll('.series[data-policy="' + name + '"]').forEach(function(g){
          g.style.display = off ? "none" : "";
        });
      });
    });
  });
})();
"""
