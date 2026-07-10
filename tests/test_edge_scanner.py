from __future__ import annotations

from pathlib import Path

import pandas as pd

from src.edge_scanner import ScannerConfig, build_candidate_dataset, summarize_candidates, walk_forward_bucket_scan


def _write(path: Path, text: str) -> None:
    path.write_text(text.strip() + "\n", encoding="utf-8")


def test_edge_scanner_builds_candidates_and_net_pnl(tmp_path: Path) -> None:
    data_dir = tmp_path
    _write(
        data_dir / "fair_value_window_labels.csv",
        """
slug,window_start_ts,opening_price,closing_price,price_delta,yes_won,label_source
btc-updown-5m-1000,1000,100,101,1,1,test
""",
    )
    _write(
        data_dir / "fair_value_signals.csv",
        """
sample_ts_utc,slug,elapsed_s,yes_best_bid,yes_best_ask,yes_best_bid_size,yes_best_ask_size,yes_spread,yes_mid,yes_bid_depth_40_60,yes_ask_depth_40_60,no_best_bid,no_best_ask,no_best_bid_size,no_best_ask_size,no_spread,no_mid,no_bid_depth_40_60,no_ask_depth_40_60,opening_price,latest_price,fair_yes,fair_no,raw_fair_yes,raw_fair_no,market_fair_yes,delta_bps,confidence,continuation_probability,contrarian_probability,kalman_delta_bps,kalman_velocity_bps_per_min,kalman_projected_delta_bps,kalman_residual_bps,kalman_abs_residual_bps,kalman_uncertainty_bps,kalman_trend_agreement
2026-01-01T00:04:10+00:00,btc-updown-5m-1000,250,0.59,0.60,10,10,0.01,0.595,10,10,0.39,0.40,10,10,0.01,0.395,10,10,100,100.6,0.72,0.28,0.72,0.28,0.70,60,0.8,0.9,0.1,0,0,0,0,0,0,0
""",
    )
    candidates = build_candidate_dataset(ScannerConfig(data_dir=data_dir, output_dir=tmp_path / "out"))

    assert len(candidates) == 4
    yes_taker = candidates[(candidates["side"] == "Yes") & (candidates["route"] == "taker")].iloc[0]
    no_taker = candidates[(candidates["side"] == "No") & (candidates["route"] == "taker")].iloc[0]

    assert yes_taker["archetype"] == "late_continuation"
    assert yes_taker["side_won"] == 1
    assert yes_taker["realized_pnl_per_usd"] > 0.6
    assert no_taker["side_won"] == 0
    assert no_taker["realized_pnl_per_usd"] < -1.0


def test_edge_scanner_summarizes_positive_buckets() -> None:
    rows = []
    for index in range(100):
        rows.append({
            "slug": f"btc-updown-5m-{1000 + index}",
            "side_won": 1 if index < 70 else 0,
            "realized_pnl_per_usd": 0.6 if index < 70 else -1.04,
            "break_even_probability": 0.62,
            "price": 0.60,
            "model_edge": 0.05,
            "market_edge": 0.02,
            "confidence": 0.5,
            "abs_delta_bps": 6.0,
            "elapsed_s": 250.0,
            "spread": 0.01,
            "bid_depth_40_60": 10.0,
            "ask_depth_40_60": 10.0,
            "archetype": "late_continuation",
            "route": "taker",
            "relation": "continuation",
            "elapsed_bucket": "240-260",
            "price_bucket": "0.55-0.60",
            "delta_bucket": "6-8",
        })
    candidates = pd.DataFrame(rows)
    summaries = summarize_candidates(candidates, min_samples=80, min_windows=20)

    assert not summaries["top_bucket"].empty
    top = summaries["top_bucket"].iloc[0]
    assert top["archetype"] == "late_continuation"
    assert top["avg_pnl_per_usd"] > 0


def test_walk_forward_bucket_scan_selects_train_positive_bucket() -> None:
    rows = []
    for index in range(240):
        win = index % 4 != 0
        rows.append({
            "window_start_ts": index,
            "slug": f"btc-updown-5m-{index}",
            "side_won": 1 if win else 0,
            "realized_pnl_per_usd": 0.7 if win else -1.04,
            "break_even_probability": 0.62,
            "price": 0.58,
            "model_edge": 0.08,
            "market_edge": 0.04,
            "confidence": 0.7,
            "abs_delta_bps": 8.0,
            "elapsed_s": 260.0,
            "spread": 0.01,
            "bid_depth_40_60": 10.0,
            "ask_depth_40_60": 10.0,
            "archetype": "momentum",
            "route": "taker",
            "relation": "continuation",
            "elapsed_bucket": "260-280",
            "price_bucket": "0.55-0.60",
            "delta_bucket": "8-12",
        })
    scan = walk_forward_bucket_scan(
        pd.DataFrame(rows),
        min_train_samples=20,
        min_train_windows=20,
        folds=4,
    )

    assert not scan.empty
    assert scan["selected_buckets"].max() >= 1
    assert scan["test_samples"].sum() > 0
