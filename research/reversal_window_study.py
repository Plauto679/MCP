from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = ROOT_DIR / "data" / "research" / "reversal_study"
WINDOW_MS = 300_000

ELAPSED_BINS = [0, 15, 30, 60, 120, 180, 220, 240, 260, 285, 300]
DELTA_BPS_BINS = [0, 1, 2, 5, 10, 20, 35, 50, 75, 100, 200, float("inf")]


def read_klines(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, low_memory=False)
    required = {"open_time_ms", "open", "high", "low", "close"}
    missing = sorted(required - set(df.columns))
    if missing:
        raise ValueError(f"Missing kline columns in {path}: {missing}")
    for column in ["open_time_ms", "open", "high", "low", "close", "volume", "trade_count"]:
        if column in df.columns:
            df[column] = pd.to_numeric(df[column], errors="coerce")
    df = df.dropna(subset=["open_time_ms", "open", "high", "low", "close"])
    df["open_time_ms"] = df["open_time_ms"].astype("int64")
    return df.sort_values("open_time_ms")


def sign_no_zero(values: pd.Series) -> pd.Series:
    signs = np.sign(values.to_numpy(dtype=float))
    result = pd.Series(signs, index=values.index)
    result = result.replace(0, np.nan).ffill().fillna(0)
    return result


def add_cross_count(group: pd.DataFrame) -> pd.Series:
    signs = sign_no_zero(group["delta_usd"])
    changed = (signs != signs.shift(1)) & (signs != 0) & (signs.shift(1).fillna(0) != 0)
    return changed.cumsum()


def build_samples(
    klines: pd.DataFrame,
    sample_step_seconds: int,
    max_windows: int,
    min_last_elapsed_s: float,
) -> pd.DataFrame:
    df = klines.copy()
    df["window_start_ms"] = (df["open_time_ms"] // WINDOW_MS) * WINDOW_MS
    df["elapsed_s"] = (df["open_time_ms"] - df["window_start_ms"]) / 1000.0
    if sample_step_seconds > 1:
        df = df[(df["elapsed_s"] % sample_step_seconds) == 0].copy()

    frames: list[pd.DataFrame] = []
    for index, (_, group) in enumerate(df.groupby("window_start_ms", sort=True), start=1):
        if max_windows and index > max_windows:
            break
        group = group.sort_values("open_time_ms").copy()
        if group.empty:
            continue
        if float(group["elapsed_s"].max()) < min_last_elapsed_s:
            continue
        opening_price = float(group.iloc[0]["open"])
        final_price = float(group.iloc[-1]["close"])
        final_delta = final_price - opening_price
        if opening_price <= 0 or final_delta == 0:
            continue

        group["opening_price"] = opening_price
        group["final_price"] = final_price
        group["delta_usd"] = group["close"] - opening_price
        group = group[group["delta_usd"] != 0].copy()
        if group.empty:
            continue

        group["delta_bps"] = (group["delta_usd"] / opening_price) * 10000.0
        group["abs_delta_bps"] = group["delta_bps"].abs()
        group["final_delta_usd"] = final_delta
        group["final_delta_bps"] = (final_delta / opening_price) * 10000.0
        group["current_side"] = np.where(group["delta_usd"] > 0, "up", "down")
        group["final_side"] = "up" if final_delta > 0 else "down"
        group["reversal"] = (group["current_side"] != group["final_side"]).astype(int)
        group["continuation"] = 1 - group["reversal"]
        elapsed_minutes = (group["elapsed_s"] / 60.0).replace(0, np.nan)
        group["velocity_bps_per_min"] = group["delta_bps"] / elapsed_minutes
        group["high_so_far"] = group["high"].cummax()
        group["low_so_far"] = group["low"].cummin()
        group["range_so_far_bps"] = ((group["high_so_far"] - group["low_so_far"]) / opening_price) * 10000.0
        group["cross_count_so_far"] = add_cross_count(group)
        group["remaining_s"] = 300.0 - group["elapsed_s"]
        frames.append(group)

    if not frames:
        return pd.DataFrame()
    samples = pd.concat(frames, ignore_index=True)
    samples["window_start_utc"] = pd.to_datetime(samples["window_start_ms"], unit="ms", utc=True)
    samples["sample_ts_utc"] = pd.to_datetime(samples["open_time_ms"], unit="ms", utc=True)
    return samples


def bucket_summary(samples: pd.DataFrame, min_samples: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    df = samples.copy()
    df["elapsed_bucket"] = pd.cut(df["elapsed_s"], bins=ELAPSED_BINS, include_lowest=True, right=False)
    df["abs_delta_bucket_bps"] = pd.cut(df["abs_delta_bps"], bins=DELTA_BPS_BINS, include_lowest=True, right=False)
    group_cols = ["elapsed_bucket", "abs_delta_bucket_bps", "current_side"]
    summary = (
        df.groupby(group_cols, observed=False)
        .agg(
            samples=("reversal", "size"),
            windows=("window_start_ms", "nunique"),
            reversal_rate=("reversal", "mean"),
            continuation_rate=("continuation", "mean"),
            avg_abs_delta_bps=("abs_delta_bps", "mean"),
            median_abs_delta_bps=("abs_delta_bps", "median"),
            avg_range_so_far_bps=("range_so_far_bps", "mean"),
            avg_cross_count=("cross_count_so_far", "mean"),
            avg_elapsed_s=("elapsed_s", "mean"),
        )
        .reset_index()
    )
    summary["edge_vs_50pp"] = (summary["reversal_rate"] - 0.5) * 100.0
    candidates = summary[summary["samples"] >= min_samples].copy()
    candidates["abs_edge_vs_50pp"] = candidates["edge_vs_50pp"].abs()
    candidates = candidates.sort_values(["abs_edge_vs_50pp", "samples"], ascending=[False, False])
    return summary, candidates


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


def write_outputs(samples: pd.DataFrame, output_dir: Path, min_samples: int, write_samples: bool) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    summary, candidates = bucket_summary(samples, min_samples=min_samples)
    summary_path = output_dir / "reversal_summary.csv"
    candidates_path = output_dir / "candidate_zones.csv"
    report_path = output_dir / "reversal_report.json"
    summary.to_csv(summary_path, index=False)
    candidates.to_csv(candidates_path, index=False)
    if write_samples:
        samples.to_csv(output_dir / "reversal_samples.csv", index=False)

    report = {
        "sample_rows": int(len(samples)),
        "windows": int(samples["window_start_ms"].nunique()),
        "first_window_utc": json_safe(samples["window_start_utc"].min()),
        "last_window_utc": json_safe(samples["window_start_utc"].max()),
        "overall_reversal_rate": float(samples["reversal"].mean()),
        "overall_continuation_rate": float(samples["continuation"].mean()),
        "current_up_rate": float((samples["current_side"] == "up").mean()),
        "final_up_rate": float((samples["final_side"] == "up").mean()),
        "summary_path": str(summary_path),
        "candidate_zones_path": str(candidates_path),
    }
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Study BTC 5-minute reversal/continuation behavior from klines.")
    parser.add_argument("--klines", required=True, help="CSV produced by research/binance_klines.py.")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--sample-step-seconds", type=int, default=1)
    parser.add_argument("--max-windows", type=int, default=0)
    parser.add_argument("--min-last-elapsed-s", type=float, default=295.0)
    parser.add_argument("--min-samples", type=int, default=100)
    parser.add_argument("--write-samples", action="store_true")
    args = parser.parse_args()

    klines = read_klines(Path(args.klines))
    samples = build_samples(
        klines=klines,
        sample_step_seconds=max(int(args.sample_step_seconds), 1),
        max_windows=max(int(args.max_windows), 0),
        min_last_elapsed_s=max(float(args.min_last_elapsed_s), 0.0),
    )
    if samples.empty:
        raise RuntimeError("No usable 5-minute windows found in kline file.")

    report = write_outputs(
        samples=samples,
        output_dir=Path(args.output_dir),
        min_samples=max(int(args.min_samples), 1),
        write_samples=bool(args.write_samples),
    )
    print(f"[reversal] samples={report['sample_rows']} windows={report['windows']}")
    print(f"[reversal] reversal_rate={report['overall_reversal_rate']:.4f} continuation_rate={report['overall_continuation_rate']:.4f}")
    print(f"[reversal] wrote {report['summary_path']}")
    print(f"[reversal] wrote {report['candidate_zones_path']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
