from __future__ import annotations

import json
import math
import os
from typing import Dict, Iterable, Optional


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


def _sigmoid(value: float) -> float:
    value = max(min(float(value), 40.0), -40.0)
    return 1.0 / (1.0 + math.exp(-value))


def load_cheap_reversal_model(path: str) -> Optional[Dict]:
    if not path or not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        model = json.load(f)
    features = model.get("features") or []
    weights = model.get("weights") or {}
    medians = model.get("medians") or {}
    scales = model.get("scales") or {}
    if not features or not isinstance(weights, dict):
        raise ValueError("cheap reversal model is missing features/weights")
    for feature in features:
        scales[feature] = _safe_float(scales.get(feature), 1.0)
        if abs(scales[feature]) < 1e-12:
            scales[feature] = 1.0
        medians[feature] = _safe_float(medians.get(feature), 0.0)
        weights[feature] = _safe_float(weights.get(feature), 0.0)
    model["features"] = features
    model["weights"] = weights
    model["medians"] = medians
    model["scales"] = scales
    model["bias"] = _safe_float(model.get("bias"), 0.0)
    return model


def score_cheap_reversal_candidate(model: Dict, features: Dict) -> float:
    total = _safe_float(model.get("bias"), 0.0)
    feature_names: Iterable[str] = model.get("features") or []
    weights = model.get("weights") or {}
    medians = model.get("medians") or {}
    scales = model.get("scales") or {}
    for name in feature_names:
        value = _safe_float(features.get(name), 0.0)
        center = _safe_float(medians.get(name), 0.0)
        scale = _safe_float(scales.get(name), 1.0)
        if abs(scale) < 1e-12:
            scale = 1.0
        total += _safe_float(weights.get(name), 0.0) * ((value - center) / scale)
    return _sigmoid(total)
