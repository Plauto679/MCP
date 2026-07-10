from __future__ import annotations

import datetime as dt

from src.market_feature_recorder import FeatureState
from src.market_ws_recorder import MarketTokens, btc_5m_slug, updown_slug


def _tokens() -> MarketTokens:
    return MarketTokens(
        slug="btc-updown-5m-1000",
        window_start_ts=1000,
        market_id="0xmarket",
        yes_token_id="yes-token",
        no_token_id="no-token",
    )


def test_feature_state_samples_compact_pair_and_counter_features() -> None:
    tokens = _tokens()
    state = FeatureState(tokens)
    receive = dt.datetime.fromtimestamp(1001, tz=dt.timezone.utc)

    state._ingest_event(
        {
            "event_type": "book",
            "asset_id": "yes-token",
            "bids": [{"price": "0.47", "size": "80"}],
            "asks": [{"price": "0.49", "size": "20"}],
        },
        receive,
    )
    state._ingest_event(
        {
            "event_type": "book",
            "asset_id": "no-token",
            "bids": [{"price": "0.50", "size": "20"}],
            "asks": [{"price": "0.52", "size": "80"}],
        },
        receive,
    )

    row = state.row(dt.datetime.fromtimestamp(1002, tz=dt.timezone.utc))

    assert row["maker_pair_bid_sum"] == 0.97
    assert row["maker_pair_edge"] == 0.03
    assert row["directional_top_imbalance"] == 1.2
    assert row["book_events_since_sample"] == 2
    assert row["yes_quote_age_ms"] == 1000.0

    next_row = state.row(dt.datetime.fromtimestamp(1003, tz=dt.timezone.utc))
    assert next_row["book_events_since_sample"] == 0


def test_updown_slug_supports_5m_and_15m_windows() -> None:
    assert btc_5m_slug(1783319499) == "btc-updown-5m-1783319400"
    assert updown_slug("btc-updown-15m", 900, 1783319999) == "btc-updown-15m-1783319400"
