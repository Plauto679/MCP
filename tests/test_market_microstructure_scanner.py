from __future__ import annotations

from pathlib import Path
import datetime as dt

import pandas as pd

from src.market_microstructure_scanner import MicrostructureScannerConfig, scan_microstructure
from src.market_ws_recorder import MarketTokens, SQLiteMarketWriter


def test_microstructure_scanner_segments_paired_candidates_and_scores_imbalance(tmp_path: Path) -> None:
    rows = []
    base = pd.Timestamp("2026-01-01T00:00:00Z")
    for index in range(6):
        yes_mid = 0.48 + index * 0.01
        rows.append({
            "receive_ts_utc": (base + pd.Timedelta(seconds=index)).isoformat(),
            "slug": "btc-updown-5m-1000",
            "elapsed_s": index,
            "yes_best_bid": 0.47,
            "yes_best_bid_size": 80,
            "yes_best_ask": 0.49,
            "yes_best_ask_size": 20,
            "yes_mid": yes_mid,
            "no_best_bid": 0.50,
            "no_best_bid_size": 20,
            "no_best_ask": 0.52,
            "no_best_ask_size": 80,
            "no_mid": 1.0 - yes_mid,
            "directional_top_imbalance": 1.2,
            "directional_depth_imbalance_40_60": 1.0,
            "maker_pair_bid_sum": 0.97,
            "maker_pair_edge": 0.03,
            "taker_pair_ask_sum": 1.01,
            "taker_pair_edge": -0.01,
            "mid_pair_sum": 1.0,
        })
    input_csv = tmp_path / "market_ws.csv"
    pd.DataFrame(rows).to_csv(input_csv, index=False)

    report = scan_microstructure(
        MicrostructureScannerConfig(
            input_csv=input_csv,
            output_dir=tmp_path / "out",
            maker_pair_max_cost=0.98,
            imbalance_threshold=0.5,
            future_horizons=(1.0, 3.0),
        )
    )

    assert report["paired_candidate_rows"] == 6
    assert report["paired_segments"] == 1
    assert report["imbalance_event_rows"] > 0
    assert (tmp_path / "out" / "paired_segments.csv").exists()
    assert report["imbalance_summary"][0]["hit_rate"] == 1.0


def test_microstructure_scanner_reads_sqlite_recorder_output(tmp_path: Path) -> None:
    tokens = MarketTokens(
        slug="btc-updown-5m-1000",
        window_start_ts=1000,
        market_id="0xmarket",
        yes_token_id="yes-token",
        no_token_id="no-token",
    )
    path = tmp_path / "window.market.sqlite"
    writer = SQLiteMarketWriter(path, tokens)
    base = dt.datetime.fromtimestamp(1000, tz=dt.timezone.utc)
    writer.write_event(
        {
            "event_type": "book",
            "asset_id": "yes-token",
            "market": "0xmarket",
            "bids": [{"price": "0.47", "size": "80"}],
            "asks": [{"price": "0.49", "size": "20"}],
            "timestamp": "1000000",
        },
        base,
    )
    writer.write_event(
        {
            "event_type": "book",
            "asset_id": "no-token",
            "market": "0xmarket",
            "bids": [{"price": "0.50", "size": "20"}],
            "asks": [{"price": "0.52", "size": "80"}],
            "timestamp": "1000000",
        },
        base,
    )
    writer.close()

    report = scan_microstructure(
        MicrostructureScannerConfig(
            input_csv=path,
            output_dir=tmp_path / "out_sqlite",
            maker_pair_max_cost=0.98,
        )
    )

    assert report["event_rows"] == 2
    assert report["paired_candidate_rows"] == 1
