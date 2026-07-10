from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from .edge_scanner import (
    _delta_bucket,
    _elapsed_bucket,
    _iter_signal_rows,
    _price_bucket,
    finite_float,
    load_window_labels,
)
from .fair_value import taker_fee_fraction


FEATURE_COLUMNS = [
    "elapsed_s",
    "remaining_s",
    "elapsed_fraction",
    "price",
    "maker_price",
    "side_spread",
    "side_mid",
    "side_bid_depth_40_60",
    "side_ask_depth_40_60",
    "is_yes",
    "delta_bps",
    "abs_delta_bps",
    "velocity_bps_per_min",
    "fair_probability",
    "raw_fair_probability",
    "market_probability",
    "confidence",
    "continuation_probability",
    "contrarian_probability",
    "kalman_delta_bps",
    "kalman_velocity_bps_per_min",
    "kalman_projected_delta_bps",
    "kalman_residual_bps",
    "kalman_abs_residual_bps",
    "kalman_uncertainty_bps",
    "kalman_trend_agreement",
]


@dataclass(frozen=True)
class CheapReversalConfig:
    data_dir: Path
    output_dir: Path
    start_utc: pd.Timestamp | None = None
    end_utc: pd.Timestamp | None = None
    min_price: float = 0.01
    max_price: float = 0.42
    min_abs_delta_bps: float = 0.5
    fee_rate: float = 0.07
    folds: int = 5
    max_signal_rows: int = 0
    min_train_rows: int = 150
    min_test_rows: int = 20
    iterations: int = 900
    learning_rate: float = 0.04
    l2: float = 0.02


def sigmoid(values: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-np.clip(values, -40.0, 40.0)))


def log_loss(y_true: np.ndarray, proba: np.ndarray) -> float:
    proba = np.clip(proba, 1e-6, 1.0 - 1e-6)
    return float(-np.mean(y_true * np.log(proba) + (1.0 - y_true) * np.log(1.0 - proba)))


def brier_score(y_true: np.ndarray, proba: np.ndarray) -> float:
    return float(np.mean((proba - y_true) ** 2))


def roc_auc(y_true: np.ndarray, proba: np.ndarray) -> float:
    y_true = np.asarray(y_true, dtype=float)
    proba = np.asarray(proba, dtype=float)
    positives = int(y_true.sum())
    negatives = int(len(y_true) - positives)
    if positives == 0 or negatives == 0:
        return float("nan")
    ranks = pd.Series(proba).rank(method="average").to_numpy(dtype=float)
    rank_sum_pos = float(ranks[y_true == 1].sum())
    return (rank_sum_pos - positives * (positives + 1) / 2.0) / (positives * negatives)


def _numeric(df: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    for column in columns:
        if column in df.columns:
            df[column] = pd.to_numeric(df[column], errors="coerce")
    return df


def _side_probability(row: pd.Series, side: str, prefix: str) -> float:
    if prefix == "fair":
        yes_value = finite_float(row.get("fair_yes"), 0.5)
        no_value = finite_float(row.get("fair_no"), 1.0 - yes_value)
    elif prefix == "raw":
        yes_value = finite_float(row.get("raw_fair_yes"), 0.5)
        no_value = finite_float(row.get("raw_fair_no"), 1.0 - yes_value)
    else:
        yes_value = finite_float(row.get("market_fair_yes"), 0.5)
        no_value = 1.0 - yes_value
    return no_value if side == "No" else yes_value


def _pnl_per_usd(side_won: int, price: float, fee_rate: float) -> float:
    if not math.isfinite(price) or price <= 0.01 or price >= 0.99:
        return float("nan")
    fee = taker_fee_fraction(price, fee_rate)
    if int(side_won) == 1:
        return (1.0 / price) - 1.0 - fee
    return -1.0 - fee


def _opportunity_from_signal(row: pd.Series, fee_rate: float) -> dict[str, Any] | None:
    delta_bps = finite_float(row.get("delta_bps"), 0.0)
    if abs(delta_bps) < 1e-12:
        return None
    side = "No" if delta_bps > 0 else "Yes"
    prefix = "no" if side == "No" else "yes"
    price = finite_float(row.get(f"{prefix}_best_ask"), 0.0)
    maker_price = finite_float(row.get(f"{prefix}_best_bid"), 0.0)
    yes_won = int(finite_float(row.get("yes_won"), 0.0))
    side_won = yes_won if side == "Yes" else 1 - yes_won
    elapsed = finite_float(row.get("elapsed_s"), 0.0)
    abs_delta = abs(delta_bps)
    opening_price = finite_float(row.get("opening_price"), 0.0)
    latest_price = finite_float(row.get("latest_price"), 0.0)
    elapsed_minutes = elapsed / 60.0 if elapsed > 0 else float("nan")
    fair_probability = _side_probability(row, side, "fair")
    raw_fair_probability = _side_probability(row, side, "raw")
    market_probability = _side_probability(row, side, "market")
    pnl = _pnl_per_usd(side_won, price, fee_rate)
    return {
        "sample_ts_utc": row.get("sample_ts_utc"),
        "slug": str(row.get("slug")),
        "window_start_ts": row.get("window_start_ts"),
        "side": side,
        "is_yes": 1 if side == "Yes" else 0,
        "side_won": int(side_won),
        "price": price,
        "maker_price": maker_price,
        "pnl_per_usd": pnl,
        "fee_fraction": taker_fee_fraction(price, fee_rate) if price > 0 else float("nan"),
        "elapsed_s": elapsed,
        "remaining_s": max(300.0 - elapsed, 0.0),
        "elapsed_fraction": min(max(elapsed / 300.0, 0.0), 1.0),
        "delta_bps": delta_bps,
        "abs_delta_bps": abs_delta,
        "velocity_bps_per_min": delta_bps / elapsed_minutes if elapsed_minutes and math.isfinite(elapsed_minutes) else 0.0,
        "opening_price": opening_price,
        "latest_price": latest_price,
        "latest_delta_usd": latest_price - opening_price if opening_price and latest_price else float("nan"),
        "fair_probability": fair_probability,
        "raw_fair_probability": raw_fair_probability,
        "market_probability": market_probability,
        "confidence": finite_float(row.get("confidence"), 0.0),
        "continuation_probability": finite_float(row.get("continuation_probability"), 0.5),
        "contrarian_probability": finite_float(row.get("contrarian_probability"), 0.5),
        "side_spread": finite_float(row.get(f"{prefix}_spread"), 0.0),
        "side_mid": finite_float(row.get(f"{prefix}_mid"), 0.0),
        "side_bid_depth_40_60": finite_float(row.get(f"{prefix}_bid_depth_40_60"), 0.0),
        "side_ask_depth_40_60": finite_float(row.get(f"{prefix}_ask_depth_40_60"), 0.0),
        "elapsed_bucket": _elapsed_bucket(elapsed),
        "price_bucket": _price_bucket(price),
        "delta_bucket": _delta_bucket(abs_delta),
        "kalman_delta_bps": finite_float(row.get("kalman_delta_bps"), 0.0),
        "kalman_velocity_bps_per_min": finite_float(row.get("kalman_velocity_bps_per_min"), 0.0),
        "kalman_projected_delta_bps": finite_float(row.get("kalman_projected_delta_bps"), 0.0),
        "kalman_residual_bps": finite_float(row.get("kalman_residual_bps"), 0.0),
        "kalman_abs_residual_bps": finite_float(row.get("kalman_abs_residual_bps"), 0.0),
        "kalman_uncertainty_bps": finite_float(row.get("kalman_uncertainty_bps"), 0.0),
        "kalman_trend_agreement": finite_float(row.get("kalman_trend_agreement"), 0.0),
    }


def build_cheap_reversal_opportunities(config: CheapReversalConfig) -> pd.DataFrame:
    signal_path = config.data_dir / "fair_value_signals.csv"
    if not signal_path.exists():
        return pd.DataFrame()
    labels = load_window_labels(config.data_dir)
    if labels.empty:
        return pd.DataFrame()
    labels = labels[["slug", "window_start_ts", "yes_won"]].copy()
    labels["yes_won"] = pd.to_numeric(labels["yes_won"], errors="coerce")
    labels = labels.dropna(subset=["slug", "yes_won"])
    label_by_slug = {
        str(row["slug"]): {
            "window_start_ts": row.get("window_start_ts"),
            "yes_won": int(row["yes_won"]),
        }
        for _, row in labels.iterrows()
    }
    rows: list[dict[str, Any]] = []
    for index, raw in enumerate(_iter_signal_rows(signal_path, config.start_utc, config.end_utc), start=1):
        label = label_by_slug.get(str(raw.get("slug")))
        if not label:
            continue
        raw["window_start_ts"] = label["window_start_ts"]
        raw["yes_won"] = label["yes_won"]
        row = pd.Series(raw)
        opportunity = _opportunity_from_signal(row, config.fee_rate)
        if not opportunity:
            continue
        if opportunity["price"] < config.min_price or opportunity["price"] > config.max_price:
            continue
        if opportunity["abs_delta_bps"] < config.min_abs_delta_bps:
            continue
        rows.append(opportunity)
        if config.max_signal_rows and index >= config.max_signal_rows:
            break
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df["sample_ts_utc"] = pd.to_datetime(df["sample_ts_utc"], errors="coerce", utc=True)
    df = _numeric(df, FEATURE_COLUMNS + ["window_start_ts", "side_won", "pnl_per_usd"])
    df = df.dropna(subset=["sample_ts_utc", "slug", "window_start_ts", "side_won", "price"])
    df = df.sort_values(["window_start_ts", "sample_ts_utc"])
    # One opportunity per market/side/bucket. This avoids treating every repeated
    # snapshot inside the same setup as an independent trade.
    dedupe_cols = ["slug", "side", "elapsed_bucket", "price_bucket", "delta_bucket"]
    df = df.drop_duplicates(dedupe_cols, keep="first").reset_index(drop=True)
    return df


def prepare_matrix(
    df: pd.DataFrame,
    medians: pd.Series | None = None,
    scales: pd.Series | None = None,
) -> tuple[np.ndarray, list[str], pd.Series, pd.Series]:
    available = [column for column in FEATURE_COLUMNS if column in df.columns]
    x = df[available].apply(pd.to_numeric, errors="coerce")
    if medians is None:
        medians = x.median(numeric_only=True).fillna(0.0)
    x = x.fillna(medians)
    if scales is None:
        scales = x.std(numeric_only=True).replace(0, 1.0).fillna(1.0)
    scales = scales.replace(0, 1.0).fillna(1.0)
    x = (x - medians) / scales
    return x.to_numpy(dtype=float), available, medians, scales


def train_logistic_regression(
    x_train: np.ndarray,
    y_train: np.ndarray,
    learning_rate: float,
    l2: float,
    iterations: int,
) -> tuple[np.ndarray, float]:
    weights = np.zeros(x_train.shape[1], dtype=float)
    bias = 0.0
    n = max(float(len(y_train)), 1.0)
    for _ in range(max(int(iterations), 1)):
        pred = sigmoid(x_train @ weights + bias)
        error = pred - y_train
        grad_w = (x_train.T @ error) / n + float(l2) * weights
        grad_b = float(error.mean())
        weights -= float(learning_rate) * grad_w
        bias -= float(learning_rate) * grad_b
    return weights, bias


def model_metrics(y_true: np.ndarray, proba: np.ndarray) -> dict[str, Any]:
    return {
        "rows": int(len(y_true)),
        "win_rate": round(float(np.mean(y_true)), 6) if len(y_true) else float("nan"),
        "avg_prediction": round(float(np.mean(proba)), 6) if len(proba) else float("nan"),
        "auc": round(roc_auc(y_true, proba), 6),
        "brier": round(brier_score(y_true, proba), 6),
        "log_loss": round(log_loss(y_true, proba), 6),
    }


def _score_fold(train: pd.DataFrame, test: pd.DataFrame, config: CheapReversalConfig) -> pd.DataFrame:
    y_train = train["side_won"].to_numpy(dtype=float)
    x_train, features, medians, scales = prepare_matrix(train)
    weights, bias = train_logistic_regression(
        x_train,
        y_train,
        learning_rate=config.learning_rate,
        l2=config.l2,
        iterations=config.iterations,
    )
    x_test, _, _, _ = prepare_matrix(test, medians=medians, scales=scales)
    scored = test.copy()
    scored["model_probability"] = sigmoid(x_test @ weights + bias)
    scored["model_ev_per_usd"] = (
        scored["model_probability"] / scored["price"].clip(lower=0.01)
        - 1.0
        - scored["price"].map(lambda price: taker_fee_fraction(float(price), config.fee_rate))
    )
    scored.attrs["features"] = features
    return scored


def _select_rule_rows(
    scored: pd.DataFrame,
    min_probability: float,
    min_ev: float,
    max_price: float,
    max_entries_per_window: int,
) -> pd.DataFrame:
    selected = scored[
        (scored["model_probability"] >= float(min_probability))
        & (scored["model_ev_per_usd"] >= float(min_ev))
        & (scored["price"] <= float(max_price))
    ].copy()
    if selected.empty:
        return selected
    selected = selected.sort_values(["window_start_ts", "model_ev_per_usd"], ascending=[True, False])
    if max_entries_per_window > 0:
        selected = selected.groupby("slug", group_keys=False).head(int(max_entries_per_window))
    return selected


def _summarize_selection(selected: pd.DataFrame) -> dict[str, Any]:
    if selected.empty:
        return {
            "entries": 0,
            "windows": 0,
            "win_rate": float("nan"),
            "avg_price": float("nan"),
            "avg_model_probability": float("nan"),
            "avg_ev_per_usd": float("nan"),
            "avg_realized_pnl_per_usd": 0.0,
            "total_pnl_per_5usd": 0.0,
        }
    return {
        "entries": int(len(selected)),
        "windows": int(selected["slug"].nunique()),
        "win_rate": round(float(selected["side_won"].mean()), 6),
        "avg_price": round(float(selected["price"].mean()), 6),
        "avg_model_probability": round(float(selected["model_probability"].mean()), 6),
        "avg_ev_per_usd": round(float(selected["model_ev_per_usd"].mean()), 6),
        "avg_realized_pnl_per_usd": round(float(selected["pnl_per_usd"].mean()), 6),
        "total_pnl_per_5usd": round(float(selected["pnl_per_usd"].sum() * 5.0), 4),
    }


def walk_forward_cheap_reversal(
    opportunities: pd.DataFrame,
    config: CheapReversalConfig,
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    if opportunities.empty:
        return pd.DataFrame(), pd.DataFrame(), {}
    windows = np.array(sorted(opportunities["window_start_ts"].dropna().unique()))
    if len(windows) < 3:
        return pd.DataFrame(), pd.DataFrame(), {}
    segments = [segment for segment in np.array_split(windows, max(int(config.folds), 2) + 1) if len(segment)]
    scored_folds: list[pd.DataFrame] = []
    fold_reports: list[dict[str, Any]] = []
    for fold_index in range(1, len(segments)):
        train_windows = np.concatenate(segments[:fold_index])
        test_windows = segments[fold_index]
        train = opportunities[opportunities["window_start_ts"].isin(train_windows)].copy()
        test = opportunities[opportunities["window_start_ts"].isin(test_windows)].copy()
        if len(train) < config.min_train_rows or len(test) < config.min_test_rows:
            continue
        scored = _score_fold(train, test, config)
        scored["fold"] = fold_index
        scored_folds.append(scored)
        fold_reports.append({
            "fold": fold_index,
            "train_rows": int(len(train)),
            "test_rows": int(len(test)),
            "train_windows": int(train["slug"].nunique()),
            "test_windows": int(test["slug"].nunique()),
            **model_metrics(test["side_won"].to_numpy(dtype=float), scored["model_probability"].to_numpy(dtype=float)),
        })
    if not scored_folds:
        return pd.DataFrame(), pd.DataFrame(fold_reports), {}
    scored_all = pd.concat(scored_folds, ignore_index=True, sort=False)
    rule_rows: list[dict[str, Any]] = []
    for min_probability in [0.16, 0.18, 0.20, 0.22, 0.25, 0.30, 0.35, 0.40]:
        for min_ev in [0.0, 0.05, 0.10, 0.20, 0.40]:
            for max_price in [0.20, 0.30, 0.42]:
                for max_entries in [1, 2]:
                    selected = _select_rule_rows(scored_all, min_probability, min_ev, max_price, max_entries)
                    row = {
                        "min_probability": min_probability,
                        "min_ev_per_usd": min_ev,
                        "max_price": max_price,
                        "max_entries_per_window": max_entries,
                    }
                    row.update(_summarize_selection(selected))
                    rule_rows.append(row)
    rule_summary = pd.DataFrame(rule_rows)
    rule_summary = rule_summary.sort_values(
        ["avg_realized_pnl_per_usd", "entries"],
        ascending=[False, False],
    ).reset_index(drop=True)
    report = {
        "folds": fold_reports,
        "scored_rows": int(len(scored_all)),
        "scored_windows": int(scored_all["slug"].nunique()),
        "overall_model": model_metrics(
            scored_all["side_won"].to_numpy(dtype=float),
            scored_all["model_probability"].to_numpy(dtype=float),
        ),
    }
    return scored_all, rule_summary, report


def train_final_model(opportunities: pd.DataFrame, config: CheapReversalConfig) -> dict[str, Any]:
    if opportunities.empty:
        return {}
    y = opportunities["side_won"].to_numpy(dtype=float)
    x, features, medians, scales = prepare_matrix(opportunities)
    weights, bias = train_logistic_regression(
        x,
        y,
        learning_rate=config.learning_rate,
        l2=config.l2,
        iterations=config.iterations,
    )
    proba = sigmoid(x @ weights + bias)
    return {
        "model_type": "logistic_regression_numpy",
        "model_version": "cheap_reversal_v1",
        "target": "contrarian_side_won",
        "features": features,
        "medians": {key: float(value) for key, value in medians.to_dict().items()},
        "scales": {key: float(value) for key, value in scales.to_dict().items()},
        "weights": {feature: float(weight) for feature, weight in zip(features, weights)},
        "bias": float(bias),
        "training_metrics": model_metrics(y, proba),
    }


def run_cheap_reversal_research(config: CheapReversalConfig) -> dict[str, Any]:
    config.output_dir.mkdir(parents=True, exist_ok=True)
    opportunities = build_cheap_reversal_opportunities(config)
    opportunities_path = config.output_dir / "cheap_reversal_opportunities.csv"
    if not opportunities.empty:
        opportunities.to_csv(opportunities_path, index=False)
    scored, rule_summary, walk_report = walk_forward_cheap_reversal(opportunities, config)
    if not scored.empty:
        scored.to_csv(config.output_dir / "cheap_reversal_walk_forward_scored.csv", index=False)
    if not rule_summary.empty:
        rule_summary.to_csv(config.output_dir / "cheap_reversal_rule_summary.csv", index=False)
    model = train_final_model(opportunities, config)
    if model:
        (config.output_dir / "cheap_reversal_model.json").write_text(json.dumps(model, indent=2), encoding="utf-8")
    report = {
        "opportunity_rows": int(len(opportunities)),
        "opportunity_windows": int(opportunities["slug"].nunique()) if not opportunities.empty else 0,
        "start_utc": config.start_utc.isoformat() if config.start_utc is not None else "",
        "end_utc": config.end_utc.isoformat() if config.end_utc is not None else "",
        "filters": {
            "min_price": config.min_price,
            "max_price": config.max_price,
            "min_abs_delta_bps": config.min_abs_delta_bps,
        },
        "walk_forward": walk_report,
        "top_rules": rule_summary.head(20).to_dict(orient="records") if not rule_summary.empty else [],
        "outputs": {
            "opportunities": str(opportunities_path),
            "walk_forward_scored": str(config.output_dir / "cheap_reversal_walk_forward_scored.csv"),
            "rule_summary": str(config.output_dir / "cheap_reversal_rule_summary.csv"),
            "model": str(config.output_dir / "cheap_reversal_model.json"),
        },
    }
    (config.output_dir / "cheap_reversal_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report
