from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


ROOT_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT_DIR / "data"


FEATURE_COLUMNS = [
    "elapsed_s",
    "remaining_s",
    "elapsed_fraction",
    "remaining_fraction",
    "delta_bps",
    "abs_delta_bps",
    "velocity_bps_per_min",
    "confidence",
    "fair_yes",
    "raw_fair_yes",
    "market_fair_yes",
    "market_blend_weight",
    "yes_best_bid",
    "yes_best_ask",
    "yes_mid",
    "yes_spread",
    "no_best_bid",
    "no_best_ask",
    "no_mid",
    "no_spread",
    "yes_best_bid_size",
    "yes_best_ask_size",
    "no_best_bid_size",
    "no_best_ask_size",
    "yes_bid_depth_40_60",
    "yes_ask_depth_40_60",
    "no_bid_depth_40_60",
    "no_ask_depth_40_60",
    "bid_depth_imbalance_40_60",
    "ask_depth_imbalance_40_60",
    "spread_sum",
    "yes_market_mid_share",
]


def sigmoid(values: np.ndarray) -> np.ndarray:
    values = np.clip(values, -40.0, 40.0)
    return 1.0 / (1.0 + np.exp(-values))


def log_loss(y_true: np.ndarray, proba: np.ndarray) -> float:
    proba = np.clip(proba, 1e-6, 1.0 - 1e-6)
    return float(-np.mean(y_true * np.log(proba) + (1.0 - y_true) * np.log(1.0 - proba)))


def brier_score(y_true: np.ndarray, proba: np.ndarray) -> float:
    return float(np.mean((proba - y_true) ** 2))


def train_logistic_regression(
    x_train: np.ndarray,
    y_train: np.ndarray,
    learning_rate: float = 0.05,
    l2: float = 0.01,
    iterations: int = 1200,
) -> tuple[np.ndarray, float]:
    weights = np.zeros(x_train.shape[1], dtype=float)
    bias = 0.0
    n = float(len(y_train))
    for _ in range(iterations):
        pred = sigmoid(x_train @ weights + bias)
        error = pred - y_train
        grad_w = (x_train.T @ error) / n + l2 * weights
        grad_b = float(error.mean())
        weights -= learning_rate * grad_w
        bias -= learning_rate * grad_b
    return weights, bias


def prepare_matrix(df: pd.DataFrame, medians: pd.Series | None = None, scales: pd.Series | None = None):
    available = [column for column in FEATURE_COLUMNS if column in df.columns]
    x = df[available].apply(pd.to_numeric, errors="coerce")
    if medians is None:
        medians = x.median(numeric_only=True).fillna(0.0)
    x = x.fillna(medians)
    if scales is None:
        scales = x.std(numeric_only=True).replace(0, 1.0).fillna(1.0)
    x = (x - medians) / scales
    return x.to_numpy(dtype=float), available, medians, scales


def summarize_predictions(name: str, y_true: np.ndarray, proba: np.ndarray) -> dict[str, Any]:
    return {
        "model": name,
        "rows": int(len(y_true)),
        "actual_yes_rate": round(float(y_true.mean()), 6),
        "avg_prediction": round(float(np.mean(proba)), 6),
        "brier": round(brier_score(y_true, proba), 6),
        "log_loss": round(log_loss(y_true, proba), 6),
    }


def calibration_table(y_true: np.ndarray, proba: np.ndarray, bins: int = 10) -> list[dict[str, Any]]:
    frame = pd.DataFrame({"y": y_true, "p": proba})
    frame["bin"] = pd.cut(frame["p"], bins=np.linspace(0.0, 1.0, bins + 1), include_lowest=True)
    rows = []
    for interval, group in frame.groupby("bin", observed=False):
        if group.empty:
            continue
        rows.append({
            "bin": str(interval),
            "rows": int(len(group)),
            "actual_yes_rate": round(float(group["y"].mean()), 6),
            "avg_prediction": round(float(group["p"].mean()), 6),
        })
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description="Train/evaluate a first Fair Value probability model.")
    parser.add_argument("--data-dir", default=str(DATA_DIR))
    parser.add_argument("--dataset", default="fair_value_training_signals.csv")
    parser.add_argument("--model-output", default="fair_value_model_candidate.json")
    parser.add_argument("--report-output", default="fair_value_model_report.json")
    parser.add_argument("--train-fraction", type=float, default=0.70)
    parser.add_argument("--iterations", type=int, default=1200)
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    dataset_path = data_dir / args.dataset
    if not dataset_path.exists():
        raise FileNotFoundError(f"Dataset not found: {dataset_path}")

    df = pd.read_csv(dataset_path, low_memory=False)
    df["window_start_ts"] = pd.to_numeric(df["window_start_ts"], errors="coerce")
    df["yes_won"] = pd.to_numeric(df["yes_won"], errors="coerce")
    df = df.dropna(subset=["window_start_ts", "yes_won"]).copy()
    df = df.sort_values(["window_start_ts", "sample_ts_utc"])

    windows = np.array(sorted(df["window_start_ts"].dropna().unique()))
    split_index = max(1, min(len(windows) - 1, int(len(windows) * args.train_fraction)))
    split_window = windows[split_index]
    train = df[df["window_start_ts"] < split_window].copy()
    test = df[df["window_start_ts"] >= split_window].copy()
    if train.empty or test.empty:
        raise RuntimeError("Temporal split produced an empty train or test set.")

    y_train = train["yes_won"].to_numpy(dtype=float)
    y_test = test["yes_won"].to_numpy(dtype=float)
    x_train, features, medians, scales = prepare_matrix(train)
    x_test, _, _, _ = prepare_matrix(test, medians=medians, scales=scales)

    weights, bias = train_logistic_regression(
        x_train=x_train,
        y_train=y_train,
        iterations=max(int(args.iterations), 1),
    )
    model_proba = sigmoid(x_test @ weights + bias)

    baselines = []
    for name, column in [
        ("market_fair_yes", "market_fair_yes"),
        ("current_fair_yes", "fair_yes"),
        ("raw_fair_yes", "raw_fair_yes"),
        ("yes_market_mid_share", "yes_market_mid_share"),
    ]:
        if column not in test.columns:
            continue
        proba = pd.to_numeric(test[column], errors="coerce").fillna(0.5).clip(0.001, 0.999).to_numpy(dtype=float)
        baselines.append(summarize_predictions(name, y_test, proba))

    report = {
        "rows": int(len(df)),
        "windows": int(len(windows)),
        "train_rows": int(len(train)),
        "test_rows": int(len(test)),
        "train_windows": int(train["window_start_ts"].nunique()),
        "test_windows": int(test["window_start_ts"].nunique()),
        "split_window_start_ts": int(split_window),
        "candidate": summarize_predictions("logistic_candidate_v1", y_test, model_proba),
        "baselines": baselines,
        "candidate_calibration": calibration_table(y_test, model_proba),
    }

    model_payload = {
        "model_type": "logistic_regression_numpy",
        "model_version": "fair_value_logistic_candidate_v1",
        "target": "yes_won",
        "features": features,
        "medians": {key: float(value) for key, value in medians.to_dict().items()},
        "scales": {key: float(value) for key, value in scales.to_dict().items()},
        "weights": {feature: float(weight) for feature, weight in zip(features, weights)},
        "bias": float(bias),
        "report": report,
    }

    (data_dir / args.model_output).write_text(json.dumps(model_payload, indent=2), encoding="utf-8")
    (data_dir / args.report_output).write_text(json.dumps(report, indent=2), encoding="utf-8")

    print("[model] temporal split")
    print(f"[model] train_rows={len(train)} test_rows={len(test)} train_windows={report['train_windows']} test_windows={report['test_windows']}")
    print("[model] candidate", report["candidate"])
    for baseline in baselines:
        print("[model] baseline", baseline)
    print(f"[model] wrote {data_dir / args.model_output}")
    print(f"[model] wrote {data_dir / args.report_output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
