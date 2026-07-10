from __future__ import annotations

import csv
import datetime as dt

from src.fed_rates_model import (
    fed_funds_symbol,
    fed_probability_details,
    fetch_fed_rates_context,
    parse_rate_outcome,
)


def test_fed_funds_symbol_uses_zq_month_code() -> None:
    assert fed_funds_symbol(2026, 7) == "ZQN26.CBT"
    assert fed_funds_symbol(2026, 9) == "ZQU26.CBT"


def test_parse_rate_outcome_reads_direction_and_bps() -> None:
    assert parse_rate_outcome("Will the Fed increase interest rates by 25 bps after the July 2026 meeting?") == (
        "hike_at_least",
        25.0,
    )
    assert parse_rate_outcome("Will there be no change in Fed interest rates after the July 2026 meeting?") == (
        "no_change",
        0.0,
    )


def test_fed_probability_proxy_prefers_no_change_when_futures_imply_flat_path() -> None:
    row = {
        "market_slug": "fed-no-change-july",
        "question": "Will there be no change in Fed interest rates after the July 2026 meeting?",
        "end_date": "2026-07-29T18:00:00+00:00",
    }
    context = {
        "current_effr": 3.625,
        "futures": {"2026-07": {"symbol": "ZQN26.CBT", "price": 96.375}},
        "manual_probabilities": [],
    }

    details = fed_probability_details(row, context)

    assert details.outcome_type == "no_change"
    assert details.probability > 0.90
    assert abs(details.expected_change_bps) < 1.0


def test_fed_probability_proxy_marks_hike_likely_when_post_meeting_rate_jumps() -> None:
    decision = dt.date(2026, 7, 29)
    days = 31
    post_days = days - decision.day + 1
    pre_days = days - post_days
    current_effr = 3.625
    post_rate = 4.0
    implied_monthly_rate = (current_effr * pre_days + post_rate * post_days) / days
    row = {
        "market_slug": "fed-hike-july",
        "question": "Will the Fed increase interest rates by 25 bps after the July 2026 meeting?",
        "end_date": "2026-07-29T18:00:00+00:00",
    }
    context = {
        "current_effr": current_effr,
        "futures": {"2026-07": {"symbol": "ZQN26.CBT", "price": 100.0 - implied_monthly_rate}},
        "manual_probabilities": [],
    }

    details = fed_probability_details(row, context)

    assert details.outcome_type == "hike_at_least"
    assert details.probability > 0.80
    assert details.expected_change_bps > 35.0


def test_fed_probability_uses_manual_csv_override(tmp_path, monkeypatch) -> None:
    path = tmp_path / "fedwatch_probabilities.csv"
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["market_slug", "probability", "source"])
        writer.writeheader()
        writer.writerow(
            {
                "market_slug": "fed-hike-july",
                "probability": "0.42",
                "source": "manual_test",
            }
        )

    monkeypatch.setattr("src.fed_rates_model._fetch_latest_effr", lambda timeout_s: 3.625)
    monkeypatch.setattr(
        "src.fed_rates_model.fetch_yahoo_futures_price",
        lambda symbol, timeout_s: {"symbol": symbol, "price": 96.375, "source": "test"},
    )
    context = fetch_fed_rates_context(
        [
            {
                "market_slug": "fed-hike-july",
                "question": "Will the Fed increase interest rates by 25 bps after the July 2026 meeting?",
            }
        ],
        manual_probabilities_csv=path,
    )

    details = fed_probability_details(
        {
            "market_slug": "fed-hike-july",
            "question": "Will the Fed increase interest rates by 25 bps after the July 2026 meeting?",
        },
        context,
    )

    assert details.probability == 0.42
    assert details.source == "manual_test"


def test_fed_probability_rejects_non_meeting_markets_without_manual_override() -> None:
    details = fed_probability_details(
        {
            "market_slug": "will-no-fed-rate-cuts-happen-in-2026",
            "question": "Will no Fed rate cuts happen in 2026?",
            "end_date": "2026-12-31T23:59:00+00:00",
        },
        {
            "current_effr": 3.625,
            "futures": {"2026-12": {"symbol": "ZQZ26.CBT", "price": 96.10}},
            "manual_probabilities": [],
        },
    )

    assert details.warning == "unsupported_non_meeting_market"
