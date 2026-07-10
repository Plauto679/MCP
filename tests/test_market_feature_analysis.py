from __future__ import annotations

import csv
import datetime as dt
import gzip
from pathlib import Path

from src.market_feature_analysis import (
    MarketFeatureAnalysisConfig,
    analyze_market_features,
    read_feature_rows,
    write_analysis_outputs,
)


def _row(slug: str, offset_s: int, yes_mid: float, imbalance: float) -> dict[str, object]:
    start_ts = int(slug.rsplit("-", 1)[-1])
    sample_ts = dt.datetime.fromtimestamp(start_ts + offset_s, tz=dt.timezone.utc).isoformat()
    yes_bid = round(max(yes_mid - 0.01, 0.01), 2)
    yes_ask = round(min(yes_mid + 0.01, 0.99), 2)
    no_bid = round(max(1.0 - yes_mid - 0.02, 0.01), 2)
    no_ask = round(min(1.0 - yes_mid + 0.02, 0.99), 2)
    return {
        "sample_ts_utc": sample_ts,
        "slug": slug,
        "window_start_ts": start_ts,
        "elapsed_s": offset_s,
        "yes_best_bid": yes_bid,
        "yes_best_bid_size": 100,
        "yes_best_ask": yes_ask,
        "yes_best_ask_size": 80,
        "yes_spread": round(yes_ask - yes_bid, 2),
        "yes_mid": yes_mid,
        "no_best_bid": no_bid,
        "no_best_bid_size": 90,
        "no_best_ask": no_ask,
        "no_best_ask_size": 70,
        "no_spread": round(no_ask - no_bid, 2),
        "no_mid": round((no_bid + no_ask) / 2.0, 2),
        "directional_top_imbalance": imbalance,
        "directional_depth_imbalance_40_60": imbalance,
        "maker_pair_bid_sum": round(yes_bid + no_bid, 2),
        "maker_pair_edge": round(1.0 - yes_bid - no_bid, 2),
        "taker_pair_ask_sum": round(yes_ask + no_ask, 2),
        "taker_pair_edge": round(1.0 - yes_ask - no_ask, 2),
    }


def _write_features(path: Path, rows: list[dict[str, object]]) -> None:
    fieldnames = list(rows[0])
    with gzip.open(path, "wt", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def test_market_feature_analysis_builds_reports_and_markov_outputs(tmp_path: Path) -> None:
    rows = []
    labels = {}
    for index in range(10):
        start_ts = 1000 + index * 300
        slug = f"btc-updown-5m-{start_ts}"
        yes_won = int(index % 2 == 0)
        labels[slug] = yes_won
        mid = 0.72 if yes_won else 0.28
        imbalance = 1.6 if yes_won else -1.7
        rows.extend([
            _row(slug, 240, mid, imbalance),
            _row(slug, 245, mid + (0.02 if yes_won else -0.02), imbalance),
        ])
    _write_features(tmp_path / "sample.features.csv.gz", rows)
    loaded = read_feature_rows(tmp_path)

    report = analyze_market_features(
        loaded,
        labels=labels,
        config=MarketFeatureAnalysisConfig(
            input_dir=tmp_path,
            output_dir=tmp_path / "out",
            markov_min_train_windows=1,
        ),
    )

    assert report["health"]["rows"] == 20
    assert report["health"]["windows"] == 10
    assert report["paired_summary"][0]["samples"] == 20
    assert report["late_continuation"][0]["accuracy"] == 1.0
    assert report["imbalance_final"][0]["final_direction_accuracy"] == 1.0
    assert report["markov_states"]
    assert report["markov_transitions"]
    assert report["markov_validation"]

    write_analysis_outputs(report, tmp_path / "out")
    assert (tmp_path / "out" / "analysis_report.json").exists()
    assert (tmp_path / "out" / "markov_states.csv").exists()
    assert (tmp_path / "out" / "markov_validation.csv").exists()


def test_market_feature_analysis_skips_active_empty_files(tmp_path: Path) -> None:
    (tmp_path / "active.features.csv.gz").write_bytes(b"")
    loaded = read_feature_rows(tmp_path)

    assert loaded.rows == []
    assert loaded.files_read == 0
    assert loaded.files_skipped == 1
