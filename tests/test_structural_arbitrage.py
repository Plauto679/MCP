from __future__ import annotations

from pathlib import Path

from src.structural_arbitrage import (
    StructuralArbitrageConfig,
    best_level,
    complete_set_metrics,
    market_token_ids,
)


def test_best_level_sorts_books_defensively() -> None:
    book = {
        "bids": [{"price": "0.10", "size": "1"}, {"price": "0.42", "size": "7"}],
        "asks": [{"price": "0.90", "size": "2"}, {"price": "0.44", "size": "8"}],
    }

    assert best_level(book, "buy") == (0.44, 8.0)
    assert best_level(book, "sell") == (0.42, 7.0)


def test_complete_set_metrics_accounts_for_fee_and_size() -> None:
    books = [
        {"asks": [{"price": "0.48", "size": "10"}], "bids": [{"price": "0.46", "size": "4"}]},
        {"asks": [{"price": "0.49", "size": "6"}], "bids": [{"price": "0.50", "size": "8"}]},
    ]

    buy = complete_set_metrics(books, "buy", fee_rate=0.0)
    sell = complete_set_metrics(books, "sell", fee_rate=0.0)

    assert buy["sum_price"] == 0.97
    assert buy["gross_edge"] == 0.03
    assert buy["top_shares"] == 6.0
    assert sell["sum_price"] == 0.96
    assert sell["gross_edge"] == -0.04
    assert sell["top_shares"] == 4.0


def test_market_token_ids_reads_gamma_jsonish_field() -> None:
    market = {"clobTokenIds": '["yes-token", "no-token"]'}

    assert market_token_ids(market) == ["yes-token", "no-token"]


def test_structural_config_defaults_to_dry_output_dir(tmp_path: Path) -> None:
    config = StructuralArbitrageConfig(output_dir=tmp_path)

    assert config.min_net_edge == 0.0
    assert config.max_book_tokens > 0
