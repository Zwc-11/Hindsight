"""Generate local CatBoost validation artifacts for the promoted SOL model."""

from __future__ import annotations

import csv
import hashlib
import json
import subprocess
import sys
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

from hindsight.models.catboost_adapter import CatBoostMarkoutAdapter  # noqa: E402

OUTPUT_DIR = Path(__file__).resolve().parent
MARKETIMMUNE_ROOT = Path("C:/MarketImmune")
LAKE_ROOT = MARKETIMMUNE_ROOT / "data/hyperliquid"
MODEL_PATH = (
    MARKETIMMUNE_ROOT
    / "data/models/hyperliquid_catboost_SOL_20260527_20260531_holdout_20260601_10s.cbm"
)
CALIBRATOR_PATH = (
    MARKETIMMUNE_ROOT
    / "data/models/hyperliquid_catboost_SOL_20260527_20260531_holdout_20260601_10s.isotonic.json"
)
REFERENCE_REPORT_PATH = (
    MARKETIMMUNE_ROOT
    / "docs/benchmarks/hyperliquid_markout_SOL_20260527_20260531_holdout_20260601.json"
)
COIN = "SOL"
HORIZON = "10s"
TRAIN_DATES = ("20260527", "20260528", "20260529", "20260530", "20260531")
HOLDOUT_DATE = "20260601"
PANEL_DATES = (*TRAIN_DATES, HOLDOUT_DATE)
TRAIN_ROWS = 724_150
FLOAT_RECONCILIATION_TOLERANCE = 1e-12
BATCH_SIZE = 50_000


def main() -> int:
    _require_paths(MODEL_PATH, CALIBRATOR_PATH, REFERENCE_REPORT_PATH)
    partition_paths = {date: _training_path(date) for date in PANEL_DATES}
    _require_paths(*partition_paths.values())

    adapter = CatBoostMarkoutAdapter.load(
        model_path=MODEL_PATH,
        calibrator_path=CALIBRATOR_PATH,
    )
    if adapter.calibrator.decision_threshold is None:
        raise ValueError("calibrator is missing deployment_decision_threshold")

    reference_report = json.loads(REFERENCE_REPORT_PATH.read_text(encoding="utf-8"))
    holdout = _score_dataset(
        adapter,
        partitions={HOLDOUT_DATE: partition_paths[HOLDOUT_DATE]},
        model_name="hindsight_catboost_SOL_20260601_10s_holdout",
    )
    panel = _score_dataset(
        adapter,
        partitions={date: partition_paths[date] for date in PANEL_DATES},
        model_name="hindsight_catboost_SOL_20260527_20260601_10s_panel_replay",
    )
    reconciliation = _reconcile_holdout(holdout["metrics"], reference_report["holdout_split"])

    payload = {
        "schema_version": "1.0.0",
        "data_label": "[local real Hyperliquid lake]",
        "model_label": "[real-model]",
        "command": (
            "python "
            "docs/benchmarks/2026-07-02-local-hyperliquid-sol-catboost-"
            "20260527-20260601/generate.py"
        ),
        "hindsight_git_sha": _git_sha(),
        "coin": COIN,
        "horizon": HORIZON,
        "decision_threshold": adapter.calibrator.decision_threshold,
        "train_dates": list(TRAIN_DATES),
        "holdout_date": HOLDOUT_DATE,
        "panel_dates": list(PANEL_DATES),
        "sources": {
            "model": _source_payload(MODEL_PATH),
            "calibrator": _source_payload(CALIBRATOR_PATH),
            "marketimmune_reference_report": _source_payload(REFERENCE_REPORT_PATH),
            "training_partitions": [
                {"date": date, **_source_payload(path)}
                for date, path in partition_paths.items()
            ],
        },
        "holdout": holdout,
        "panel_replay": panel,
        "marketimmune_holdout_reference": _reference_metrics(reference_report["holdout_split"]),
        "reconciliation": reconciliation,
        "caveats": [
            "Raw MarketImmune market data and model files are local machine inputs, not committed.",
            "Panel replay scores the promoted final model over all requested dates; it is not an "
            "out-of-fold training metric.",
        ],
    }

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    json_path = OUTPUT_DIR / "validation.json"
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    _write_partition_metrics(OUTPUT_DIR / "partition-metrics.csv", panel["partitions"])
    (OUTPUT_DIR / "README.md").write_text(_markdown(payload), encoding="utf-8")
    print(f"Wrote {json_path}")
    print(f"Wrote {OUTPUT_DIR / 'partition-metrics.csv'}")
    print(f"Wrote {OUTPUT_DIR / 'README.md'}")
    print(f"Holdout reconciliation verdict: {reconciliation['verdict']}")
    return 0


def _score_dataset(
    adapter: CatBoostMarkoutAdapter,
    *,
    partitions: dict[str, Path],
    model_name: str,
) -> dict[str, Any]:
    partition_payloads: list[dict[str, Any]] = []
    all_timestamps: list[float] = []
    all_labels: list[bool] = []
    all_probabilities: list[float] = []
    all_raw_probabilities: list[float] = []
    all_markouts: list[float] = []
    all_quotes: list[bool] = []
    for date, path in partitions.items():
        rows = _load_rows(path)
        scores = _score_rows(adapter, rows)
        metrics = _metrics(
            timestamps=scores["timestamps"],
            labels=scores["labels"],
            probabilities=scores["probabilities"],
            markouts=scores["markouts"],
            quotes=scores["quotes"],
        )
        partition_payloads.append({
            "date": date,
            "path": str(path),
            "sha256": _sha256(path),
            "metrics": metrics,
        })
        all_timestamps.extend(scores["timestamps"])
        all_labels.extend(scores["labels"])
        all_probabilities.extend(scores["probabilities"])
        all_raw_probabilities.extend(scores["raw_probabilities"])
        all_markouts.extend(scores["markouts"])
        all_quotes.extend(scores["quotes"])

    return {
        "model_name": model_name,
        "metrics": _metrics(
            timestamps=all_timestamps,
            labels=all_labels,
            probabilities=all_probabilities,
            markouts=all_markouts,
            quotes=all_quotes,
        ),
        "partitions": partition_payloads,
        "raw_probability_summary": _summary(all_raw_probabilities),
        "probability_summary": _summary(all_probabilities),
    }


def _load_rows(path: Path) -> list[dict[str, Any]]:
    columns = [
        "ts_ms",
        "px",
        "sz",
        "maker_side",
        f"markout_bps_{HORIZON}",
        f"toxic_{HORIZON}",
        "l2_mid",
        "l2_spread_bps",
        "l2_microprice",
        "l2_top_imbalance",
        "l2_ofi_event",
        "l2_ofi_1s",
        "l2_ofi_5s",
        "l2_ofi_10s",
        "asset_basis_bps",
        "asset_funding",
        "asset_open_interest",
        "asset_premium",
    ]
    table = pq.read_table(path, columns=columns)  # type: ignore[no-untyped-call]
    rows = [dict(row) for row in table.to_pylist()]
    return [row for row in rows if all(row.get(column) is not None for column in columns)]


def _score_rows(adapter: CatBoostMarkoutAdapter, rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    timestamps = [float(row["ts_ms"]) for row in rows]
    labels = [bool(row[f"toxic_{HORIZON}"]) for row in rows]
    markouts = [float(row[f"markout_bps_{HORIZON}"]) for row in rows]
    probabilities: list[float] = []
    raw_probabilities: list[float] = []
    quotes: list[bool] = []
    for start in range(0, len(rows), BATCH_SIZE):
        batch = rows[start : start + BATCH_SIZE]
        for prediction in adapter.predict_rows(batch):
            if prediction.quote is None:
                raise ValueError("CatBoost prediction is missing quote decision")
            raw_probabilities.append(prediction.raw_probability)
            probabilities.append(prediction.probability)
            quotes.append(prediction.quote)
    return {
        "timestamps": timestamps,
        "labels": labels,
        "markouts": markouts,
        "raw_probabilities": raw_probabilities,
        "probabilities": probabilities,
        "quotes": quotes,
    }


def _metrics(
    *,
    timestamps: Sequence[float],
    labels: Sequence[bool],
    probabilities: Sequence[float],
    markouts: Sequence[float],
    quotes: Sequence[bool],
) -> dict[str, float | int]:
    if not timestamps:
        raise ValueError("cannot evaluate an empty dataset")
    baseline = _mean(markouts)
    policy_values = [
        markout if quote else 0.0
        for markout, quote in zip(markouts, quotes, strict=True)
    ]
    policy = _mean(policy_values)
    return {
        "n_rows": len(timestamps),
        "test_start_ms": min(timestamps),
        "test_end_ms": max(timestamps),
        "pr_auc": _average_precision(labels, probabilities),
        "brier": _brier(labels, probabilities),
        "ece": _expected_calibration_error(labels, probabilities, bins=10),
        "baseline_markout_bps": baseline,
        "policy_markout_bps": policy,
        "markout_lift_bps": policy - baseline,
        "quote_rate": sum(1 for quote in quotes if quote) / len(quotes),
    }


def _average_precision(labels: Sequence[bool], probabilities: Sequence[float]) -> float:
    positives = sum(1 for label in labels if label)
    if positives == 0:
        return float("nan")
    pairs = sorted(
        zip(probabilities, labels, strict=True),
        key=lambda item: item[0],
        reverse=True,
    )
    true_positives = 0
    false_positives = 0
    previous_recall = 0.0
    area = 0.0
    index = 0
    while index < len(pairs):
        score = pairs[index][0]
        group_true = 0
        group_false = 0
        while index < len(pairs) and pairs[index][0] == score:
            if pairs[index][1]:
                group_true += 1
            else:
                group_false += 1
            index += 1
        true_positives += group_true
        false_positives += group_false
        recall = true_positives / positives
        precision = true_positives / (true_positives + false_positives)
        area += (recall - previous_recall) * precision
        previous_recall = recall
    return area


def _brier(labels: Sequence[bool], probabilities: Sequence[float]) -> float:
    return _mean([
        (float(label) - probability) ** 2
        for label, probability in zip(labels, probabilities, strict=True)
    ])


def _expected_calibration_error(
    labels: Sequence[bool],
    probabilities: Sequence[float],
    *,
    bins: int,
) -> float:
    grouped: list[list[tuple[bool, float]]] = [[] for _ in range(bins)]
    for label, probability in zip(labels, probabilities, strict=True):
        index = min(bins - 1, int(probability * bins))
        grouped[index].append((label, probability))
    total = len(labels)
    ece = 0.0
    for values in grouped:
        if not values:
            continue
        mean_prediction = _mean([probability for _label, probability in values])
        observed_rate = _mean([float(label) for label, _probability in values])
        ece += len(values) / total * abs(observed_rate - mean_prediction)
    return ece


def _reconcile_holdout(
    hindsight_metrics: dict[str, float | int],
    reference: dict[str, Any],
) -> dict[str, Any]:
    metric_names = ("pr_auc", "brier", "ece", "markout_lift_bps", "quote_rate")
    deltas = {
        name: float(hindsight_metrics[name]) - float(reference[name])
        for name in metric_names
    }
    row_delta = int(hindsight_metrics["n_rows"]) - int(reference["n_rows"])
    max_abs_delta = max(abs(value) for value in deltas.values())
    verdict = (
        "pass"
        if row_delta == 0 and max_abs_delta <= FLOAT_RECONCILIATION_TOLERANCE
        else "fail"
    )
    return {
        "verdict": verdict,
        "float_tolerance": FLOAT_RECONCILIATION_TOLERANCE,
        "row_delta": row_delta,
        "deltas": deltas,
        "max_abs_delta": max_abs_delta,
    }


def _reference_metrics(reference: dict[str, Any]) -> dict[str, float | int]:
    return {
        "n_rows": int(reference["n_rows"]),
        "pr_auc": float(reference["pr_auc"]),
        "brier": float(reference["brier"]),
        "ece": float(reference["ece"]),
        "markout_lift_bps": float(reference["markout_lift_bps"]),
        "quote_rate": float(reference["quote_rate"]),
    }


def _write_partition_metrics(path: Path, partitions: Sequence[dict[str, Any]]) -> None:
    columns = (
        "date",
        "n_rows",
        "pr_auc",
        "brier",
        "ece",
        "markout_lift_bps",
        "quote_rate",
        "sha256",
    )
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for partition in partitions:
            metrics = partition["metrics"]
            writer.writerow({
                "date": partition["date"],
                "n_rows": metrics["n_rows"],
                "pr_auc": metrics["pr_auc"],
                "brier": metrics["brier"],
                "ece": metrics["ece"],
                "markout_lift_bps": metrics["markout_lift_bps"],
                "quote_rate": metrics["quote_rate"],
                "sha256": partition["sha256"],
            })


def _markdown(payload: dict[str, Any]) -> str:
    holdout = payload["holdout"]["metrics"]
    panel = payload["panel_replay"]["metrics"]
    reconciliation = payload["reconciliation"]
    return "\n".join([
        "# Local Hyperliquid SOL CatBoost Validation",
        "",
        f"Data label: {payload['data_label']}",
        f"Model label: {payload['model_label']}",
        "",
        "This artifact was produced on the local machine from MarketImmune's "
        "promoted SOL CatBoost markout model and local Hyperliquid Gold training "
        "parquet. Raw market data and model files are not committed.",
        "",
        "## Command",
        "",
        "```powershell",
        payload["command"],
        "```",
        "",
        "## Holdout 20260601",
        "",
        f"- Rows: `{holdout['n_rows']}`",
        f"- PR-AUC: `{holdout['pr_auc']}`",
        f"- Brier: `{holdout['brier']}`",
        f"- ECE: `{holdout['ece']}`",
        f"- Markout lift bps: `{holdout['markout_lift_bps']}`",
        f"- Quote rate: `{holdout['quote_rate']}`",
        "",
        "## Panel Replay 20260527..20260601",
        "",
        f"- Rows: `{panel['n_rows']}`",
        f"- PR-AUC: `{panel['pr_auc']}`",
        f"- Brier: `{panel['brier']}`",
        f"- ECE: `{panel['ece']}`",
        f"- Markout lift bps: `{panel['markout_lift_bps']}`",
        f"- Quote rate: `{panel['quote_rate']}`",
        "",
        "## Reconciliation",
        "",
        f"- Verdict: `{reconciliation['verdict']}`",
        f"- Max absolute metric delta: `{reconciliation['max_abs_delta']}`",
        f"- Row delta: `{reconciliation['row_delta']}`",
        "",
        "## Artifacts",
        "",
        "- [validation.json](validation.json)",
        "- [partition-metrics.csv](partition-metrics.csv)",
        "",
        "## Caveats",
        "",
        "- The panel replay scores the promoted final model over all requested "
        "dates; it is not an out-of-fold training metric.",
        "- This benchmark is reproducible only on machines with the same local "
        "MarketImmune lake and promoted model artifacts.",
        "",
    ])


def _training_path(date: str) -> Path:
    return (
        LAKE_ROOT
        / "gold"
        / "hyperliquid"
        / "training"
        / COIN
        / f"{COIN}-training-{date}.parquet"
    )


def _source_payload(path: Path) -> dict[str, str | int]:
    return {
        "path": str(path),
        "sha256": _sha256(path),
        "bytes": path.stat().st_size,
    }


def _summary(values: Sequence[float]) -> dict[str, float]:
    ordered = sorted(values)
    return {
        "min": ordered[0],
        "p50": ordered[len(ordered) // 2],
        "p95": ordered[int((len(ordered) - 1) * 0.95)],
        "max": ordered[-1],
    }


def _mean(values: Iterable[float]) -> float:
    items = list(values)
    if not items:
        raise ValueError("cannot average an empty sequence")
    return sum(items) / len(items)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git_sha() -> str:
    return subprocess.check_output(
        ["git", "rev-parse", "HEAD"],
        cwd=REPO_ROOT,
        text=True,
    ).strip()


def _require_paths(*paths: Path) -> None:
    missing = [path for path in paths if not path.exists()]
    if missing:
        commands = [
            "cd C:\\MarketImmune",
            (
                "python scripts\\train_hyperliquid_markout.py --coin SOL "
                "--dates 20260527..20260531 --holdout-date 20260601 "
                "--horizon 10s --lake-root data\\hyperliquid "
                "--model-out "
                "data\\models\\hyperliquid_catboost_SOL_20260527_20260531_"
                "holdout_20260601_10s.cbm "
                "--report "
                "docs\\benchmarks\\hyperliquid_markout_SOL_20260527_20260531_"
                "holdout_20260601.json"
            ),
        ]
        joined = "\n".join(str(path) for path in missing)
        raise FileNotFoundError(
            "Missing local C8 input(s):\n"
            f"{joined}\n"
            "Regenerate them with:\n"
            + "\n".join(commands)
        )


if __name__ == "__main__":
    raise SystemExit(main())
