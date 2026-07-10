import argparse
import datetime as dt
import json
import math
import os
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
import sys
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.fair_value_entry_model import ENTRY_MODEL_FEATURES


def _series_float(df: pd.DataFrame, *names: str, default: float = 0.0) -> pd.Series:
    for name in names:
        if name in df.columns:
            return pd.to_numeric(df[name], errors="coerce").fillna(default)
    return pd.Series(default, index=df.index, dtype="float64")


def _series_str(df: pd.DataFrame, *names: str) -> pd.Series:
    for name in names:
        if name in df.columns:
            return df[name].fillna("").astype(str)
    return pd.Series("", index=df.index, dtype="object")


def _auc(y_true: np.ndarray, score: np.ndarray) -> float:
    positives = int(y_true.sum())
    negatives = int(len(y_true) - positives)
    if positives == 0 or negatives == 0:
        return 0.5
    order = np.argsort(score)
    ranks = np.empty_like(order, dtype="float64")
    ranks[order] = np.arange(1, len(score) + 1, dtype="float64")
    rank_sum_pos = ranks[y_true == 1].sum()
    return float((rank_sum_pos - positives * (positives + 1) / 2) / (positives * negatives))


def _sigmoid(values: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-np.clip(values, -40.0, 40.0)))


def _build_features(df: pd.DataFrame) -> pd.DataFrame:
    route = _series_str(df, "route_entry", "route").str.lower()
    tactic = _series_str(df, "tactic_entry", "tactic").str.lower()
    direction = _series_str(df, "direction_entry", "direction").str.lower()
    features = pd.DataFrame(index=df.index)
    features["entry_number"] = _series_float(
        df,
        "entry_number_in_window_entry",
        "entry_number_in_window",
        default=1.0,
    )
    features["price"] = _series_float(df, "price", "entry_price")
    features["elapsed"] = _series_float(df, "elapsed_s")
    features["edge"] = _series_float(df, "edge_probability_entry", "edge_probability")
    features["fair"] = _series_float(df, "fair_probability_entry", "fair_probability")
    features["ev"] = _series_float(df, "ev_per_usd_entry", "ev_per_usd")
    features["fee"] = _series_float(df, "fee_usd_entry", "fee_usd")
    features["direction_yes"] = (direction == "yes").astype(float)
    features["route_maker"] = route.str.startswith("maker").astype(float)
    features["route_taker"] = (route == "taker").astype(float)
    features["tactic_momentum"] = (tactic == "momentum").astype(float)
    features["is_late_60"] = (features["elapsed"] >= 60.0).astype(float)
    features["is_late_180"] = (features["elapsed"] >= 180.0).astype(float)
    for column in [
        "kalman_delta_bps",
        "kalman_velocity_bps_per_min",
        "kalman_projected_delta_bps",
        "kalman_residual_bps",
        "kalman_abs_residual_bps",
        "kalman_uncertainty_bps",
        "kalman_trend_agreement",
    ]:
        features[column] = _series_float(df, column)
    return features[ENTRY_MODEL_FEATURES].fillna(0.0)


def train_logistic(X: np.ndarray, y: np.ndarray, iterations: int = 8000, lr: float = 0.08, l2: float = 0.02):
    weights = np.zeros(X.shape[1], dtype="float64")
    intercept = 0.0
    n = float(len(y))
    for _ in range(iterations):
        pred = _sigmoid(X @ weights + intercept)
        error = pred - y
        grad_w = (X.T @ error) / n + l2 * weights
        grad_b = float(error.mean())
        weights -= lr * grad_w
        intercept -= lr * grad_b
    return weights, intercept


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="data/fair_value_training_entries.csv")
    parser.add_argument("--output", default="data/fair_value_entry_model.json")
    args = parser.parse_args()

    input_path = Path(args.input)
    output_path = Path(args.output)
    df = pd.read_csv(input_path)
    result = _series_str(df, "result")
    mask = result.isin(["WIN", "LOSS"])
    df = df.loc[mask].copy()
    y = (result.loc[mask] == "WIN").astype(float).to_numpy()
    features = _build_features(df)
    mean = features.mean().to_numpy(dtype="float64")
    scale = features.std(ddof=0).replace(0.0, 1.0).to_numpy(dtype="float64")
    X = (features.to_numpy(dtype="float64") - mean) / scale
    weights, intercept = train_logistic(X, y)
    probabilities = _sigmoid(X @ weights + intercept)
    brier = float(np.mean((probabilities - y) ** 2))
    auc = _auc(y, probabilities)

    model = {
        "model_type": "logistic_regression",
        "trained_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "source": str(input_path).replace("\\", "/"),
        "train_rows": int(len(df)),
        "train_win_rate": round(float(y.mean()), 6) if len(y) else 0.0,
        "train_auc": round(auc, 6),
        "train_brier": round(brier, 6),
        "feature_names": ENTRY_MODEL_FEATURES,
        "mean": [round(float(item), 10) for item in mean],
        "scale": [round(float(item), 10) if abs(float(item)) > 1e-12 else 1.0 for item in scale],
        "coefficients": [round(float(item), 10) for item in weights],
        "intercept": round(float(intercept), 10),
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(model, f, indent=2)
    print(
        f"wrote {output_path} rows={model['train_rows']} "
        f"auc={model['train_auc']:.4f} brier={model['train_brier']:.4f}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
