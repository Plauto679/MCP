from __future__ import annotations

import pandas as pd

from research.external_polymarket_dataset_probe import (
    normalise_parquet_inventory,
    rows_payload_to_frame,
    summarize_markets,
)


def test_rows_payload_to_frame_extracts_rows() -> None:
    payload = {"rows": [{"row": {"condition_id": "a", "t": 1}}, {"row": {"condition_id": "b", "t": 2}}]}

    frame = rows_payload_to_frame(payload)

    assert list(frame["condition_id"]) == ["a", "b"]


def test_normalise_parquet_inventory_flattens_dataset_server_response() -> None:
    payload = {
        "parquet_files": [
            {
                "dataset": "repo",
                "config": "markets",
                "split": "btc",
                "filename": "0000.parquet",
                "size": 4_000_000,
                "url": "https://example.test/0000.parquet",
            }
        ]
    }

    frame = normalise_parquet_inventory(payload)

    assert frame.iloc[0]["split"] == "btc"
    assert frame.iloc[0]["size_mb"] == 4.0


def test_summarize_markets_reports_outcome_balance_and_coverage() -> None:
    markets = pd.DataFrame(
        [
            {
                "market_start": "2026-03-24T22:10:00Z",
                "market_end": "2026-03-24T22:15:00Z",
                "outcome": "Up",
                "n_ticks": 300,
                "volume": 10,
                "liquidity": 1000,
            },
            {
                "market_start": "2026-03-24T22:15:00Z",
                "market_end": "2026-03-24T22:20:00Z",
                "outcome": "Down",
                "n_ticks": 298,
                "volume": 20,
                "liquidity": 2000,
            },
        ]
    )

    summary = summarize_markets(markets, "btc")

    assert summary["markets"] == 2
    assert summary["labeled_markets"] == 2
    assert summary["outcome_counts"] == {"Up": 1, "Down": 1}
    assert summary["up_rate_labeled"] == 0.5
    assert summary["full_300_tick_pct"] == 0.5
    assert summary["volume_sum"] == 30.0
