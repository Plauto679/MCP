from __future__ import annotations

import csv
import datetime as dt
import gzip
from pathlib import Path

from src.late_continuation_execution import (
    LateContinuationExecutionConfig,
    build_late_continuation_candidates,
    run_late_continuation_execution_scanner,
    summarize_late_continuation_execution,
)


def _feature_row(slug: str, elapsed_s: int, yes_mid: float) -> dict[str, object]:
    start_ts = int(slug.rsplit("-", 1)[-1])
    sample_ts = dt.datetime.fromtimestamp(start_ts + elapsed_s, tz=dt.timezone.utc).isoformat()
    yes_ask = round(min(yes_mid + 0.01, 0.99), 2)
    yes_bid = round(max(yes_mid - 0.01, 0.01), 2)
    no_mid = 1.0 - yes_mid
    no_ask = round(min(no_mid + 0.01, 0.99), 2)
    no_bid = round(max(no_mid - 0.01, 0.01), 2)
    return {
        "sample_ts_utc": sample_ts,
        "slug": slug,
        "window_start_ts": start_ts,
        "elapsed_s": elapsed_s,
        "yes_best_bid": yes_bid,
        "yes_best_ask": yes_ask,
        "yes_best_ask_size": 10,
        "yes_spread": round(yes_ask - yes_bid, 2),
        "yes_mid": yes_mid,
        "yes_quote_age_ms": 100,
        "no_best_bid": no_bid,
        "no_best_ask": no_ask,
        "no_best_ask_size": 10,
        "no_spread": round(no_ask - no_bid, 2),
        "no_mid": no_mid,
        "no_quote_age_ms": 100,
        "directional_top_imbalance": 1.6 if yes_mid >= 0.5 else -1.6,
    }


def _write_features(path: Path, rows: list[dict[str, object]]) -> None:
    fieldnames = list(rows[0])
    with gzip.open(path, "wt", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _write_labels(path: Path, labels: dict[str, int]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["slug", "window_start_ts", "yes_won"])
        writer.writeheader()
        for slug, yes_won in labels.items():
            writer.writerow({"slug": slug, "window_start_ts": slug.rsplit("-", 1)[-1], "yes_won": yes_won})


def test_late_continuation_scanner_models_limit_delay_and_split(tmp_path: Path) -> None:
    rows = []
    labels = {}
    for index in range(8):
        slug = f"btc-updown-5m-{1000 + index * 300}"
        yes_won = int(index % 2 == 0)
        labels[slug] = yes_won
        base_mid = 0.70 if yes_won else 0.30
        rows.extend([
            _feature_row(slug, 240, base_mid),
            _feature_row(slug, 241, base_mid + (0.02 if yes_won else -0.02)),
            _feature_row(slug, 245, base_mid + (0.03 if yes_won else -0.03)),
        ])
    _write_features(tmp_path / "features.features.csv.gz", rows)
    labels_csv = tmp_path / "labels.csv"
    _write_labels(labels_csv, labels)

    config = LateContinuationExecutionConfig(
        feature_dir=tmp_path,
        labels_csv=labels_csv,
        output_dir=tmp_path / "out",
        entry_elapsed_s=(240.0,),
        taker_delays_s=(0.0, 1.0, 5.0),
        max_prices=(0.75,),
        min_mid_edges=(0.10,),
        min_confirm_imbalances=(1.0,),
        max_spreads=(0.03,),
        min_train_attempts=1,
        min_train_windows=1,
        min_test_attempts=1,
    )
    candidates = build_late_continuation_candidates(config)
    summaries = summarize_late_continuation_execution(candidates, config)

    assert not candidates.empty
    assert set(candidates["split"]) == {"train", "test"}
    assert candidates["executed"].eq(1).all()
    assert candidates["pnl_per_attempt_usd"].mean() > 0
    assert not summaries["selected_train"].empty


def test_late_continuation_scanner_writes_outputs(tmp_path: Path) -> None:
    slug = "btc-updown-5m-1000"
    _write_features(
        tmp_path / "features.features.csv.gz",
        [_feature_row(slug, 240, 0.70), _feature_row(slug, 241, 0.71)],
    )
    labels_csv = tmp_path / "labels.csv"
    _write_labels(labels_csv, {slug: 1})

    report = run_late_continuation_execution_scanner(
        LateContinuationExecutionConfig(
            feature_dir=tmp_path,
            labels_csv=labels_csv,
            output_dir=tmp_path / "out",
            entry_elapsed_s=(240.0,),
            taker_delays_s=(0.0,),
            max_prices=(0.75,),
            min_mid_edges=(0.10,),
            min_confirm_imbalances=(0.0,),
            max_spreads=(0.03,),
            min_train_attempts=1,
            min_test_attempts=1,
            min_train_windows=1,
        )
    )

    assert report["candidate_rows"] == 1
    assert report["fill_model"]["min_top_shares"] == 0.0
    assert (tmp_path / "out" / "split_summary.csv").exists()
    assert (tmp_path / "out" / "report.json").exists()


def test_late_continuation_strict_fill_filters_size_quote_age_and_crossed_books(tmp_path: Path) -> None:
    good_slug = "btc-updown-5m-1000"
    tiny_slug = "btc-updown-5m-1300"
    stale_slug = "btc-updown-5m-1600"
    crossed_slug = "btc-updown-5m-1900"

    rows = [
        _feature_row(good_slug, 240, 0.70),
        _feature_row(good_slug, 241, 0.71),
        {**_feature_row(tiny_slug, 240, 0.70), "yes_best_ask_size": 2},
        {**_feature_row(tiny_slug, 241, 0.71), "yes_best_ask_size": 2},
        {**_feature_row(stale_slug, 240, 0.70), "yes_quote_age_ms": 5000},
        {**_feature_row(stale_slug, 241, 0.71), "yes_quote_age_ms": 5000},
        {**_feature_row(crossed_slug, 240, 0.70), "yes_best_bid": 0.75, "yes_best_ask": 0.71},
        {**_feature_row(crossed_slug, 241, 0.71), "yes_best_bid": 0.76, "yes_best_ask": 0.72},
    ]
    _write_features(tmp_path / "features.features.csv.gz", rows)
    labels_csv = tmp_path / "labels.csv"
    _write_labels(labels_csv, {good_slug: 1, tiny_slug: 1, stale_slug: 1, crossed_slug: 1})

    candidates = build_late_continuation_candidates(
        LateContinuationExecutionConfig(
            feature_dir=tmp_path,
            labels_csv=labels_csv,
            output_dir=tmp_path / "out",
            entry_elapsed_s=(240.0,),
            taker_delays_s=(0.0,),
            max_prices=(0.75,),
            min_mid_edges=(0.10,),
            min_confirm_imbalances=(0.0,),
            max_spreads=(0.05,),
            min_top_shares=5.0,
            require_uncrossed_books=True,
            max_quote_age_ms=1000.0,
        )
    )

    assert set(candidates["slug"]) == {good_slug}
