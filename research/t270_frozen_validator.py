from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research.analyze_market_features import fetch_missing_labels  # noqa: E402
from src.late_continuation_execution import (  # noqa: E402
    LateContinuationExecutionConfig,
    build_late_continuation_candidates,
    run_late_continuation_execution_scanner,
)
from src.market_feature_analysis import (  # noqa: E402
    MarketFeatureAnalysisConfig,
    analyze_market_features,
    load_labels,
    read_feature_rows,
    write_analysis_outputs,
)


FROZEN_ENTRY_ELAPSED_S = (270.0,)
FROZEN_TAKER_DELAYS_S = (0.0, 1.0, 2.0, 3.0, 5.0)
FROZEN_MAX_PRICES = (0.94, 0.95)
FROZEN_MIN_MID_EDGES = (0.0, 0.05)
FROZEN_MIN_CONFIRM_IMBALANCES = (0.0,)
FROZEN_MAX_SPREADS = (0.01, 0.02, 0.03, 0.05)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Frozen t270 late-continuation validator. No auth, no orders."
    )
    parser.add_argument("--feature-dir", default=str(ROOT / "data" / "market_features"))
    parser.add_argument("--output-dir", default=str(ROOT / "data" / "t270_frozen_validation"))
    parser.add_argument("--labels-csv", default="")
    parser.add_argument(
        "--seed-labels-csv",
        default=str(ROOT / "data" / "market_features_analysis_current_20260707_0827" / "binance_5m_labels.csv"),
    )
    parser.add_argument("--fetch-binance-labels", action="store_true")
    parser.add_argument("--label-sleep-seconds", type=float, default=0.0)
    parser.add_argument("--settle-lag-seconds", type=float, default=20.0)
    parser.add_argument("--min-top-shares", type=float, default=0.0)
    parser.add_argument("--require-uncrossed-books", action="store_true")
    parser.add_argument(
        "--max-quote-age-ms",
        type=float,
        default=0.0,
        help="Optional max selected-side quote age at decision and execution. 0 disables.",
    )
    parser.add_argument("--iterations", type=int, default=1)
    parser.add_argument("--interval-seconds", type=float, default=900.0)
    parser.add_argument(
        "--holdout-start-ts",
        type=int,
        default=0,
        help="Optional first window_start_ts for true forward holdout validation.",
    )
    return parser.parse_args()


def run_once(args: argparse.Namespace) -> dict[str, Any]:
    output_dir = Path(args.output_dir)
    analysis_dir = output_dir / "analysis"
    execution_dir = output_dir / "execution"
    labels_csv = Path(args.labels_csv) if args.labels_csv else output_dir / "binance_5m_labels.csv"
    baseline_labels_csv = output_dir / "baseline_labels.csv"
    _seed_labels(labels_csv, baseline_labels_csv, Path(args.seed_labels_csv))

    loaded = read_feature_rows(Path(args.feature_dir))
    slugs = {str(row.get("slug") or "") for row in loaded.rows if row.get("slug")}
    fetched_count = 0
    if bool(args.fetch_binance_labels):
        fetched_count = len(
            fetch_missing_labels(
                slugs=slugs,
                labels_csv=labels_csv,
                sleep_seconds=max(float(args.label_sleep_seconds), 0.0),
                settle_lag_seconds=max(float(args.settle_lag_seconds), 0.0),
            )
        )

    labels = load_labels(labels_csv)
    baseline_labels = load_labels(baseline_labels_csv)
    analysis_report = analyze_market_features(
        loaded,
        labels,
        MarketFeatureAnalysisConfig(
            input_dir=Path(args.feature_dir),
            output_dir=analysis_dir,
            labels_csv=labels_csv,
        ),
    )
    write_analysis_outputs(analysis_report, analysis_dir)

    execution_config = LateContinuationExecutionConfig(
        feature_dir=Path(args.feature_dir),
        labels_csv=labels_csv,
        output_dir=execution_dir,
        entry_elapsed_s=FROZEN_ENTRY_ELAPSED_S,
        taker_delays_s=FROZEN_TAKER_DELAYS_S,
        max_prices=FROZEN_MAX_PRICES,
        min_mid_edges=FROZEN_MIN_MID_EDGES,
        min_confirm_imbalances=FROZEN_MIN_CONFIRM_IMBALANCES,
        max_spreads=FROZEN_MAX_SPREADS,
        min_top_shares=max(float(args.min_top_shares), 0.0),
        require_uncrossed_books=bool(args.require_uncrossed_books),
        max_quote_age_ms=max(float(args.max_quote_age_ms), 0.0),
        min_train_attempts=50,
        min_test_attempts=20,
        min_train_windows=40,
    )
    execution_report = run_late_continuation_execution_scanner(
        execution_config
    )
    holdout_report = write_holdout_outputs(
        execution_config,
        output_dir,
        labels,
        baseline_labels,
        holdout_start_ts=int(args.holdout_start_ts),
    )

    report = {
        "run_ts_utc": _utc_iso(),
        "mode": "dry_research_only",
        "frozen_family": {
            "entry_elapsed_s": FROZEN_ENTRY_ELAPSED_S,
            "taker_delays_s": FROZEN_TAKER_DELAYS_S,
            "max_prices": FROZEN_MAX_PRICES,
            "min_mid_edges": FROZEN_MIN_MID_EDGES,
            "min_confirm_imbalances": FROZEN_MIN_CONFIRM_IMBALANCES,
            "max_spreads": FROZEN_MAX_SPREADS,
        },
        "fill_model": {
            "min_top_shares": max(float(args.min_top_shares), 0.0),
            "require_uncrossed_books": bool(args.require_uncrossed_books),
            "max_quote_age_ms": max(float(args.max_quote_age_ms), 0.0),
        },
        "features": analysis_report["health"],
        "fetched_labels": fetched_count,
        "execution": execution_report,
        "holdout": holdout_report,
        "outputs": {
            "analysis": str(analysis_dir),
            "execution": str(execution_dir),
            "labels_csv": str(labels_csv),
            "baseline_labels_csv": str(baseline_labels_csv),
            "holdout_summary": str(output_dir / "holdout_summary.csv"),
            "report": str(output_dir / "report.json"),
            "run_log": str(output_dir / "run_log.jsonl"),
        },
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    with (output_dir / "run_log.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(report, separators=(",", ":"), ensure_ascii=True) + "\n")
    return report


def write_holdout_outputs(
    execution_config: LateContinuationExecutionConfig,
    output_dir: Path,
    labels: dict[str, int],
    baseline_labels: dict[str, int],
    holdout_start_ts: int,
) -> dict[str, Any]:
    holdout_slugs = {
        slug
        for slug in labels
        if slug not in baseline_labels and (holdout_start_ts <= 0 or _window_start_ts(slug) >= holdout_start_ts)
    }
    candidates = build_late_continuation_candidates(execution_config)
    if candidates.empty or not holdout_slugs:
        _write_csv(output_dir / "holdout_summary.csv", [])
        return {
            "holdout_start_ts": holdout_start_ts,
            "baseline_labeled_windows": len(baseline_labels),
            "holdout_labeled_windows": len(holdout_slugs),
            "candidate_rows": 0,
            "positive_scenarios": 0,
            "top": [],
        }

    holdout = candidates[candidates["slug"].isin(holdout_slugs)].copy()
    if holdout.empty:
        _write_csv(output_dir / "holdout_summary.csv", [])
        return {
            "holdout_start_ts": holdout_start_ts,
            "baseline_labeled_windows": len(baseline_labels),
            "holdout_labeled_windows": len(holdout_slugs),
            "candidate_rows": 0,
            "positive_scenarios": 0,
            "top": [],
        }

    for column in ("executed", "side_won", "execution_price", "pnl_per_attempt_usd"):
        holdout[column] = pd.to_numeric(holdout[column], errors="coerce")
    rows: list[dict[str, Any]] = []
    group_columns = [
        "scenario_id",
        "entry_elapsed_s",
        "delay_s",
        "max_price",
        "min_mid_edge",
        "min_confirm_imbalance",
        "max_spread",
    ]
    for key, group in holdout.groupby(group_columns, dropna=False):
        executed = group[group["executed"].astype(bool)]
        key_values = dict(zip(group_columns, key))
        rows.append({
            **key_values,
            "attempts": int(len(group)),
            "windows": int(group["slug"].nunique()),
            "executed": int(group["executed"].sum()),
            "execution_rate": round(float(group["executed"].mean()), 6) if len(group) else 0.0,
            "accuracy_attempts": round(float(group["side_won"].mean()), 6) if len(group) else "",
            "accuracy_executed": round(float(executed["side_won"].mean()), 6) if not executed.empty else "",
            "avg_execution_price": round(float(executed["execution_price"].mean()), 6) if not executed.empty else "",
            "avg_pnl_per_attempt_usd": round(float(group["pnl_per_attempt_usd"].mean()), 8),
            "total_pnl_one_usd_attempts": round(float(group["pnl_per_attempt_usd"].sum()), 8),
            "first_slug": str(sorted(group["slug"].unique(), key=_window_start_ts)[0]),
            "last_slug": str(sorted(group["slug"].unique(), key=_window_start_ts)[-1]),
        })
    rows.sort(
        key=lambda row: (
            float(row["avg_pnl_per_attempt_usd"]),
            int(row["attempts"]),
            float(row["execution_rate"]),
        ),
        reverse=True,
    )
    _write_csv(output_dir / "holdout_summary.csv", rows)
    positive = [row for row in rows if float(row["avg_pnl_per_attempt_usd"]) > 0.0]
    return {
        "holdout_start_ts": holdout_start_ts,
        "baseline_labeled_windows": len(baseline_labels),
        "holdout_labeled_windows": len(holdout_slugs),
        "candidate_rows": int(len(holdout)),
        "positive_scenarios": len(positive),
        "top": rows[:20],
    }


def _seed_labels(labels_csv: Path, baseline_labels_csv: Path, seed_path: Path) -> None:
    if seed_path.exists() and not baseline_labels_csv.exists():
        baseline_labels_csv.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(seed_path, baseline_labels_csv)
    if labels_csv.exists() or not seed_path.exists():
        return
    labels_csv.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(seed_path, labels_csv)


def _utc_iso() -> str:
    import datetime as dt

    return dt.datetime.now(dt.timezone.utc).isoformat()


def _window_start_ts(slug: str) -> int:
    try:
        return int(str(slug).rsplit("-", 1)[-1])
    except (TypeError, ValueError):
        return 0


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fieldnames: list[str] = []
    for row in rows:
        for key in row:
            if key not in fieldnames:
                fieldnames.append(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        import csv

        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    args = parse_args()
    iterations = max(int(args.iterations), 1)
    for iteration in range(iterations):
        print(json.dumps(run_once(args), indent=2), flush=True)
        if iteration + 1 >= iterations:
            break
        time.sleep(max(float(args.interval_seconds), 1.0))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
