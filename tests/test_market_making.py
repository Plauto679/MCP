from __future__ import annotations

import csv
from pathlib import Path

from src.market_making import (
    MakerPaperSimConfig,
    MarketMakingScanConfig,
    best_level_from_book,
    evaluate_reward_market,
    market_hours_to_end,
    normalize_reward_spread,
    reward_position_score,
    simulate_market_making_history,
)


def test_normalize_reward_spread_accepts_cents_and_decimal() -> None:
    assert normalize_reward_spread(4.5) == 0.045
    assert normalize_reward_spread(0.03) == 0.03


def test_reward_position_score_uses_quadratic_decay_and_size_cutoff() -> None:
    assert reward_position_score(0.04, 0.02, 100.0, 50.0) == 25.0
    assert reward_position_score(0.04, 0.05, 100.0, 50.0) == 0.0
    assert reward_position_score(0.04, 0.01, 49.0, 50.0) == 0.0


def test_market_hours_to_end_parses_polymarket_dates() -> None:
    hours = market_hours_to_end("2026-07-07 04:30:00+00", "2026-07-07T00:00:00+00:00")

    assert hours == 4.5


def test_best_level_from_book_sorts_unsorted_levels() -> None:
    book = {
        "bids": [{"price": "0.10", "size": "1"}, {"price": "0.42", "size": "7"}],
        "asks": [{"price": "0.90", "size": "2"}, {"price": "0.44", "size": "8"}],
    }

    assert best_level_from_book(book, "bid") == (0.42, 7.0)
    assert best_level_from_book(book, "ask") == (0.44, 8.0)


def test_evaluate_reward_market_flags_viable_candidate(tmp_path: Path) -> None:
    market = {
        "condition_id": "condition",
        "market_slug": "example",
        "question": "Example?",
        "rewards_config": [{"rate_per_day": 50}],
        "rewards_max_spread": 4.5,
        "rewards_min_size": 20,
        "volume_24hr": 5000,
        "one_day_price_change": 0.02,
        "end_date": "2026-07-08 00:00:00+00",
        "tokens": [
            {"token_id": "yes-token", "outcome": "Yes"},
            {"token_id": "no-token", "outcome": "No"},
        ],
    }
    books = {
        "yes-token": {
            "tick_size": "0.01",
            "bids": [{"price": "0.48", "size": "100"}],
            "asks": [{"price": "0.52", "size": "100"}],
        },
        "no-token": {
            "tick_size": "0.01",
            "bids": [{"price": "0.48", "size": "100"}],
            "asks": [{"price": "0.52", "size": "100"}],
        },
    }
    config = MarketMakingScanConfig(output_dir=tmp_path, min_daily_rate=10, min_volume_24hr=100)

    row = evaluate_reward_market("2026-07-07T00:00:00+00:00", market, books, config)

    assert row["candidate_ok"] is True
    assert row["scoreable_sides"] == 4
    assert row["mid_pair_sum"] == 1.0
    assert row["yes_bid_quote"] == 0.49


def test_evaluate_reward_market_joins_when_spread_is_one_tick(tmp_path: Path) -> None:
    market = {
        "condition_id": "condition",
        "rewards_config": [{"rate_per_day": 50}],
        "rewards_max_spread": 4.5,
        "rewards_min_size": 20,
        "volume_24hr": 5000,
        "tokens": [
            {"token_id": "yes-token", "outcome": "Yes"},
            {"token_id": "no-token", "outcome": "No"},
        ],
    }
    books = {
        "yes-token": {
            "tick_size": "0.01",
            "bids": [{"price": "0.06", "size": "100"}],
            "asks": [{"price": "0.07", "size": "100"}],
        },
        "no-token": {
            "tick_size": "0.01",
            "bids": [{"price": "0.93", "size": "100"}],
            "asks": [{"price": "0.94", "size": "100"}],
        },
    }
    config = MarketMakingScanConfig(output_dir=tmp_path, min_daily_rate=10, min_volume_24hr=100)

    row = evaluate_reward_market("2026-07-07T00:00:00+00:00", market, books, config)

    assert row["yes_bid_quote"] == 0.06
    assert row["yes_ask_quote"] == 0.07


def test_evaluate_reward_market_rejects_near_end_market(tmp_path: Path) -> None:
    market = {
        "condition_id": "condition",
        "rewards_config": [{"rate_per_day": 50}],
        "rewards_max_spread": 4.5,
        "rewards_min_size": 20,
        "volume_24hr": 5000,
        "end_date": "2026-07-07 01:00:00+00",
        "tokens": [
            {"token_id": "yes-token", "outcome": "Yes"},
            {"token_id": "no-token", "outcome": "No"},
        ],
    }
    books = {
        "yes-token": {
            "tick_size": "0.01",
            "bids": [{"price": "0.48", "size": "100"}],
            "asks": [{"price": "0.52", "size": "100"}],
        },
        "no-token": {
            "tick_size": "0.01",
            "bids": [{"price": "0.48", "size": "100"}],
            "asks": [{"price": "0.52", "size": "100"}],
        },
    }
    config = MarketMakingScanConfig(output_dir=tmp_path, min_hours_to_end=2.0)

    row = evaluate_reward_market("2026-07-07T00:00:00+00:00", market, books, config)

    assert row["candidate_ok"] is False
    assert "near_or_past_end" in row["candidate_flags"]


def test_evaluate_reward_market_rejects_missing_end_date_by_default(tmp_path: Path) -> None:
    market = {
        "condition_id": "condition",
        "rewards_config": [{"rate_per_day": 50}],
        "rewards_max_spread": 4.5,
        "rewards_min_size": 20,
        "volume_24hr": 5000,
        "tokens": [
            {"token_id": "yes-token", "outcome": "Yes"},
            {"token_id": "no-token", "outcome": "No"},
        ],
    }
    books = {
        "yes-token": {
            "tick_size": "0.01",
            "bids": [{"price": "0.48", "size": "100"}],
            "asks": [{"price": "0.52", "size": "100"}],
        },
        "no-token": {
            "tick_size": "0.01",
            "bids": [{"price": "0.48", "size": "100"}],
            "asks": [{"price": "0.52", "size": "100"}],
        },
    }
    config = MarketMakingScanConfig(output_dir=tmp_path)

    row = evaluate_reward_market("2026-07-07T00:00:00+00:00", market, books, config)

    assert row["candidate_ok"] is False
    assert "missing_end_date" in row["candidate_flags"]


def test_market_making_paper_sim_counts_crossed_bid_as_fill(tmp_path: Path) -> None:
    history_csv = tmp_path / "candidate_history.csv"
    _write_history(
        history_csv,
        [
            {
                "scan_ts_utc": "2026-07-07T00:00:00+00:00",
                "condition_id": "condition",
                "market_slug": "example",
                "question": "Example?",
                "daily_rate": "864",
                "market_competitiveness": "0",
                "quote_size": "10",
                "yes_bid_quote": "0.49",
                "yes_bid_reward_score": "10",
                "yes_mid": "0.50",
            },
            {
                "scan_ts_utc": "2026-07-07T00:10:00+00:00",
                "condition_id": "condition",
                "market_slug": "example",
                "question": "Example?",
                "yes_best_ask": "0.48",
                "yes_mid": "0.47",
            },
        ],
    )

    report = simulate_market_making_history(MakerPaperSimConfig(history_csv=history_csv, output_dir=tmp_path))

    assert report["quote_events"] == 1
    assert report["overall"]["fills_by_next_snapshot"] == 1
    assert report["overall"]["mark_to_mid_pnl_usd"] == -0.2
    assert report["overall"]["reward_proxy_aggressive_usd"] == 6.0
    assert report["overall"]["reward_proxy_conservative_usd"] == 0.0
    assert report["overall"]["net_pnl_conservative_reward_usd"] == -0.2


def test_market_making_paper_sim_counts_crossed_ask_as_fill(tmp_path: Path) -> None:
    history_csv = tmp_path / "candidate_history.csv"
    _write_history(
        history_csv,
        [
            {
                "scan_ts_utc": "2026-07-07T00:00:00+00:00",
                "condition_id": "condition",
                "market_slug": "example",
                "question": "Example?",
                "daily_rate": "864",
                "market_competitiveness": "0",
                "quote_size": "10",
                "yes_ask_quote": "0.51",
                "yes_ask_reward_score": "10",
                "yes_mid": "0.50",
            },
            {
                "scan_ts_utc": "2026-07-07T00:10:00+00:00",
                "condition_id": "condition",
                "market_slug": "example",
                "question": "Example?",
                "yes_best_bid": "0.52",
                "yes_mid": "0.54",
            },
        ],
    )

    report = simulate_market_making_history(MakerPaperSimConfig(history_csv=history_csv, output_dir=tmp_path))

    assert report["quote_events"] == 1
    assert report["overall"]["fills_by_next_snapshot"] == 1
    assert report["overall"]["mark_to_mid_pnl_usd"] == -0.3


def test_market_making_paper_sim_credits_conservative_reward_when_not_filled(tmp_path: Path) -> None:
    history_csv = tmp_path / "candidate_history.csv"
    _write_history(
        history_csv,
        [
            {
                "scan_ts_utc": "2026-07-07T00:00:00+00:00",
                "condition_id": "condition",
                "market_slug": "example",
                "question": "Example?",
                "daily_rate": "864",
                "market_competitiveness": "0",
                "quote_size": "10",
                "yes_bid_quote": "0.49",
                "yes_bid_reward_score": "10",
                "yes_mid": "0.50",
            },
            {
                "scan_ts_utc": "2026-07-07T00:10:00+00:00",
                "condition_id": "condition",
                "market_slug": "example",
                "question": "Example?",
                "yes_best_ask": "0.51",
                "yes_mid": "0.50",
            },
        ],
    )

    report = simulate_market_making_history(MakerPaperSimConfig(history_csv=history_csv, output_dir=tmp_path))

    assert report["quote_events"] == 1
    assert report["overall"]["fills_by_next_snapshot"] == 0
    assert report["overall"]["reward_proxy_conservative_usd"] == 6.0
    assert report["overall"]["net_pnl_conservative_reward_usd"] == 6.0


def _write_history(path: Path, rows: list[dict[str, str]]) -> None:
    fieldnames: list[str] = []
    for row in rows:
        for key in row:
            if key not in fieldnames:
                fieldnames.append(key)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
