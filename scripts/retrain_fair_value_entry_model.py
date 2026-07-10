from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import shutil
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research.fair_value_dataset import build_entry_dataset
from scripts.train_fair_value_entry_model import _auc, _build_features, _series_str, _sigmoid, train_logistic
from src.fair_value_entry_model import ENTRY_MODEL_FEATURES, load_entry_model


def _brier(y_true: np.ndarray, score: np.ndarray) -> float:
    return float(np.mean((score - y_true) ** 2)) if len(y_true) else float("nan")


def _log_loss(y_true: np.ndarray, score: np.ndarray) -> float:
    if not len(y_true):
        return float("nan")
    clipped = np.clip(score, 1e-6, 1.0 - 1e-6)
    return float(-np.mean(y_true * np.log(clipped) + (1.0 - y_true) * np.log(1.0 - clipped)))


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        if math.isfinite(number):
            return number
    except (TypeError, ValueError):
        pass
    return default


def _read_csv(path: Path) -> pd.DataFrame:
    if not path.exists() or path.stat().st_size == 0:
        return pd.DataFrame()
    return pd.read_csv(path, low_memory=False)


def _normalise_training_frame(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    result = _series_str(df, "result").str.upper()
    df = df.loc[result.isin(["WIN", "LOSS"])].copy()
    if df.empty:
        return df
    if "entry_ts_utc" not in df.columns:
        if "event_ts_utc_entry" in df.columns:
            df["entry_ts_utc"] = pd.to_datetime(df["event_ts_utc_entry"], utc=True, errors="coerce")
        elif "event_ts_utc" in df.columns:
            df["entry_ts_utc"] = pd.to_datetime(df["event_ts_utc"], utc=True, errors="coerce")
        else:
            df["entry_ts_utc"] = pd.NaT
    else:
        df["entry_ts_utc"] = pd.to_datetime(df["entry_ts_utc"], utc=True, errors="coerce")
    if "direction_won" not in df.columns:
        df["direction_won"] = (result.loc[df.index] == "WIN").astype(int)
    return df


def build_combined_dataset(data_dir: Path, include_existing_training: bool = True) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    if include_existing_training:
        existing = _read_csv(data_dir / "fair_value_training_entries.csv")
        if not existing.empty:
            existing["dataset_source"] = "existing_training_entries"
            frames.append(existing)
    live_entries = build_entry_dataset(data_dir)
    if not live_entries.empty:
        live_entries["dataset_source"] = "live_entries_outcomes"
        frames.append(live_entries)
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames, ignore_index=True, sort=False)
    df = _normalise_training_frame(df)
    if df.empty:
        return df

    key_cols = [
        col
        for col in [
            "event_ts_utc_entry",
            "slug",
            "entry_id",
            "entry_number_in_window_entry",
            "tactic_entry",
            "direction_entry",
            "price",
        ]
        if col in df.columns
    ]
    if key_cols:
        df = df.drop_duplicates(key_cols, keep="last")
    df = df.sort_values(["entry_ts_utc", "slug"], na_position="first").reset_index(drop=True)
    return df


def train_payload(df: pd.DataFrame, source: str, iterations: int, lr: float, l2: float) -> dict[str, Any]:
    df = _normalise_training_frame(df)
    if df.empty:
        raise RuntimeError("No WIN/LOSS rows available for model training.")
    y = (_series_str(df, "result").str.upper() == "WIN").astype(float).to_numpy()
    features = _build_features(df)
    mean = features.mean().to_numpy(dtype="float64")
    scale = features.std(ddof=0).replace(0.0, 1.0).to_numpy(dtype="float64")
    X = (features.to_numpy(dtype="float64") - mean) / scale
    weights, intercept = train_logistic(X, y, iterations=iterations, lr=lr, l2=l2)
    probabilities = _sigmoid(X @ weights + intercept)
    return {
        "model_type": "logistic_regression",
        "trained_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "source": source.replace("\\", "/"),
        "train_rows": int(len(df)),
        "train_win_rate": round(float(y.mean()), 6) if len(y) else 0.0,
        "train_auc": round(_auc(y, probabilities), 6),
        "train_brier": round(_brier(y, probabilities), 6),
        "train_log_loss": round(_log_loss(y, probabilities), 6),
        "feature_names": ENTRY_MODEL_FEATURES,
        "mean": [round(float(item), 10) for item in mean],
        "scale": [round(float(item), 10) if abs(float(item)) > 1e-12 else 1.0 for item in scale],
        "coefficients": [round(float(item), 10) for item in weights],
        "intercept": round(float(intercept), 10),
    }


def score_frame(model: dict[str, Any] | None, df: pd.DataFrame) -> np.ndarray:
    if model is None or df.empty:
        return np.full(len(df), np.nan, dtype="float64")
    features = _build_features(df)
    names = model.get("feature_names") or ENTRY_MODEL_FEATURES
    total = np.full(len(features), _safe_float(model.get("intercept"), 0.0), dtype="float64")
    for idx, name in enumerate(names):
        if name not in features.columns:
            values = np.zeros(len(features), dtype="float64")
        else:
            values = pd.to_numeric(features[name], errors="coerce").fillna(0.0).to_numpy(dtype="float64")
        mean = _safe_float((model.get("mean") or [0.0])[idx], 0.0) if idx < len(model.get("mean") or []) else 0.0
        scale = _safe_float((model.get("scale") or [1.0])[idx], 1.0) if idx < len(model.get("scale") or []) else 1.0
        coef = _safe_float((model.get("coefficients") or [0.0])[idx], 0.0) if idx < len(model.get("coefficients") or []) else 0.0
        if abs(scale) < 1e-12:
            scale = 1.0
        total += coef * ((values - mean) / scale)
    return _sigmoid(total)


def evaluate_scores(name: str, df: pd.DataFrame, scores: np.ndarray) -> dict[str, Any]:
    df = _normalise_training_frame(df)
    mask = np.isfinite(scores)
    if df.empty or not mask.any():
        return {"model": name, "rows": 0}
    y = (_series_str(df, "result").str.upper() == "WIN").astype(float).to_numpy()
    y = y[mask]
    scores = scores[mask]
    return {
        "model": name,
        "rows": int(len(y)),
        "win_rate": round(float(y.mean()), 6),
        "avg_score": round(float(scores.mean()), 6),
        "auc": round(_auc(y, scores), 6),
        "brier": round(_brier(y, scores), 6),
        "log_loss": round(_log_loss(y, scores), 6),
    }


def threshold_analysis(
    df: pd.DataFrame,
    score_map: dict[str, np.ndarray],
    output_path: Path,
    split_ts: pd.Timestamp | None,
) -> pd.DataFrame:
    df = _normalise_training_frame(df).copy()
    if df.empty:
        return pd.DataFrame()
    df["pnl_usd"] = pd.to_numeric(df.get("pnl_usd"), errors="coerce").fillna(0.0)
    df["stake_usd"] = pd.to_numeric(df.get("stake_usd_outcome", df.get("stake_usd_entry")), errors="coerce").fillna(0.0)
    df["is_win"] = (_series_str(df, "result").str.upper() == "WIN").astype(int)
    tactic = _series_str(df, "tactic_entry", "tactic_outcome", "tactic").replace("", "core_edge")
    df["_tactic"] = tactic
    df["_entry_ts"] = pd.to_datetime(df["entry_ts_utc"], utc=True, errors="coerce")

    now = pd.Timestamp.now(tz="UTC")
    periods: dict[str, pd.Series] = {
        "all_entries": pd.Series(True, index=df.index),
        "since_2026_07_01": df["_entry_ts"] >= pd.Timestamp("2026-07-01T00:00:00Z"),
        "last_24h": df["_entry_ts"] >= (now - pd.Timedelta(hours=24)),
    }
    if split_ts is not None:
        periods["holdout"] = df["_entry_ts"] >= split_ts
    thresholds = [0.40, 0.45, 0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80]
    rows: list[dict[str, Any]] = []
    for model_name, scores in score_map.items():
        if len(scores) != len(df):
            continue
        df[f"_score_{model_name}"] = scores
        for period_name, period_mask in periods.items():
            base = df.loc[period_mask.fillna(False)].copy()
            if base.empty:
                continue
            for tactic_name, group in [("all_tactics", base)] + list(base.groupby("_tactic", dropna=False)):
                original_n = len(group)
                original_pnl = float(group["pnl_usd"].sum())
                for threshold in thresholds:
                    kept = group[group[f"_score_{model_name}"] >= threshold]
                    rows.append({
                        "period": period_name,
                        "model": model_name,
                        "tactic": str(tactic_name),
                        "threshold": threshold,
                        "original_entries": int(original_n),
                        "kept_entries": int(len(kept)),
                        "kept_pct": round(float(len(kept) / original_n), 6) if original_n else 0.0,
                        "wins": int(kept["is_win"].sum()) if len(kept) else 0,
                        "losses": int(len(kept) - kept["is_win"].sum()) if len(kept) else 0,
                        "win_rate": round(float(kept["is_win"].mean()), 6) if len(kept) else "",
                        "pnl_usd": round(float(kept["pnl_usd"].sum()), 6) if len(kept) else 0.0,
                        "stake_usd": round(float(kept["stake_usd"].sum()), 6) if len(kept) else 0.0,
                        "roi_on_stake": round(float(kept["pnl_usd"].sum() / kept["stake_usd"].sum()), 6)
                        if len(kept) and kept["stake_usd"].sum() else "",
                        "avg_score": round(float(kept[f"_score_{model_name}"].mean()), 6) if len(kept) else "",
                        "original_pnl_usd": round(original_pnl, 6),
                        "filtered_out_pnl_usd": round(float(original_pnl - kept["pnl_usd"].sum()), 6) if len(kept) else round(original_pnl, 6),
                    })
    result = pd.DataFrame(rows)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    result.to_csv(output_path, index=False)
    return result


def promote_model(candidate_path: Path, output_path: Path, backup_dir: Path) -> str:
    backup_dir.mkdir(parents=True, exist_ok=True)
    if output_path.exists():
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d_%H%M%S")
        backup_path = backup_dir / f"{output_path.stem}.backup_{stamp}{output_path.suffix}"
        shutil.copy2(output_path, backup_path)
    tmp_path = output_path.with_suffix(output_path.suffix + ".tmp")
    shutil.copy2(candidate_path, tmp_path)
    tmp_path.replace(output_path)
    return str(output_path)


def main() -> int:
    parser = argparse.ArgumentParser(description="Retrain and validate Fair Value entry guard model.")
    parser.add_argument("--data-dir", default=str(ROOT / "data"))
    parser.add_argument("--output", default="fair_value_entry_model.json")
    parser.add_argument("--candidate-output", default="fair_value_entry_model_candidate_latest.json")
    parser.add_argument("--combined-output", default="fair_value_training_entries_combined_latest.csv")
    parser.add_argument("--report-output", default="fair_value_entry_model_retrain_report.json")
    parser.add_argument("--threshold-output", default="research/entry_model_threshold_analysis_latest.csv")
    parser.add_argument("--train-fraction", type=float, default=0.75)
    parser.add_argument("--iterations", type=int, default=8000)
    parser.add_argument("--lr", type=float, default=0.08)
    parser.add_argument("--l2", type=float, default=0.02)
    parser.add_argument("--min-holdout-rows", type=int, default=80)
    parser.add_argument("--min-holdout-auc", type=float, default=0.52)
    parser.add_argument("--min-auc-improvement", type=float, default=0.005)
    parser.add_argument("--min-brier-improvement", type=float, default=0.002)
    parser.add_argument("--max-brier-regression", type=float, default=0.002)
    parser.add_argument("--promote", action="store_true")
    parser.add_argument("--force-promote", action="store_true")
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    combined = build_combined_dataset(data_dir)
    if combined.empty:
        raise RuntimeError("No combined entry dataset could be built.")
    combined_path = data_dir / args.combined_output
    combined.to_csv(combined_path, index=False)

    dated = combined.dropna(subset=["entry_ts_utc"]).copy()
    if dated.empty:
        dated = combined.copy()
    dated = dated.sort_values(["entry_ts_utc", "slug"], na_position="first")
    split_index = max(1, min(len(dated) - 1, int(len(dated) * float(args.train_fraction))))
    split_ts = pd.to_datetime(dated.iloc[split_index]["entry_ts_utc"], utc=True, errors="coerce")
    train = dated.iloc[:split_index].copy()
    holdout = dated.iloc[split_index:].copy()

    split_model = train_payload(
        train,
        source=str(combined_path),
        iterations=max(int(args.iterations), 1),
        lr=float(args.lr),
        l2=float(args.l2),
    )
    candidate_holdout_scores = score_frame(split_model, holdout)
    candidate_holdout = evaluate_scores("candidate_temporal_holdout", holdout, candidate_holdout_scores)

    output_path = data_dir / args.output
    current_model = load_entry_model(str(output_path)) if output_path.exists() else None
    current_holdout_scores = score_frame(current_model, holdout)
    current_holdout = evaluate_scores("current_temporal_holdout", holdout, current_holdout_scores)

    final_model = train_payload(
        dated,
        source=str(combined_path),
        iterations=max(int(args.iterations), 1),
        lr=float(args.lr),
        l2=float(args.l2),
    )
    candidate_path = data_dir / args.candidate_output
    candidate_path.write_text(json.dumps(final_model, indent=2), encoding="utf-8")

    full_candidate_scores = score_frame(final_model, dated)
    full_current_scores = score_frame(current_model, dated)
    threshold_path = data_dir / args.threshold_output
    threshold_df = threshold_analysis(
        dated,
        {
            "candidate": full_candidate_scores,
            "current": full_current_scores,
        },
        threshold_path,
        split_ts=split_ts if pd.notna(split_ts) else None,
    )

    holdout_rows = int(candidate_holdout.get("rows", 0) or 0)
    candidate_auc = float(candidate_holdout.get("auc", 0.0) or 0.0)
    candidate_brier = float(candidate_holdout.get("brier", float("inf")) or float("inf"))
    current_auc = float(current_holdout.get("auc", 0.0) or 0.0)
    current_brier = float(current_holdout.get("brier", float("inf")) or float("inf"))

    enough_rows = holdout_rows >= int(args.min_holdout_rows)
    good_auc = candidate_auc >= float(args.min_holdout_auc)
    auc_improved = candidate_auc >= current_auc + float(args.min_auc_improvement)
    brier_improved = candidate_brier <= current_brier - float(args.min_brier_improvement)
    brier_not_regressed = candidate_brier <= current_brier + float(args.max_brier_regression)
    should_promote = bool(
        args.force_promote
        or (
            args.promote
            and enough_rows
            and good_auc
            and brier_not_regressed
            and (auc_improved or brier_improved)
        )
    )

    promoted_path = ""
    if should_promote:
        promoted_path = promote_model(candidate_path, output_path, data_dir / "model_backups")

    report = {
        "event_ts_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "combined_rows": int(len(combined)),
        "dated_rows": int(len(dated)),
        "train_rows": int(len(train)),
        "holdout_rows": int(len(holdout)),
        "split_ts_utc": split_ts.isoformat() if pd.notna(split_ts) else "",
        "candidate_final_train": {
            key: final_model.get(key)
            for key in ["train_rows", "train_win_rate", "train_auc", "train_brier", "train_log_loss"]
        },
        "candidate_holdout": candidate_holdout,
        "current_holdout": current_holdout,
        "promotion_requested": bool(args.promote),
        "force_promote": bool(args.force_promote),
        "promotion_checks": {
            "enough_rows": enough_rows,
            "good_auc": good_auc,
            "auc_improved": auc_improved,
            "brier_improved": brier_improved,
            "brier_not_regressed": brier_not_regressed,
            "min_holdout_rows": int(args.min_holdout_rows),
            "min_holdout_auc": float(args.min_holdout_auc),
            "min_auc_improvement": float(args.min_auc_improvement),
            "min_brier_improvement": float(args.min_brier_improvement),
            "max_brier_regression": float(args.max_brier_regression),
        },
        "promoted": should_promote,
        "promoted_path": promoted_path,
        "outputs": {
            "combined_dataset": str(combined_path),
            "candidate_model": str(candidate_path),
            "active_model": str(output_path),
            "threshold_analysis": str(threshold_path),
            "report": str(data_dir / args.report_output),
        },
    }
    report_path = data_dir / args.report_output
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print(
        "[retrain] "
        f"combined={len(combined)} train={len(train)} holdout={len(holdout)} "
        f"candidate_auc={candidate_auc:.4f} current_auc={current_auc:.4f} "
        f"candidate_brier={candidate_brier:.4f} current_brier={current_brier:.4f} "
        f"promoted={should_promote}"
    )
    if not threshold_df.empty:
        core = threshold_df[
            (threshold_df["period"] == "since_2026_07_01")
            & (threshold_df["model"] == "candidate")
            & (threshold_df["tactic"] == "core_edge")
        ].sort_values(["pnl_usd", "kept_entries"], ascending=[False, False])
        if not core.empty:
            cols = ["threshold", "kept_entries", "win_rate", "pnl_usd", "filtered_out_pnl_usd", "original_pnl_usd"]
            print("[retrain] best candidate core filters since 2026-07-01")
            print(core[cols].head(5).to_string(index=False))
    print(f"[retrain] wrote {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
