import json
import math
import os
from typing import Dict, Iterable, Optional


ENTRY_MODEL_FEATURES = [
    "entry_number",
    "price",
    "elapsed",
    "edge",
    "fair",
    "ev",
    "fee",
    "direction_yes",
    "route_maker",
    "route_taker",
    "tactic_momentum",
    "is_late_60",
    "is_late_180",
    "kalman_delta_bps",
    "kalman_velocity_bps_per_min",
    "kalman_projected_delta_bps",
    "kalman_residual_bps",
    "kalman_abs_residual_bps",
    "kalman_uncertainty_bps",
    "kalman_trend_agreement",
]


def _safe_float(value, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        number = float(value)
        if math.isnan(number) or math.isinf(number):
            return default
        return number
    except (TypeError, ValueError):
        return default


def sigmoid(value: float) -> float:
    value = max(min(float(value), 40.0), -40.0)
    return 1.0 / (1.0 + math.exp(-value))


def load_entry_model(path: str) -> Optional[Dict]:
    if not path or not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        model = json.load(f)
    feature_names = model.get("feature_names") or ENTRY_MODEL_FEATURES
    coefficients = model.get("coefficients") or []
    mean = model.get("mean") or [0.0] * len(feature_names)
    scale = model.get("scale") or [1.0] * len(feature_names)
    if not (
        len(feature_names) == len(coefficients) == len(mean) == len(scale)
    ):
        raise ValueError("entry model has inconsistent feature lengths")
    model["feature_names"] = feature_names
    model["coefficients"] = coefficients
    model["mean"] = mean
    model["scale"] = [float(item) if abs(float(item or 0.0)) > 1e-12 else 1.0 for item in scale]
    model["intercept"] = _safe_float(model.get("intercept"), 0.0)
    return model


def score_entry_candidate(model: Dict, features: Dict) -> float:
    feature_names: Iterable[str] = model.get("feature_names") or ENTRY_MODEL_FEATURES
    coefficients = model.get("coefficients") or []
    mean = model.get("mean") or []
    scale = model.get("scale") or []
    total = _safe_float(model.get("intercept"), 0.0)
    for idx, name in enumerate(feature_names):
        value = _safe_float(features.get(name), 0.0)
        center = _safe_float(mean[idx] if idx < len(mean) else 0.0, 0.0)
        spread = _safe_float(scale[idx] if idx < len(scale) else 1.0, 1.0)
        if abs(spread) < 1e-12:
            spread = 1.0
        coef = _safe_float(coefficients[idx] if idx < len(coefficients) else 0.0, 0.0)
        total += coef * ((value - center) / spread)
    return sigmoid(total)
