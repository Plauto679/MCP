from __future__ import annotations

import datetime as dt
from pathlib import Path

from src.market_ws_recorder import (
    MarketTokens,
    OrderBookState,
    SQLiteMarketWriter,
    gzip_file,
    iter_summary_rows_from_sqlite,
)


def _tokens() -> MarketTokens:
    return MarketTokens(
        slug="btc-updown-5m-1000",
        window_start_ts=1000,
        market_id="0xmarket",
        yes_token_id="yes-token",
        no_token_id="no-token",
    )


def test_orderbook_state_normalizes_pair_and_imbalance_metrics() -> None:
    state = OrderBookState()
    tokens = _tokens()
    receive = dt.datetime.fromtimestamp(1010, tz=dt.timezone.utc)

    yes_rows = state.normalized_rows(
        {
            "event_type": "book",
            "asset_id": "yes-token",
            "market": "0xmarket",
            "bids": [{"price": "0.47", "size": "80"}, {"price": "0.46", "size": "10"}],
            "asks": [{"price": "0.49", "size": "20"}],
            "timestamp": "1010000",
        },
        tokens,
        receive,
    )
    assert len(yes_rows) == 1
    assert yes_rows[0]["yes_best_bid"] == 0.47
    assert yes_rows[0]["maker_pair_bid_sum"] == ""

    no_rows = state.normalized_rows(
        {
            "event_type": "book",
            "asset_id": "no-token",
            "market": "0xmarket",
            "bids": [{"price": "0.50", "size": "20"}],
            "asks": [{"price": "0.52", "size": "80"}],
            "timestamp": "1010001",
        },
        tokens,
        receive,
    )
    row = no_rows[0]
    assert row["maker_pair_bid_sum"] == 0.97
    assert row["maker_pair_edge"] == 0.03
    assert row["taker_pair_ask_sum"] == 1.01
    assert row["yes_top_imbalance"] == 0.6
    assert row["no_top_imbalance"] == -0.6
    assert row["directional_top_imbalance"] == 1.2


def test_price_change_updates_and_removes_levels() -> None:
    state = OrderBookState()
    tokens = _tokens()
    receive = dt.datetime.fromtimestamp(1010, tz=dt.timezone.utc)
    state.normalized_rows(
        {
            "event_type": "book",
            "asset_id": "yes-token",
            "bids": [{"price": "0.47", "size": "80"}],
            "asks": [{"price": "0.49", "size": "20"}],
            "timestamp": "1010000",
        },
        tokens,
        receive,
    )

    rows = state.normalized_rows(
        {
            "event_type": "price_change",
            "market": "0xmarket",
            "timestamp": "1011000",
            "price_changes": [
                {
                    "asset_id": "yes-token",
                    "price": "0.49",
                    "size": "0",
                    "side": "SELL",
                    "best_bid": "0.47",
                    "best_ask": "0.50",
                }
            ],
        },
        tokens,
        receive,
    )
    assert len(rows) == 1
    assert rows[0]["yes_best_ask"] == 0.5
    assert rows[0]["yes_best_ask_size"] == 0.0


def test_sqlite_writer_round_trips_to_summary_rows(tmp_path: Path) -> None:
    tokens = _tokens()
    path = tmp_path / "window.market.sqlite"
    writer = SQLiteMarketWriter(path, tokens)
    receive = dt.datetime.fromtimestamp(1010, tz=dt.timezone.utc)
    writer.write_event(
        {
            "event_type": "book",
            "asset_id": "yes-token",
            "market": "0xmarket",
            "bids": [{"price": "0.47", "size": "80"}],
            "asks": [{"price": "0.49", "size": "20"}],
            "timestamp": "1010000",
        },
        receive,
    )
    writer.write_event(
        {
            "event_type": "book",
            "asset_id": "no-token",
            "market": "0xmarket",
            "bids": [{"price": "0.50", "size": "20"}],
            "asks": [{"price": "0.52", "size": "80"}],
            "timestamp": "1010001",
        },
        receive,
    )
    writer.write_event(
        {
            "event_type": "price_change",
            "market": "0xmarket",
            "timestamp": "1011000",
            "price_changes": [{
                "asset_id": "yes-token",
                "price": "0.48",
                "size": "40",
                "side": "BUY",
                "best_bid": "0.48",
                "best_ask": "0.49",
            }],
        },
        receive,
    )
    writer.close()

    rows = list(iter_summary_rows_from_sqlite(path))
    assert rows[-1]["yes_best_bid"] == 0.48
    assert rows[-1]["maker_pair_bid_sum"] == 0.98

    gz_path = gzip_file(path, remove_source=False)
    gz_rows = list(iter_summary_rows_from_sqlite(gz_path))
    assert gz_rows[-1]["yes_best_bid"] == 0.48
