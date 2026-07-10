from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from reversal_window_study import (
    DELTA_BPS_BINS,
    ELAPSED_BINS,
    build_samples,
    read_klines,
)


ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = ROOT_DIR / "data" / "research" / "rule_simulator"


TIME_BANDS = {
    "all": (0.0, 300.0),
    "early_0_60": (0.0, 60.0),
    "first_half_0_150": (0.0, 150.0),
    "mid_60_180": (60.0, 180.0),
    "mid_late_120_240": (120.0, 240.0),
    "late_180_260": (180.0, 260.0),
    "momentum_220_300": (220.0, 300.0),
    "final_260_300": (260.0, 300.0),
    "no_final_0_260": (0.0, 260.0),
}

DELTA_BANDS = {
    "any": (0.0, float("inf")),
    "tiny_0_2bps": (0.0, 2.0),
    "small_2_5bps": (2.0, 5.0),
    "medium_5_10bps": (5.0, 10.0),
    "large_10_20bps": (10.0, 20.0),
    "huge_20plus_bps": (20.0, float("inf")),
}

DEFAULT_MIN_PROBS = [0.50, 0.52, 0.55, 0.58, 0.60, 0.65, 0.70, 0.75]
DEFAULT_PRICE_ASSUMPTIONS = [0.40, 0.45, 0.50, 0.55, 0.60, 0.65]


@dataclass(frozen=True)
class StrategyRule:
    side_model: str
    time_band: str
    delta_band: str
    min_probability: float

    @property
    def key(self) -> str:
        return f"{self.side_model}|{self.time_band}|{self.delta_band}|p>={self.min_probability:.2f}"


def assign_buckets(samples: pd.DataFrame) -> pd.DataFrame:
    df = samples.copy()
    df["elapsed_bucket"] = pd.cut(df["elapsed_s"], bins=ELAPSED_BINS, include_lowest=True, right=False)
    df["abs_delta_bucket_bps"] = pd.cut(df["abs_delta_bps"], bins=DELTA_BPS_BINS, include_lowest=True, right=False)
    return df


def temporal_split(samples: pd.DataFrame, train_fraction: float) -> tuple[pd.DataFrame, pd.DataFrame, int]:
    windows = np.array(sorted(samples["window_start_ms"].dropna().unique()))
    if len(windows) < 3:
        raise RuntimeError("Need at least 3 windows for temporal train/test split.")
    split_index = max(1, min(len(windows) - 1, int(len(windows) * train_fraction)))
    split_window = int(windows[split_index])
    train = samples[samples["window_start_ms"] < split_window].copy()
    test = samples[samples["window_start_ms"] >= split_window].copy()
    if train.empty or test.empty:
        raise RuntimeError("Temporal split produced empty train or test set.")
    return train, test, split_window


def probability_table(train: pd.DataFrame) -> pd.DataFrame:
    group_cols = ["elapsed_bucket", "abs_delta_bucket_bps", "current_side"]
    table = (
        train.groupby(group_cols, observed=False)
        .agg(
            bucket_samples=("reversal", "size"),
            bucket_windows=("window_start_ms", "nunique"),
            reversal_probability=("reversal", "mean"),
            continuation_probability=("continuation", "mean"),
            avg_abs_delta_bps=("abs_delta_bps", "mean"),
            avg_cross_count=("cross_count_so_far", "mean"),
        )
        .reset_index()
    )
    return table


def attach_predictions(test: pd.DataFrame, table: pd.DataFrame) -> pd.DataFrame:
    keys = ["elapsed_bucket", "abs_delta_bucket_bps", "current_side"]
    merged = test.merge(table, on=keys, how="left", suffixes=("", "_train"))
    merged["reversal_probability"] = pd.to_numeric(merged["reversal_probability"], errors="coerce")
    merged["continuation_probability"] = pd.to_numeric(merged["continuation_probability"], errors="coerce")
    merged["bucket_samples"] = pd.to_numeric(merged["bucket_samples"], errors="coerce").fillna(0)
    return merged


def generate_rules(min_probs: list[float]) -> list[StrategyRule]:
    rules: list[StrategyRule] = []
    for side_model in ["reversal", "continuation"]:
        for time_band in TIME_BANDS:
            for delta_band in DELTA_BANDS:
                for min_probability in min_probs:
                    rules.append(StrategyRule(
                        side_model=side_model,
                        time_band=time_band,
                        delta_band=delta_band,
                        min_probability=min_probability,
                    ))
    return rules


def assumed_price_pnl(win: pd.Series, stake: float, entry_price: float, fee_per_usd: float) -> pd.Series:
    fee = stake * fee_per_usd
    win_pnl = (stake / entry_price) - stake - fee
    loss_pnl = -stake - fee
    return pd.Series(np.where(win, win_pnl, loss_pnl), index=win.index)


def select_entries(
    predicted: pd.DataFrame,
    rule: StrategyRule,
    min_bucket_samples: int,
    max_entries_per_window: int,
) -> pd.DataFrame:
    elapsed_min, elapsed_max = TIME_BANDS[rule.time_band]
    delta_min, delta_max = DELTA_BANDS[rule.delta_band]
    probability_column = f"{rule.side_model}_probability"

    mask = (
        (predicted["elapsed_s"] >= elapsed_min)
        & (predicted["elapsed_s"] < elapsed_max)
        & (predicted["abs_delta_bps"] >= delta_min)
        & (predicted["abs_delta_bps"] < delta_max)
        & (predicted["bucket_samples"] >= min_bucket_samples)
        & (predicted[probability_column] >= rule.min_probability)
    )
    candidates = predicted.loc[mask].copy()
    if candidates.empty:
        return candidates
    candidates["predicted_probability"] = candidates[probability_column]
    if rule.side_model == "reversal":
        candidates["selected_side"] = np.where(candidates["current_side"] == "up", "No", "Yes")
        candidates["selected_win"] = candidates["reversal"].astype(int)
    else:
        candidates["selected_side"] = np.where(candidates["current_side"] == "up", "Yes", "No")
        candidates["selected_win"] = candidates["continuation"].astype(int)
    candidates = candidates.sort_values(["window_start_ms", "elapsed_s"])
    if max_entries_per_window <= 1:
        return candidates.groupby("window_start_ms", as_index=False, group_keys=False).head(1)
    return candidates.groupby("window_start_ms", as_index=False, group_keys=False).head(max_entries_per_window)


def summarize_rule(
    entries: pd.DataFrame,
    rule: StrategyRule,
    stake: float,
    fee_per_usd: float,
    price_assumptions: list[float],
) -> dict[str, Any]:
    wins = int(entries["selected_win"].sum())
    count = int(len(entries))
    winrate = wins / count if count else float("nan")
    summary: dict[str, Any] = {
        "rule_key": rule.key,
        "side_model": rule.side_model,
        "time_band": rule.time_band,
        "delta_band": rule.delta_band,
        "min_probability": rule.min_probability,
        "entries": count,
        "windows": int(entries["window_start_ms"].nunique()) if count else 0,
        "wins": wins,
        "losses": count - wins,
        "winrate": winrate,
        "avg_predicted_probability": float(entries["predicted_probability"].mean()) if count else float("nan"),
        "median_predicted_probability": float(entries["predicted_probability"].median()) if count else float("nan"),
        "avg_elapsed_s": float(entries["elapsed_s"].mean()) if count else float("nan"),
        "median_elapsed_s": float(entries["elapsed_s"].median()) if count else float("nan"),
        "avg_abs_delta_bps": float(entries["abs_delta_bps"].mean()) if count else float("nan"),
        "avg_bucket_samples": float(entries["bucket_samples"].mean()) if count else float("nan"),
        "break_even_price_no_fee": winrate,
        "break_even_price_with_fee": winrate / (1.0 + fee_per_usd) if count else float("nan"),
    }
    total_stake = count * stake
    for price in price_assumptions:
        pnl = assumed_price_pnl(entries["selected_win"].astype(bool), stake=stake, entry_price=price, fee_per_usd=fee_per_usd)
        summary[f"pnl_at_{price:.2f}"] = float(pnl.sum())
        summary[f"roi_at_{price:.2f}"] = float(pnl.sum() / total_stake) if total_stake else float("nan")
    return summary


def run_grid(
    predicted: pd.DataFrame,
    min_probs: list[float],
    min_bucket_samples: int,
    max_entries_per_window: int,
    min_entries: int,
    stake: float,
    fee_per_usd: float,
    price_assumptions: list[float],
) -> tuple[pd.DataFrame, pd.DataFrame]:
    predicted = predicted.copy()
    rules = generate_rules(min_probs)
    summaries: list[dict[str, Any]] = []
    trade_frames: list[pd.DataFrame] = []

    for rule in rules:
        entries = select_entries(
            predicted=predicted,
            rule=rule,
            min_bucket_samples=min_bucket_samples,
            max_entries_per_window=max_entries_per_window,
        )
        if len(entries) < min_entries:
            continue
        summary = summarize_rule(
            entries=entries,
            rule=rule,
            stake=stake,
            fee_per_usd=fee_per_usd,
            price_assumptions=price_assumptions,
        )
        summaries.append(summary)
        top_marker = entries.copy()
        top_marker["rule_key"] = rule.key
        top_marker["side_model"] = rule.side_model
        trade_frames.append(top_marker)

    summary_df = pd.DataFrame(summaries)
    if summary_df.empty:
        return summary_df, pd.DataFrame()
    sort_column = "break_even_price_with_fee"
    summary_df = summary_df.sort_values([sort_column, "entries"], ascending=[False, False])
    trades_df = pd.concat(trade_frames, ignore_index=True) if trade_frames else pd.DataFrame()
    return summary_df, trades_df


def json_safe(value: Any) -> Any:
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return float(value)
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if pd.isna(value):
        return None
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description="Offline BTC 5m rule simulator using historical reversal/continuation probabilities.")
    parser.add_argument("--klines", required=True, help="CSV produced by research/binance_klines.py.")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--train-fraction", type=float, default=0.70)
    parser.add_argument("--sample-step-seconds", type=int, default=1)
    parser.add_argument("--max-windows", type=int, default=0)
    parser.add_argument("--min-last-elapsed-s", type=float, default=295.0)
    parser.add_argument("--min-bucket-samples", type=int, default=200)
    parser.add_argument("--min-entries", type=int, default=20)
    parser.add_argument("--max-entries-per-window", type=int, default=1)
    parser.add_argument("--stake", type=float, default=5.0)
    parser.add_argument("--fee-per-usd", type=float, default=0.0)
    parser.add_argument("--min-probs", default=",".join(str(value) for value in DEFAULT_MIN_PROBS))
    parser.add_argument("--price-assumptions", default=",".join(str(value) for value in DEFAULT_PRICE_ASSUMPTIONS))
    parser.add_argument("--write-trades", action="store_true")
    args = parser.parse_args()

    min_probs = [float(value.strip()) for value in args.min_probs.split(",") if value.strip()]
    price_assumptions = [float(value.strip()) for value in args.price_assumptions.split(",") if value.strip()]

    klines = read_klines(Path(args.klines))
    samples = build_samples(
        klines=klines,
        sample_step_seconds=max(int(args.sample_step_seconds), 1),
        max_windows=max(int(args.max_windows), 0),
        min_last_elapsed_s=max(float(args.min_last_elapsed_s), 0.0),
    )
    if samples.empty:
        raise RuntimeError("No usable samples from kline file.")
    samples = assign_buckets(samples)
    train, test, split_window = temporal_split(samples, train_fraction=float(args.train_fraction))
    table = probability_table(train)
    predicted = attach_predictions(test, table)
    predicted["side_model"] = ""

    summary, trades = run_grid(
        predicted=predicted,
        min_probs=min_probs,
        min_bucket_samples=max(int(args.min_bucket_samples), 1),
        max_entries_per_window=max(int(args.max_entries_per_window), 1),
        min_entries=max(int(args.min_entries), 1),
        stake=max(float(args.stake), 0.01),
        fee_per_usd=max(float(args.fee_per_usd), 0.0),
        price_assumptions=price_assumptions,
    )
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    summary_path = output_dir / "strategy_simulation_summary.csv"
    table_path = output_dir / "probability_table.csv"
    report_path = output_dir / "strategy_simulation_report.json"
    table.to_csv(table_path, index=False)
    summary.to_csv(summary_path, index=False)

    trades_path = ""
    if args.write_trades and not trades.empty:
        keep_cols = [
            "rule_key",
            "side_model",
            "window_start_utc",
            "sample_ts_utc",
            "elapsed_s",
            "current_side",
            "selected_side",
            "selected_win",
            "predicted_probability",
            "abs_delta_bps",
            "delta_bps",
            "final_delta_bps",
            "cross_count_so_far",
            "range_so_far_bps",
        ]
        keep_cols = [column for column in keep_cols if column in trades.columns]
        trades_path = str(output_dir / "strategy_simulation_trades.csv")
        trades[keep_cols].to_csv(trades_path, index=False)

    report = {
        "sample_rows": int(len(samples)),
        "train_rows": int(len(train)),
        "test_rows": int(len(test)),
        "windows": int(samples["window_start_ms"].nunique()),
        "train_windows": int(train["window_start_ms"].nunique()),
        "test_windows": int(test["window_start_ms"].nunique()),
        "split_window_start_utc": json_safe(pd.to_datetime(split_window, unit="ms", utc=True)),
        "min_bucket_samples": int(args.min_bucket_samples),
        "min_entries": int(args.min_entries),
        "max_entries_per_window": int(args.max_entries_per_window),
        "stake": float(args.stake),
        "fee_per_usd": float(args.fee_per_usd),
        "summary_rows": int(len(summary)),
        "summary_path": str(summary_path),
        "probability_table_path": str(table_path),
        "trades_path": trades_path,
    }
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print(f"[sim] samples={report['sample_rows']} windows={report['windows']} train_windows={report['train_windows']} test_windows={report['test_windows']}")
    print(f"[sim] strategy_rows={len(summary)} -> {summary_path}")
    if not summary.empty:
        display_cols = [
            "rule_key",
            "entries",
            "winrate",
            "break_even_price_with_fee",
            "avg_predicted_probability",
            "avg_elapsed_s",
            "avg_abs_delta_bps",
        ]
        price_col = f"roi_at_{price_assumptions[0]:.2f}" if price_assumptions else ""
        if price_col in summary.columns:
            display_cols.append(price_col)
        print(summary[display_cols].head(12).to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
