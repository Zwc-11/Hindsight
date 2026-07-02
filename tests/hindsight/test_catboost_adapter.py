from __future__ import annotations

import importlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from hindsight.models.catboost_adapter import (
    MARKOUT_FEATURE_COLUMNS,
    CatBoostCalibrator,
    CatBoostMarkoutAdapter,
    prepare_markout_feature_row,
)


def write_calibrator(path: Path, **overrides: Any) -> None:
    payload: dict[str, Any] = {
        "method": "isotonic",
        "enabled": True,
        "feature_columns": ["a", "b"],
        "deployment_decision_threshold": 0.3,
        "x_thresholds": [0.0, 1.0],
        "y_thresholds": [0.2, 0.8],
    }
    payload.update(overrides)
    path.write_text(json.dumps(payload), encoding="utf-8")


def gold_row() -> dict[str, Any]:
    return {
        "px": 101.0,
        "sz": 3.0,
        "maker_side": -1.0,
        "l2_mid": 100.0,
        "l2_spread_bps": 2.5,
        "l2_microprice": 100.5,
        "l2_top_imbalance": -0.25,
        "asset_basis_bps": 1.5,
        "asset_funding": 0.01,
        "asset_open_interest": 42.0,
        "asset_premium": -0.02,
    }


def test_load_fails_loudly_without_catboost(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model_path = tmp_path / "model.cbm"
    calibrator_path = tmp_path / "calibrator.json"
    model_path.write_text("placeholder", encoding="utf-8")
    write_calibrator(calibrator_path)
    real_import = importlib.import_module

    def fake_import(name: str, package: str | None = None) -> Any:
        if name == "catboost":
            raise ImportError("missing catboost")
        return real_import(name, package)

    monkeypatch.setattr(importlib, "import_module", fake_import)

    with pytest.raises(RuntimeError, match=r"hindsight\[catboost\]"):
        CatBoostMarkoutAdapter.load(model_path=model_path, calibrator_path=calibrator_path)


def test_load_rejects_missing_model_before_importing_catboost(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="CatBoost model not found"):
        CatBoostMarkoutAdapter.load(
            model_path=tmp_path / "missing.cbm",
            calibrator_path=tmp_path / "missing.json",
        )


def test_adapter_loads_predicts_and_calibrates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model_path = tmp_path / "model.cbm"
    calibrator_path = tmp_path / "calibrator.json"
    model_path.write_text("placeholder", encoding="utf-8")
    write_calibrator(calibrator_path)
    loaded_paths: list[str] = []
    seen_matrices: list[list[list[float]]] = []
    real_import = importlib.import_module

    class FakeClassifier:
        def load_model(self, path: str) -> None:
            loaded_paths.append(path)

        def predict_proba(self, matrix: list[list[float]]) -> list[list[float]]:
            seen_matrices.append(matrix)
            return [[0.75, 0.25]]

    def fake_import(name: str, package: str | None = None) -> Any:
        if name == "catboost":
            return SimpleNamespace(CatBoostClassifier=FakeClassifier)
        return real_import(name, package)

    monkeypatch.setattr(importlib, "import_module", fake_import)

    adapter = CatBoostMarkoutAdapter.load(
        model_path=model_path,
        calibrator_path=calibrator_path,
    )
    predictions = adapter.predict_rows([{"a": "2.0", "b": -1}])

    assert loaded_paths == [str(model_path)]
    assert seen_matrices == [[[2.0, -1.0]]]
    assert predictions[0].raw_probability == pytest.approx(0.25)
    assert predictions[0].probability == pytest.approx(0.35)
    assert predictions[0].quote is False


def test_adapter_quotes_below_deployment_skip_threshold(tmp_path: Path) -> None:
    calibrator_path = tmp_path / "calibrator.json"
    write_calibrator(calibrator_path)
    adapter = CatBoostMarkoutAdapter(
        model=SimpleNamespace(predict_proba=lambda matrix: [[1.0, 0.0]]),
        calibrator=CatBoostCalibrator.from_json(calibrator_path),
    )

    prediction = adapter.predict_rows([{"a": 1.0, "b": 2.0}])[0]

    assert prediction.probability == pytest.approx(0.2)
    assert prediction.quote is True


@pytest.mark.parametrize(
    ("overrides", "match"),
    [
        ({"enabled": "yes"}, "enabled"),
        ({"method": "linear"}, "method"),
        ({"feature_columns": []}, "feature_columns"),
        ({"feature_columns": ["a", 1]}, "feature_columns"),
        ({"x_thresholds": [0.0], "y_thresholds": [0.2]}, "at least two"),
        ({"x_thresholds": [0.5, 0.4]}, "sorted"),
        ({"x_thresholds": "bad"}, "x_thresholds"),
        ({"deployment_decision_threshold": 1.5}, "threshold"),
    ],
)
def test_calibrator_rejects_invalid_schema(
    tmp_path: Path,
    overrides: dict[str, Any],
    match: str,
) -> None:
    calibrator_path = tmp_path / "calibrator.json"
    write_calibrator(calibrator_path, **overrides)

    with pytest.raises(ValueError, match=match):
        CatBoostCalibrator.from_json(calibrator_path)


def test_calibrator_rejects_non_object_json(tmp_path: Path) -> None:
    calibrator_path = tmp_path / "calibrator.json"
    calibrator_path.write_text("[]", encoding="utf-8")

    with pytest.raises(ValueError, match="JSON object"):
        CatBoostCalibrator.from_json(calibrator_path)


def test_calibrator_rejects_missing_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="CatBoost calibrator not found"):
        CatBoostCalibrator.from_json(tmp_path / "missing.json")


def test_calibrator_rejects_mismatched_thresholds(tmp_path: Path) -> None:
    calibrator_path = tmp_path / "calibrator.json"
    write_calibrator(calibrator_path, y_thresholds=[0.2])

    with pytest.raises(ValueError, match="threshold"):
        CatBoostCalibrator.from_json(calibrator_path)


def test_calibrator_interpolates_boundaries_and_exact_threshold(tmp_path: Path) -> None:
    calibrator_path = tmp_path / "calibrator.json"
    write_calibrator(
        calibrator_path,
        x_thresholds=[0.0, 0.5, 1.0],
        y_thresholds=[0.2, 0.4, 0.8],
    )
    calibrator = CatBoostCalibrator.from_json(calibrator_path)

    assert calibrator.transform(0.0) == pytest.approx(0.2)
    assert calibrator.transform(0.5) == pytest.approx(0.4)
    assert calibrator.transform(1.0) == pytest.approx(0.8)


def test_disabled_calibrator_returns_raw_probability_and_no_quote(tmp_path: Path) -> None:
    calibrator_path = tmp_path / "calibrator.json"
    write_calibrator(
        calibrator_path,
        enabled=False,
        deployment_decision_threshold=None,
        x_thresholds=[],
        y_thresholds=[],
    )
    adapter = CatBoostMarkoutAdapter(
        model=SimpleNamespace(predict_proba=lambda matrix: [[0.6, 0.4]]),
        calibrator=CatBoostCalibrator.from_json(calibrator_path),
    )

    assert adapter.calibrator.transform(0.4) == 0.4
    assert adapter.predict_rows([]) == ()
    prediction = adapter.predict_rows([{"a": 1.0, "b": 2.0}])[0]
    assert prediction.probability == pytest.approx(0.4)
    assert prediction.quote is None


def test_predict_rows_fails_on_missing_feature(tmp_path: Path) -> None:
    calibrator_path = tmp_path / "calibrator.json"
    write_calibrator(calibrator_path)
    adapter = CatBoostMarkoutAdapter(
        model=SimpleNamespace(predict_proba=lambda matrix: [[0.5, 0.5]]),
        calibrator=CatBoostCalibrator.from_json(calibrator_path),
    )

    with pytest.raises(ValueError, match="missing features: b"):
        adapter.predict_rows([{"a": 1.0}])


def test_predict_rows_rejects_bad_model_probability_shapes(tmp_path: Path) -> None:
    calibrator_path = tmp_path / "calibrator.json"
    write_calibrator(calibrator_path)
    calibrator = CatBoostCalibrator.from_json(calibrator_path)
    short_output = CatBoostMarkoutAdapter(
        model=SimpleNamespace(predict_proba=lambda matrix: []),
        calibrator=calibrator,
    )
    missing_class_probability = CatBoostMarkoutAdapter(
        model=SimpleNamespace(predict_proba=lambda matrix: [[1.0]]),
        calibrator=calibrator,
    )
    invalid_probability = CatBoostMarkoutAdapter(
        model=SimpleNamespace(predict_proba=lambda matrix: [[-0.1, 1.1]]),
        calibrator=calibrator,
    )

    with pytest.raises(ValueError, match="different number"):
        short_output.predict_rows([{"a": 1.0, "b": 2.0}])
    with pytest.raises(ValueError, match="class-1"):
        missing_class_probability.predict_rows([{"a": 1.0, "b": 2.0}])
    with pytest.raises(ValueError, match="raw_probability"):
        invalid_probability.predict_rows([{"a": 1.0, "b": 2.0}])


def test_markout_adapter_accepts_precomputed_and_gold_rows() -> None:
    calibrator = CatBoostCalibrator(
        enabled=False,
        feature_columns=MARKOUT_FEATURE_COLUMNS,
        decision_threshold=None,
    )
    adapter = CatBoostMarkoutAdapter(
        model=SimpleNamespace(predict_proba=lambda matrix: [[0.5, 0.5]]),
        calibrator=calibrator,
    )
    precomputed = {
        name: float(index + 1) for index, name in enumerate(MARKOUT_FEATURE_COLUMNS)
    }

    assert adapter.feature_vector(precomputed) == [
        float(index + 1) for index in range(len(MARKOUT_FEATURE_COLUMNS))
    ]
    derived = dict(zip(MARKOUT_FEATURE_COLUMNS, adapter.feature_vector(gold_row()), strict=True))
    assert derived["l2_microprice_offset_bps"] == pytest.approx(50.0)
    assert derived["fill_vs_mid_bps"] == pytest.approx(-100.0)


def test_prepare_markout_feature_row_matches_marketimmune_contract() -> None:
    features = prepare_markout_feature_row(gold_row())

    assert tuple(features) == MARKOUT_FEATURE_COLUMNS
    assert features["fee_bps"] == 0.0
    assert features["l2_ofi_event"] == 0.0
    assert features["l2_microprice_offset_bps"] == pytest.approx(50.0)
    assert features["fill_vs_mid_bps"] == pytest.approx(-100.0)


def test_prepare_markout_feature_row_fails_loudly_on_invalid_gold_rows() -> None:
    missing = gold_row()
    missing.pop("l2_mid")
    nonpositive_mid = gold_row()
    nonpositive_mid["l2_mid"] = 0.0
    nonnumeric = gold_row()
    nonnumeric["px"] = "bad"
    nonfinite = gold_row()
    nonfinite["asset_premium"] = float("nan")

    with pytest.raises(ValueError, match="missing markout scoring features: l2_mid"):
        prepare_markout_feature_row(missing)
    with pytest.raises(ValueError, match="l2_mid must be positive"):
        prepare_markout_feature_row(nonpositive_mid)
    with pytest.raises(ValueError, match="px must be numeric"):
        prepare_markout_feature_row(nonnumeric)
    with pytest.raises(ValueError, match="asset_premium must be finite"):
        prepare_markout_feature_row(nonfinite)
