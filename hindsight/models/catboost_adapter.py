"""CatBoost markout model adapter."""

from __future__ import annotations

import importlib
import json
import math
from bisect import bisect_left
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, cast

HYPERLIQUID_OFI_COLUMNS = ("l2_ofi_event", "l2_ofi_1s", "l2_ofi_5s", "l2_ofi_10s")
MARKOUT_FEATURE_COLUMNS = (
    "sz",
    "maker_side",
    "fee_bps",
    "l2_spread_bps",
    "l2_microprice_offset_bps",
    "l2_top_imbalance",
    *HYPERLIQUID_OFI_COLUMNS,
    "fill_vs_mid_bps",
    "asset_basis_bps",
    "asset_funding",
    "asset_open_interest",
    "asset_premium",
)


class _CatBoostModel(Protocol):
    def load_model(self, path: str) -> Any:
        """Load a serialized CatBoost model."""

    def predict_proba(self, matrix: Sequence[Sequence[float]]) -> Sequence[Sequence[float]]:
        """Return class probabilities for each feature row."""


@dataclass(frozen=True, slots=True)
class CatBoostCalibrator:
    """JSON isotonic calibrator emitted by MarketImmune's trainer."""

    enabled: bool
    feature_columns: tuple[str, ...]
    decision_threshold: float | None
    x_thresholds: tuple[float, ...] = ()
    y_thresholds: tuple[float, ...] = ()

    def __post_init__(self) -> None:
        if not self.feature_columns:
            raise ValueError("calibrator feature_columns cannot be empty")
        if self.decision_threshold is not None:
            _validate_probability(self.decision_threshold, "deployment_decision_threshold")
        if not self.enabled:
            return
        if len(self.x_thresholds) != len(self.y_thresholds):
            raise ValueError("calibrator threshold arrays must have the same length")
        if len(self.x_thresholds) < 2:
            raise ValueError("enabled calibrator requires at least two thresholds")
        previous = -math.inf
        for index, value in enumerate(self.x_thresholds):
            _validate_probability(value, f"x_thresholds[{index}]")
            if value < previous:
                raise ValueError("calibrator x_thresholds must be sorted")
            previous = value
        for index, value in enumerate(self.y_thresholds):
            _validate_probability(value, f"y_thresholds[{index}]")

    @classmethod
    def from_json(cls, path: Path) -> CatBoostCalibrator:
        if not path.exists():
            raise FileNotFoundError(f"CatBoost calibrator not found: {path}")
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("CatBoost calibrator must be a JSON object")
        if payload.get("method") != "isotonic":
            raise ValueError("CatBoost calibrator method must be isotonic")
        enabled = _required_bool(payload, "enabled")
        return cls(
            enabled=enabled,
            feature_columns=_required_str_tuple(payload, "feature_columns"),
            decision_threshold=_optional_probability(payload.get("deployment_decision_threshold")),
            x_thresholds=_float_tuple(payload.get("x_thresholds", ()), "x_thresholds"),
            y_thresholds=_float_tuple(payload.get("y_thresholds", ()), "y_thresholds"),
        )

    def transform(self, raw_probability: float) -> float:
        raw = _validate_probability(raw_probability, "raw_probability")
        if not self.enabled:
            return raw
        return _validate_probability(
            _interpolate(raw, self.x_thresholds, self.y_thresholds),
            "calibrated_probability",
        )


@dataclass(frozen=True, slots=True)
class CatBoostMarkoutPrediction:
    """One CatBoost markout score."""

    raw_probability: float
    probability: float
    quote: bool | None


@dataclass(frozen=True, slots=True)
class CatBoostMarkoutAdapter:
    """Loaded CatBoost model plus its deployment calibrator."""

    model: _CatBoostModel
    calibrator: CatBoostCalibrator

    @classmethod
    def load(cls, *, model_path: Path, calibrator_path: Path) -> CatBoostMarkoutAdapter:
        if not model_path.exists():
            raise FileNotFoundError(f"CatBoost model not found: {model_path}")
        calibrator = CatBoostCalibrator.from_json(calibrator_path)
        classifier_type = _catboost_classifier_type()
        model = classifier_type()
        model.load_model(str(model_path))
        return cls(model=model, calibrator=calibrator)

    @property
    def feature_columns(self) -> tuple[str, ...]:
        return self.calibrator.feature_columns

    def predict_rows(
        self,
        rows: Sequence[Mapping[str, object]],
    ) -> tuple[CatBoostMarkoutPrediction, ...]:
        if not rows:
            return ()
        matrix = [self.feature_vector(row) for row in rows]
        probabilities = self.model.predict_proba(matrix)
        if len(probabilities) != len(matrix):
            raise ValueError("CatBoost returned a different number of probability rows")
        return tuple(self._prediction_from_probability(row) for row in probabilities)

    def feature_vector(self, row: Mapping[str, object]) -> list[float]:
        features = self._feature_mapping(row)
        missing = [
            name
            for name in self.feature_columns
            if name not in features or features[name] is None
        ]
        if missing:
            joined = ", ".join(missing[:5])
            suffix = "" if len(missing) <= 5 else f", +{len(missing) - 5} more"
            raise ValueError(f"CatBoost row missing features: {joined}{suffix}")
        return [_as_float(features[name], name) for name in self.feature_columns]

    def _feature_mapping(self, row: Mapping[str, object]) -> Mapping[str, object]:
        if self.feature_columns == MARKOUT_FEATURE_COLUMNS:
            if all(name in row and row[name] is not None for name in self.feature_columns):
                return row
            return prepare_markout_feature_row(row)
        return row

    def _prediction_from_probability(
        self,
        probability_row: Sequence[float],
    ) -> CatBoostMarkoutPrediction:
        if len(probability_row) < 2:
            raise ValueError("CatBoost probability row must contain class-1 probability")
        raw_probability = _validate_probability(float(probability_row[1]), "raw_probability")
        probability = self.calibrator.transform(raw_probability)
        threshold = self.calibrator.decision_threshold
        quote = None if threshold is None else probability < threshold
        return CatBoostMarkoutPrediction(
            raw_probability=raw_probability,
            probability=probability,
            quote=quote,
        )


def prepare_markout_feature_row(row: Mapping[str, object]) -> dict[str, float]:
    """Derive the deployable markout feature vector from one Gold training row."""

    required = (
        "px",
        "sz",
        "maker_side",
        "l2_mid",
        "l2_spread_bps",
        "l2_microprice",
        "l2_top_imbalance",
        "asset_basis_bps",
        "asset_funding",
        "asset_open_interest",
        "asset_premium",
    )
    missing = [name for name in required if name not in row or row[name] is None]
    if missing:
        joined = ", ".join(missing[:5])
        suffix = "" if len(missing) <= 5 else f", +{len(missing) - 5} more"
        raise ValueError(f"Gold row missing markout scoring features: {joined}{suffix}")

    mid = _as_float(row["l2_mid"], "l2_mid")
    if mid <= 0.0:
        raise ValueError("Gold row l2_mid must be positive for bps feature derivation")

    features = {
        "sz": _as_float(row["sz"], "sz"),
        "maker_side": _as_float(row["maker_side"], "maker_side"),
        "fee_bps": _as_float(row.get("fee_bps", 0.0) or 0.0, "fee_bps"),
        "l2_spread_bps": _as_float(row["l2_spread_bps"], "l2_spread_bps"),
        "l2_microprice_offset_bps": (
            (_as_float(row["l2_microprice"], "l2_microprice") - mid) / mid * 10_000.0
        ),
        "l2_top_imbalance": _as_float(row["l2_top_imbalance"], "l2_top_imbalance"),
        "fill_vs_mid_bps": (
            (_as_float(row["px"], "px") - mid)
            / mid
            * 10_000.0
            * _as_float(row["maker_side"], "maker_side")
        ),
        "asset_basis_bps": _as_float(row["asset_basis_bps"], "asset_basis_bps"),
        "asset_funding": _as_float(row["asset_funding"], "asset_funding"),
        "asset_open_interest": _as_float(row["asset_open_interest"], "asset_open_interest"),
        "asset_premium": _as_float(row["asset_premium"], "asset_premium"),
    }
    for column in HYPERLIQUID_OFI_COLUMNS:
        features[column] = _as_float(row.get(column, 0.0) or 0.0, column)
    return {name: features[name] for name in MARKOUT_FEATURE_COLUMNS}


def _catboost_classifier_type() -> type[_CatBoostModel]:
    try:
        module = importlib.import_module("catboost")
    except ImportError as exc:  # pragma: no cover - environment boundary.
        raise RuntimeError(
            "Install optional CatBoost support first: "
            "python -m pip install 'hindsight[catboost]'"
        ) from exc
    return cast(type[_CatBoostModel], module.CatBoostClassifier)


def _required_bool(payload: Mapping[str, Any], key: str) -> bool:
    value = payload.get(key)
    if not isinstance(value, bool):
        raise ValueError(f"CatBoost calibrator {key} must be a boolean")
    return value


def _required_str_tuple(payload: Mapping[str, Any], key: str) -> tuple[str, ...]:
    value = payload.get(key)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"CatBoost calibrator {key} must be a string list")
    return tuple(value)


def _float_tuple(value: object, name: str) -> tuple[float, ...]:
    if not isinstance(value, list | tuple):
        raise ValueError(f"CatBoost calibrator {name} must be a number list")
    return tuple(_as_float(item, f"{name}[]") for item in value)


def _optional_probability(value: object) -> float | None:
    if value is None:
        return None
    return _validate_probability(_as_float(value, "deployment_decision_threshold"), "threshold")


def _validate_probability(value: float, name: str) -> float:
    if not math.isfinite(value) or value < 0.0 or value > 1.0:
        raise ValueError(f"{name} must be a finite probability in [0, 1]")
    return value


def _interpolate(x: float, xs: tuple[float, ...], ys: tuple[float, ...]) -> float:
    if x <= xs[0]:
        return ys[0]
    if x >= xs[-1]:
        return ys[-1]
    right = bisect_left(xs, x)
    if xs[right] == x:
        return ys[right]
    left = right - 1
    denominator = xs[right] - xs[left]
    weight = (x - xs[left]) / denominator
    return ys[left] + (ys[right] - ys[left]) * weight


def _as_float(value: object, name: str) -> float:
    try:
        result = float(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be numeric") from exc
    if not math.isfinite(result):
        raise ValueError(f"{name} must be finite")
    return result
