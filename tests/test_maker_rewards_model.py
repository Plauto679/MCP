from __future__ import annotations

from src.maker_rewards_model import (
    HistoricalQuoteStats,
    estimate_maker_snapshot_economics,
    fee_equivalent_usd,
    infer_category,
    liquidity_reward_score,
    liquidity_reward_usd,
    maker_rebate_proxy_usd,
    order_position_score,
)


def test_order_position_score_matches_quadratic_shape() -> None:
    assert order_position_score(max_spread=0.04, distance_from_mid=0.02, shares=100, min_size=50) == 25.0
    assert order_position_score(max_spread=0.04, distance_from_mid=0.05, shares=100, min_size=50) == 0.0
    assert order_position_score(max_spread=0.04, distance_from_mid=0.01, shares=49, min_size=50) == 0.0


def test_liquidity_reward_score_boosts_two_sided_depth() -> None:
    one_sided = liquidity_reward_score(
        yes_bid_score=90,
        yes_ask_score=0,
        no_bid_score=0,
        no_ask_score=0,
        market_midpoint=0.5,
    )
    two_sided = liquidity_reward_score(
        yes_bid_score=90,
        yes_ask_score=80,
        no_bid_score=80,
        no_ask_score=90,
        market_midpoint=0.5,
    )

    assert one_sided == 30.0
    assert two_sided == 160.0


def test_liquidity_reward_usd_uses_relative_competition_share() -> None:
    reward = liquidity_reward_usd(daily_rate=144, seconds=600, our_score=100, competition_score=300)

    assert reward == 0.25


def test_fee_equivalent_and_maker_rebate_proxy_follow_fee_curve() -> None:
    assert fee_equivalent_usd(shares=100, price=0.5, category="crypto") == 1.75
    assert maker_rebate_proxy_usd(shares=100, price=0.5, category="crypto") == 0.35
    assert maker_rebate_proxy_usd(shares=100, price=0.5, category="sports") == 0.1875


def test_infer_category_maps_research_families_to_fee_categories() -> None:
    assert infer_category(family="wti_daily", question="WTI closes above 70?") == "finance"
    assert infer_category(family="fed_rates", question="Fed cut?") == "economics"
    assert infer_category(question="Will BTC be above 100k?") == "crypto"


def test_estimate_maker_snapshot_economics_outputs_scenarios() -> None:
    row = {
        "question": "Example sports market?",
        "daily_rate": "144",
        "market_competitiveness": "300",
        "quote_size": "100",
        "yes_bid_quote": "0.49",
        "yes_ask_quote": "0.51",
        "no_bid_quote": "0.49",
        "no_ask_quote": "0.51",
        "yes_bid_reward_score": "90",
        "yes_ask_reward_score": "80",
        "no_bid_reward_score": "80",
        "no_ask_reward_score": "90",
        "yes_mid": "0.50",
    }
    stats = HistoricalQuoteStats(
        observations=100,
        fill_rate=0.10,
        avg_mark_pnl_per_filled_quote_usd=-0.50,
    )

    economics = estimate_maker_snapshot_economics(row, interval_seconds=600, history_stats=stats)

    assert economics["quote_count"] == 4
    assert economics["our_liquidity_score"] == 160.0
    assert economics["history_fill_rate"] == 0.10
    assert economics["net_ev_optimistic_usd"] > economics["net_ev_pessimistic_usd"]
