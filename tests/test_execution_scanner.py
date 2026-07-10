from __future__ import annotations

from pathlib import Path

import pandas as pd

from src.execution_scanner import ExecutionScannerConfig, build_execution_dataset, summarize_execution


def _write(path: Path, text: str) -> None:
    path.write_text(text.strip() + "\n", encoding="utf-8")


def _write_fixture(data_dir: Path) -> None:
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
2026-01-01T00:00:00+00:00,btc-updown-5m-1000,0,0.40,0.42,10,10,0.02,0.41,10,10,0.58,0.60,10,10,0.02,0.59,10,10,100,100.0,0.55,0.45,0.55,0.45,0.50,0,0.8,0.5,0.5,0,0,0,0,0,0,0
2026-01-01T00:00:01+00:00,btc-updown-5m-1000,1,0.40,0.41,10,10,0.01,0.405,10,10,0.59,0.60,10,10,0.01,0.595,10,10,100,100.1,0.55,0.45,0.55,0.45,0.50,1,0.8,0.5,0.5,0,0,0,0,0,0,0
2026-01-01T00:00:02+00:00,btc-updown-5m-1000,2,0.40,0.40,10,10,0.00,0.40,10,10,0.60,0.61,10,10,0.01,0.605,10,10,100,100.2,0.55,0.45,0.55,0.45,0.50,2,0.8,0.5,0.5,0,0,0,0,0,0,0
""",
    )


def test_execution_scanner_models_taker_delay_and_maker_fill(tmp_path: Path) -> None:
    _write_fixture(tmp_path)
    config = ExecutionScannerConfig(
        data_dir=tmp_path,
        output_dir=tmp_path / "out",
        taker_delays=(0.0, 2.0),
        maker_waits=(1.0, 2.0),
        min_bucket_attempts=1,
        min_bucket_windows=1,
    )
    dataset = build_execution_dataset(config)

    assert set(dataset["side"]) == {"Yes", "No"}
    first_yes = dataset[(dataset["side"] == "Yes") & (dataset["elapsed_s"] == 0.0)].iloc[0]
    first_no = dataset[(dataset["side"] == "No") & (dataset["elapsed_s"] == 0.0)].iloc[0]

    assert first_yes["taker_delay_0_pnl"] > 1.0
    assert first_no["taker_delay_0_pnl"] < -1.0
    assert not bool(first_yes["maker_wait_1_filled"])
    assert bool(first_yes["maker_wait_2_filled"])
    assert first_yes["maker_wait_2_attempt_pnl"] > first_yes["taker_delay_0_pnl"]


def test_execution_summary_reports_fill_rate_and_attempt_pnl(tmp_path: Path) -> None:
    _write_fixture(tmp_path)
    config = ExecutionScannerConfig(
        data_dir=tmp_path,
        output_dir=tmp_path / "out",
        taker_delays=(0.0,),
        maker_waits=(2.0,),
        min_bucket_attempts=1,
        min_bucket_windows=1,
    )
    dataset = build_execution_dataset(config)
    summaries = summarize_execution(dataset, config)

    maker_rows = summaries["archetype"][summaries["archetype"]["scenario"] == "maker_only_2"]
    assert not maker_rows.empty
    assert maker_rows["fill_rate"].between(0, 1).all()
    assert "avg_pnl_per_attempt_usd" in maker_rows.columns
    assert isinstance(summaries["top_bucket"], pd.DataFrame)
