from __future__ import annotations

from pathlib import Path

import pandas as pd

from src.cheap_reversal import CheapReversalConfig, build_cheap_reversal_opportunities, walk_forward_cheap_reversal


def _write(path: Path, text: str) -> None:
    path.write_text(text.strip() + "\n", encoding="utf-8")


def test_build_cheap_reversal_opportunities_keeps_contrarian_side(tmp_path: Path) -> None:
    _write(
        tmp_path / "fair_value_window_labels.csv",
        """
slug,window_start_ts,opening_price,closing_price,price_delta,yes_won,label_source
btc-updown-5m-1000,1000,100,101,1,1,test
""",
    )
    _write(
        tmp_path / "fair_value_signals.csv",
        """
sample_ts_utc,slug,elapsed_s,yes_best_bid,yes_best_ask,yes_best_bid_size,yes_best_ask_size,yes_spread,yes_mid,yes_bid_depth_40_60,yes_ask_depth_40_60,no_best_bid,no_best_ask,no_best_bid_size,no_best_ask_size,no_spread,no_mid,no_bid_depth_40_60,no_ask_depth_40_60,opening_price,latest_price,fair_yes,fair_no,raw_fair_yes,raw_fair_no,market_fair_yes,delta_bps,confidence,continuation_probability,contrarian_probability,kalman_delta_bps,kalman_velocity_bps_per_min,kalman_projected_delta_bps,kalman_residual_bps,kalman_abs_residual_bps,kalman_uncertainty_bps,kalman_trend_agreement
2026-01-01T00:00:05+00:00,btc-updown-5m-1000,5,0.78,0.80,10,10,0.02,0.79,10,10,0.18,0.20,10,10,0.02,0.19,10,10,100,100.2,0.78,0.22,0.78,0.22,0.78,20,0.7,0.8,0.2,0,0,0,0,0,0,0
""",
    )
    config = CheapReversalConfig(data_dir=tmp_path, output_dir=tmp_path / "out", max_price=0.42)
    opportunities = build_cheap_reversal_opportunities(config)

    assert len(opportunities) == 1
    row = opportunities.iloc[0]
    assert row["side"] == "No"
    assert row["side_won"] == 0
    assert row["price"] == 0.20
    assert row["price_bucket"] == "0.20-0.30"


def test_walk_forward_cheap_reversal_scores_rules() -> None:
    rows = []
    for index in range(80):
        won = index % 4 == 0
        rows.append({
            "sample_ts_utc": pd.Timestamp("2026-01-01T00:00:00Z") + pd.Timedelta(minutes=5 * index),
            "slug": f"btc-updown-5m-{1000 + index * 300}",
            "window_start_ts": 1000 + index * 300,
            "side": "No",
            "is_yes": 0,
            "side_won": int(won),
            "price": 0.16,
            "maker_price": 0.15,
            "pnl_per_usd": (1 / 0.16 - 1 - 0.07 * (1 - 0.16)) if won else -(1 + 0.07 * (1 - 0.16)),
            "fee_fraction": 0.07 * (1 - 0.16),
            "elapsed_s": 30 + (index % 4) * 10,
            "remaining_s": 260,
            "elapsed_fraction": 0.1,
            "delta_bps": 12 + (index % 3),
            "abs_delta_bps": 12 + (index % 3),
            "velocity_bps_per_min": 20,
            "fair_probability": 0.25 if won else 0.18,
            "raw_fair_probability": 0.25 if won else 0.18,
            "market_probability": 0.16,
            "confidence": 0.5,
            "continuation_probability": 0.75,
            "contrarian_probability": 0.25,
            "side_spread": 0.02,
            "side_mid": 0.17,
            "side_bid_depth_40_60": 10,
            "side_ask_depth_40_60": 10,
            "elapsed_bucket": "0-60",
            "price_bucket": "0.01-0.20",
            "delta_bucket": "12-20",
            "kalman_delta_bps": 0,
            "kalman_velocity_bps_per_min": 0,
            "kalman_projected_delta_bps": 0,
            "kalman_residual_bps": 0,
            "kalman_abs_residual_bps": 0,
            "kalman_uncertainty_bps": 0,
            "kalman_trend_agreement": 0,
        })
    opportunities = pd.DataFrame(rows)
    config = CheapReversalConfig(
        data_dir=Path("."),
        output_dir=Path("."),
        folds=4,
        min_train_rows=10,
        min_test_rows=5,
        iterations=50,
    )
    scored, rules, report = walk_forward_cheap_reversal(opportunities, config)

    assert not scored.empty
    assert not rules.empty
    assert report["overall_model"]["rows"] == len(scored)
