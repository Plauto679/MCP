from __future__ import annotations

import argparse
import csv
import sys
import time
from pathlib import Path
from typing import Any


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from research.fair_value_dataset import fetch_binance_5m_label
from src.market_feature_analysis import (
    MarketFeatureAnalysisConfig,
    analyze_market_features,
    load_labels,
    read_feature_rows,
    window_start_ts,
    write_analysis_outputs,
    write_labels,
)


def read_label_rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    with path.open("r", newline="", encoding="utf-8") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def merge_label_rows(existing: list[dict[str, Any]], fetched: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_slug: dict[str, dict[str, Any]] = {}
    for row in existing:
        slug = str(row.get("slug") or "")
        if slug:
            by_slug[slug] = row
    for row in fetched:
        slug = str(row.get("slug") or "")
        if slug:
            by_slug[slug] = row
    return sorted(by_slug.values(), key=lambda row: int(float(row.get("window_start_ts") or 0)))


def fetch_missing_labels(
    slugs: set[str],
    labels_csv: Path,
    sleep_seconds: float,
    settle_lag_seconds: float,
) -> list[dict[str, Any]]:
    existing_rows = read_label_rows(labels_csv)
    existing_labels = load_labels(labels_csv)
    now_ts = time.time()
    fetched: list[dict[str, Any]] = []
    missing = sorted(slug for slug in slugs if slug not in existing_labels)
    for index, slug in enumerate(missing, start=1):
        start_ts = window_start_ts(slug)
        if start_ts is None:
            continue
        if start_ts + 300.0 + settle_lag_seconds > now_ts:
            continue
        try:
            label = fetch_binance_5m_label(start_ts)
        except Exception as exc:
            print(f"[market_features] Binance label failed for {slug}: {type(exc).__name__}: {str(exc)[:140]}")
            label = None
        if label:
            fetched.append(label)
        if sleep_seconds > 0 and index < len(missing):
            time.sleep(sleep_seconds)
    if fetched:
        write_labels(labels_csv, merge_label_rows(existing_rows, fetched))
    return fetched


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Analyze lightweight Polymarket BTC 5m market feature captures."
    )
    parser.add_argument("--input-dir", default=str(ROOT_DIR / "data" / "market_features"))
    parser.add_argument("--output-dir", default=str(ROOT_DIR / "data" / "market_features_analysis"))
    parser.add_argument(
        "--labels-csv",
        default="",
        help="Optional labels CSV. Defaults to <output-dir>/binance_5m_labels.csv when fetching labels.",
    )
    parser.add_argument("--fetch-binance-labels", action="store_true")
    parser.add_argument("--label-sleep-seconds", type=float, default=0.03)
    parser.add_argument(
        "--settle-lag-seconds",
        type=float,
        default=20.0,
        help="Only fetch labels for windows closed at least this long ago.",
    )
    parser.add_argument("--markov-horizon-s", type=float, default=5.0)
    parser.add_argument("--markov-sample-step-s", type=float, default=5.0)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    input_dir = Path(args.input_dir)
    output_dir = Path(args.output_dir)
    labels_csv = Path(args.labels_csv) if args.labels_csv else output_dir / "binance_5m_labels.csv"

    loaded = read_feature_rows(input_dir)
    slugs = {str(row.get("slug") or "") for row in loaded.rows if row.get("slug")}
    fetched_count = 0
    if args.fetch_binance_labels:
        fetched_count = len(
            fetch_missing_labels(
                slugs=slugs,
                labels_csv=labels_csv,
                sleep_seconds=max(float(args.label_sleep_seconds), 0.0),
                settle_lag_seconds=max(float(args.settle_lag_seconds), 0.0),
            )
        )

    labels = load_labels(labels_csv)
    config = MarketFeatureAnalysisConfig(
        input_dir=input_dir,
        output_dir=output_dir,
        labels_csv=labels_csv,
        markov_horizon_s=float(args.markov_horizon_s),
        markov_sample_step_s=float(args.markov_sample_step_s),
    )
    report = analyze_market_features(loaded, labels, config)
    write_analysis_outputs(report, output_dir)

    health = report["health"]
    print(
        "[market_features] "
        f"rows={health['rows']} windows={health['windows']} labels={health['labeled_windows']} "
        f"files_read={health['feature_files_read']} files_skipped={health['feature_files_skipped']} "
        f"fetched_labels={fetched_count} output={output_dir}"
    )


if __name__ == "__main__":
    main()
