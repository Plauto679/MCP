from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.train_fair_value_entry_model import _auc, _sigmoid, train_logistic
from src.kalman_features import add_kalman_features_to_frame


WINDOW_MS = 300_000
KALMAN_COLUMNS = [
    "kalman_delta_bps",
    "kalman_velocity_bps_per_min",
    "kalman_projected_delta_bps",
    "kalman_residual_bps",
    "kalman_abs_residual_bps",
    "kalman_uncertainty_bps",
    "kalman_trend_agreement",
]
BASE_FEATURES = [
    "elapsed_s",
    "remaining_s",
    "delta_bps",
    "abs_delta_bps",
    "velocity_bps_per_min",
    "range_so_far_bps",
    "cross_count_so_far",
]


def _normalise_time_ms(series: pd.Series) -> pd.Series:
    values = pd.to_numeric(series, errors="coerce")
    values = values.where(values < 10_000_000_000_000_000, values / 1_000_000.0)
    values = values.where(values < 10_000_000_000_000, values / 1_000.0)
    return values.round().astype("Int64")


def read_klines(path: Path, start: str = "", end: str = "") -> pd.DataFrame:
    usecols = ["open_time", "open_time_ms", "open", "high", "low", "close", "close_time"]
    probe = pd.read_csv(path, nrows=1)
    available = [col for col in usecols if col in probe.columns]
    df = pd.read_csv(path, usecols=available, low_memory=False)
    if "open_time_ms" not in df.columns:
        df["open_time_ms"] = df["open_time"]
    df["open_time_ms"] = _normalise_time_ms(df["open_time_ms"])
    for column in ["open", "high", "low", "close"]:
        df[column] = pd.to_numeric(df[column], errors="coerce")
    df = df.dropna(subset=["open_time_ms", "open", "high", "low", "close"]).copy()
    df["open_time_ms"] = df["open_time_ms"].astype("int64")
    df = df.sort_values("open_time_ms")
    if start:
        start_ms = int(pd.Timestamp(start, tz="UTC").timestamp() * 1000)
        df = df[df["open_time_ms"] >= start_ms]
    if end:
        end_ms = int(pd.Timestamp(end, tz="UTC").timestamp() * 1000)
        df = df[df["open_time_ms"] < end_ms]
    return df.reset_index(drop=True)


def _sign_no_zero(values: pd.Series) -> pd.Series:
    signs = np.sign(values.to_numpy(dtype=float))
    result = pd.Series(signs, index=values.index)
    return result.replace(0, np.nan).ffill().fillna(0)


def _cross_count(group: pd.DataFrame) -> pd.Series:
    signs = _sign_no_zero(group["delta_usd"])
    changed = (signs != signs.shift(1)) & (signs != 0) & (signs.shift(1).fillna(0) != 0)
    return changed.cumsum()


def build_nonleaky_samples(klines: pd.DataFrame) -> pd.DataFrame:
    df = klines.copy()
    df["window_start_ms"] = (df["open_time_ms"] // WINDOW_MS) * WINDOW_MS
    df["elapsed_s"] = (df["open_time_ms"] - df["window_start_ms"]) / 1000.0
    grouped = df.groupby("window_start_ms", sort=False)
    df["rows_in_window"] = grouped["open"].transform("size")
    df["min_elapsed_s"] = grouped["elapsed_s"].transform("min")
    df["max_elapsed_s"] = grouped["elapsed_s"].transform("max")
    df["opening_price"] = grouped["open"].transform("first")
    df["final_price"] = grouped["close"].transform("last")
    valid = (
        (df["rows_in_window"] >= 5)
        & (df["min_elapsed_s"] <= 0.0)
        & (df["max_elapsed_s"] >= 240.0)
        & (df["opening_price"] > 0.0)
        & (df["final_price"] > 0.0)
        & ((df["final_price"] - df["opening_price"]).abs() > 1e-12)
    )
    df = df.loc[valid].copy()
    if df.empty:
        return pd.DataFrame()

    df["latest_price"] = df["open"]
    df["delta_usd"] = df["latest_price"] - df["opening_price"]
    df["delta_bps"] = (df["delta_usd"] / df["opening_price"]) * 10000.0
    df["abs_delta_bps"] = df["delta_bps"].abs()
    df["price_delta"] = df["final_price"] - df["opening_price"]
    df["final_delta_bps"] = (df["price_delta"] / df["opening_price"]) * 10000.0
    df["current_side"] = np.where(df["delta_usd"] >= 0.0, "up", "down")
    df["final_side"] = np.where(df["price_delta"] > 0.0, "up", "down")
    df["reversal"] = (df["current_side"] != df["final_side"]).astype(int)
    df["continuation"] = 1 - df["reversal"]
    elapsed_minutes = (df["elapsed_s"] / 60.0).replace(0.0, np.nan)
    df["velocity_bps_per_min"] = (df["delta_bps"] / elapsed_minutes).fillna(0.0)

    grouped = df.groupby("window_start_ms", sort=False)
    known_high = grouped["high"].cummax().groupby(df["window_start_ms"]).shift(1)
    known_low = grouped["low"].cummin().groupby(df["window_start_ms"]).shift(1)
    df["range_so_far_bps"] = ((known_high - known_low) / df["opening_price"] * 10000.0).fillna(0.0)
    signs = np.sign(df["delta_usd"].to_numpy(dtype=float))
    sign_series = pd.Series(signs, index=df.index).replace(0, np.nan)
    sign_series = sign_series.groupby(df["window_start_ms"]).ffill().fillna(0.0)
    prev_sign = sign_series.groupby(df["window_start_ms"]).shift(1).fillna(0.0)
    changed = (sign_series != prev_sign) & (sign_series != 0.0) & (prev_sign != 0.0)
    df["cross_count_so_far"] = changed.groupby(df["window_start_ms"]).cumsum()
    df["remaining_s"] = 300.0 - df["elapsed_s"]
    df["window_start_utc"] = pd.to_datetime(df["window_start_ms"], unit="ms", utc=True)
    df["sample_ts_utc"] = pd.to_datetime(df["open_time_ms"], unit="ms", utc=True)
    df["slug"] = "btc-updown-5m-" + (df["window_start_ms"] // 1000).astype(str)
    samples = df[(df["elapsed_s"] >= 60.0) & (df["elapsed_s"] <= 240.0) & (df["abs_delta_bps"] > 0)].copy()
    if samples.empty:
        return pd.DataFrame()
    samples = add_kalman_features_to_frame(samples)
    samples["test_month"] = samples["sample_ts_utc"].dt.to_period("M").astype(str)
    return samples


def _brier(y_true: np.ndarray, score: np.ndarray) -> float:
    return float(np.mean((score - y_true) ** 2)) if len(y_true) else float("nan")


def _fit_predict(train: pd.DataFrame, test: pd.DataFrame, label: str, features: list[str], max_train_rows: int) -> np.ndarray:
    if max_train_rows > 0 and len(train) > max_train_rows:
        train = train.tail(max_train_rows).copy()
    x_train_raw = train[features].apply(pd.to_numeric, errors="coerce").fillna(0.0)
    x_test_raw = test[features].apply(pd.to_numeric, errors="coerce").fillna(0.0)
    mean = x_train_raw.mean()
    scale = x_train_raw.std(ddof=0).replace(0.0, 1.0)
    x_train = ((x_train_raw - mean) / scale).to_numpy(dtype="float64")
    x_test = ((x_test_raw - mean) / scale).to_numpy(dtype="float64")
    y_train = train[label].astype(float).to_numpy()
    weights, intercept = train_logistic(x_train, y_train, iterations=260, lr=0.06, l2=0.03)
    return _sigmoid(x_test @ weights + intercept)


def _pnl_for_price(wins: pd.Series, stake: float, price: float, fee_per_usd: float) -> pd.Series:
    fee = stake * fee_per_usd
    return pd.Series(np.where(wins.astype(bool), stake / price - stake - fee, -stake - fee), index=wins.index)


def _select_strategy_entries(
    test: pd.DataFrame,
    probability_col: str,
    tactic: str,
    threshold: float,
    max_entries_per_window: int,
    min_abs_delta_bps: float = 0.0,
) -> pd.DataFrame:
    df = test.copy()
    if tactic == "core_reversal":
        mask = (
            (df["elapsed_s"] >= 60.0)
            & (df["elapsed_s"] <= 240.0)
            & (df[probability_col] >= threshold)
            & (df["abs_delta_bps"] >= min_abs_delta_bps)
        )
        win_col = "reversal"
    elif tactic == "momentum":
        mask = (
            (df["elapsed_s"] >= 180.0)
            & (df["elapsed_s"] <= 240.0)
            & (df[probability_col] >= threshold)
            & (df["abs_delta_bps"] >= min_abs_delta_bps)
        )
        win_col = "continuation"
    else:
        mask = (
            (df["elapsed_s"] >= 240.0)
            & (df[probability_col] >= threshold)
            & (df["abs_delta_bps"] >= min_abs_delta_bps)
        )
        win_col = "continuation"
    entries = df.loc[mask].sort_values(["window_start_ms", "elapsed_s"]).copy()
    if entries.empty:
        return entries
    entries["selected_win"] = entries[win_col].astype(int)
    entries["predicted_probability"] = entries[probability_col]
    return entries.groupby("window_start_ms", as_index=False, group_keys=False).head(max_entries_per_window)


def _summarise_entries(entries: pd.DataFrame, tactic: str, model: str, threshold: float, price_grid: list[float], stake: float, fee_per_usd: float) -> dict[str, Any]:
    count = int(len(entries))
    wins = int(entries["selected_win"].sum()) if count else 0
    result: dict[str, Any] = {
        "tactic": tactic,
        "model": model,
        "threshold": threshold,
        "entries": count,
        "windows": int(entries["window_start_ms"].nunique()) if count else 0,
        "wins": wins,
        "losses": count - wins,
        "winrate": wins / count if count else float("nan"),
        "avg_predicted_probability": float(entries["predicted_probability"].mean()) if count else float("nan"),
        "avg_elapsed_s": float(entries["elapsed_s"].mean()) if count else float("nan"),
        "avg_abs_delta_bps": float(entries["abs_delta_bps"].mean()) if count else float("nan"),
    }
    for price in price_grid:
        pnl = _pnl_for_price(entries["selected_win"], stake=stake, price=price, fee_per_usd=fee_per_usd) if count else pd.Series(dtype=float)
        result[f"pnl_price_{price:.2f}"] = float(pnl.sum()) if count else 0.0
        result[f"roi_price_{price:.2f}"] = float(pnl.sum() / (count * stake)) if count else float("nan")
    return result


def walk_forward(
    samples: pd.DataFrame,
    min_train_windows: int,
    stake: float,
    fee_per_usd: float,
    fold_months: int,
    max_train_rows: int,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    months = sorted(samples["test_month"].dropna().unique())
    metric_rows: list[dict[str, Any]] = []
    strategy_rows: list[dict[str, Any]] = []
    feature_sets = {
        "base": BASE_FEATURES,
        "base_kalman": BASE_FEATURES + KALMAN_COLUMNS,
    }

    fold_months = max(int(fold_months), 1)
    for start_idx in range(0, len(months), fold_months):
        test_months = months[start_idx:start_idx + fold_months]
        if not test_months:
            continue
        month = f"{test_months[0]}..{test_months[-1]}" if len(test_months) > 1 else test_months[0]
        test = samples[samples["test_month"].isin(test_months)].copy()
        train = samples[samples["sample_ts_utc"] < test["sample_ts_utc"].min()].copy()
        if train["window_start_ms"].nunique() < min_train_windows or test.empty:
            continue

        scored = test.copy()
        for model_name, features in feature_sets.items():
            reversal_score = _fit_predict(train, test, "reversal", features, max_train_rows=max_train_rows)
            continuation_score = _fit_predict(train, test, "continuation", features, max_train_rows=max_train_rows)
            scored[f"{model_name}_reversal_p"] = reversal_score
            scored[f"{model_name}_continuation_p"] = continuation_score
            for label, score in (("reversal", reversal_score), ("continuation", continuation_score)):
                y = test[label].astype(float).to_numpy()
                metric_rows.append({
                    "month": month,
                    "model": model_name,
                    "label": label,
                    "rows": int(len(test)),
                    "windows": int(test["window_start_ms"].nunique()),
                    "auc": _auc(y, score),
                    "brier": _brier(y, score),
                    "base_rate": float(y.mean()) if len(y) else float("nan"),
                })

        grids = [
            ("core_reversal", "reversal_p", [0.38, 0.40, 0.42, 0.45, 0.50], 2, 0.0, [0.35, 0.40, 0.45, 0.50]),
            ("momentum", "continuation_p", [0.58, 0.60, 0.65, 0.70, 0.75], 2, 2.0, [0.55, 0.65, 0.75, 0.85]),
            ("late", "continuation_p", [0.86, 0.88, 0.90, 0.92, 0.95], 1, 6.0, [0.80, 0.90, 0.95]),
        ]
        for tactic, suffix, thresholds, max_entries, min_delta, prices in grids:
            for model_name in feature_sets:
                probability_col = f"{model_name}_{suffix}"
                for threshold in thresholds:
                    entries = _select_strategy_entries(
                        scored,
                        probability_col=probability_col,
                        tactic=tactic,
                        threshold=threshold,
                        max_entries_per_window=max_entries,
                        min_abs_delta_bps=min_delta,
                    )
                    row = _summarise_entries(entries, tactic, model_name, threshold, prices, stake, fee_per_usd)
                    row["month"] = month
                    strategy_rows.append(row)

    return pd.DataFrame(metric_rows), pd.DataFrame(strategy_rows)


def aggregate_strategy(strategy: pd.DataFrame) -> pd.DataFrame:
    if strategy.empty:
        return strategy
    group_cols = ["tactic", "model", "threshold"]
    numeric = [col for col in strategy.columns if col.startswith("pnl_")]
    weighted_rows = []
    for key, group in strategy.groupby(group_cols, sort=False):
        entries = int(group["entries"].sum())
        wins = int(group["wins"].sum())
        row = {
            "tactic": key[0],
            "model": key[1],
            "threshold": key[2],
            "months": int(group["month"].nunique()),
            "entries": entries,
            "windows": int(group["windows"].sum()),
            "wins": wins,
            "losses": entries - wins,
            "winrate": wins / entries if entries else float("nan"),
            "avg_predicted_probability": float(np.average(group["avg_predicted_probability"].fillna(0), weights=group["entries"].clip(lower=1))) if entries else float("nan"),
            "avg_elapsed_s": float(np.average(group["avg_elapsed_s"].fillna(0), weights=group["entries"].clip(lower=1))) if entries else float("nan"),
            "avg_abs_delta_bps": float(np.average(group["avg_abs_delta_bps"].fillna(0), weights=group["entries"].clip(lower=1))) if entries else float("nan"),
        }
        for col in numeric:
            row[col] = float(group[col].sum())
        weighted_rows.append(row)
    return pd.DataFrame(weighted_rows).sort_values(["tactic", "winrate", "entries"], ascending=[True, False, False])


def dry_run_summary(data_dir: Path) -> pd.DataFrame:
    entries_path = data_dir / "fair_value_entries.csv"
    outcomes_path = data_dir / "fair_value_outcomes.csv"
    if not entries_path.exists() or not outcomes_path.exists():
        return pd.DataFrame()
    entries = pd.read_csv(entries_path, low_memory=False)
    outcomes = pd.read_csv(outcomes_path, low_memory=False)
    if entries.empty or outcomes.empty:
        return pd.DataFrame()
    outcomes = outcomes.drop_duplicates(["slug", "entry_id"], keep="last")
    df = entries.merge(outcomes, on=["slug", "entry_id"], how="left", suffixes=("_entry", "_outcome"))
    df = df[df["result"].isin(["WIN", "LOSS"])].copy()
    if df.empty:
        return pd.DataFrame()
    df["event_ts_utc_entry"] = pd.to_datetime(df["event_ts_utc_entry"], utc=True, errors="coerce")
    df = df[df["event_ts_utc_entry"] >= pd.Timestamp("2026-07-01", tz="UTC")].copy()
    for col in ["pnl_usd", "stake_usd_entry", "price", "entry_model_win_probability_entry"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    group = (
        df.groupby(["dry_run_entry", "tactic_entry"], dropna=False)
        .agg(
            entries=("entry_id", "size"),
            wins=("result", lambda s: int((s == "WIN").sum())),
            losses=("result", lambda s: int((s == "LOSS").sum())),
            pnl_usd=("pnl_usd", "sum"),
            avg_price=("price", "mean"),
            avg_model_p=("entry_model_win_probability_entry", "mean"),
            avg_stake=("stake_usd_entry", "mean"),
        )
        .reset_index()
    )
    group["winrate"] = group["wins"] / group["entries"]
    return group


def main() -> int:
    parser = argparse.ArgumentParser(description="Walk-forward Fair Value/Kalman study on BTC 5m windows.")
    parser.add_argument("--klines", default=str(ROOT / "data" / "btcusdt_1m_binance_2024_2026.csv.gz"))
    parser.add_argument("--data-dir", default=str(ROOT / "data"))
    parser.add_argument("--output-dir", default=str(ROOT / "data" / "research" / "fair_value_walkforward_kalman_latest"))
    parser.add_argument("--start", default="2025-07-01")
    parser.add_argument("--end", default="")
    parser.add_argument("--min-train-windows", type=int, default=10_000)
    parser.add_argument("--fold-months", type=int, default=3)
    parser.add_argument("--max-train-rows", type=int, default=120_000)
    parser.add_argument("--stake", type=float, default=5.0)
    parser.add_argument("--fee-per-usd", type=float, default=0.04)
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    klines = read_klines(Path(args.klines), start=args.start, end=args.end)
    samples = build_nonleaky_samples(klines)
    if samples.empty:
        raise RuntimeError("No usable samples built.")
    metrics, strategy = walk_forward(
        samples=samples,
        min_train_windows=max(int(args.min_train_windows), 1),
        stake=float(args.stake),
        fee_per_usd=float(args.fee_per_usd),
        fold_months=max(int(args.fold_months), 1),
        max_train_rows=max(int(args.max_train_rows), 0),
    )
    strategy_agg = aggregate_strategy(strategy)
    dry = dry_run_summary(Path(args.data_dir))

    samples.tail(200_000).to_csv(output_dir / "samples_tail.csv", index=False)
    metrics.to_csv(output_dir / "walkforward_metrics.csv", index=False)
    strategy.to_csv(output_dir / "strategy_by_month.csv", index=False)
    strategy_agg.to_csv(output_dir / "strategy_summary.csv", index=False)
    dry.to_csv(output_dir / "dry_run_comparison.csv", index=False)

    report = {
        "rows": int(len(samples)),
        "windows": int(samples["window_start_ms"].nunique()),
        "first_sample": samples["sample_ts_utc"].min().isoformat(),
        "last_sample": samples["sample_ts_utc"].max().isoformat(),
        "metrics_rows": int(len(metrics)),
        "strategy_rows": int(len(strategy_agg)),
        "output_dir": str(output_dir),
    }
    (output_dir / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")

    print(f"[walkforward] samples={report['rows']} windows={report['windows']}")
    print(f"[walkforward] range={report['first_sample']} -> {report['last_sample']}")
    if not metrics.empty:
        metric_summary = metrics.groupby(["model", "label"]).agg(auc=("auc", "mean"), brier=("brier", "mean"), rows=("rows", "sum")).reset_index()
        print(metric_summary.to_string(index=False))
    if not strategy_agg.empty:
        print("[walkforward] top strategy rows:")
        sort_cols = [col for col in strategy_agg.columns if col.startswith("pnl_price_")]
        top = strategy_agg.sort_values(sort_cols[0] if sort_cols else "entries", ascending=False).head(12)
        print(top.to_string(index=False))
    print(f"[walkforward] wrote {output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
