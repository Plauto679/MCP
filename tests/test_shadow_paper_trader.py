from __future__ import annotations

import csv

from src.shadow_paper_trader import ShadowPaperTraderConfig, run_shadow_paper_trader


def write_csv(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def test_shadow_paper_trader_opens_and_settles_bid_only(tmp_path):
    actions = tmp_path / "shadow_actions.csv"
    history = tmp_path / "candidate_history.csv"
    output = tmp_path / "out"
    write_csv(
        actions,
        [
            {
                "run_ts_utc": "2026-07-09T00:00:01+00:00",
                "action": "paper_maker_quote",
                "confidence": "medium",
                "action_priority_score": "3.0",
                "market_slug": "m",
                "question": "Question?",
                "family": "fed_rates",
                "fair_value_probability_proxy": "",
                "fair_value_edge_proxy": "",
                "net_ev_pessimistic_usd": "1.0",
                "net_ev_base_usd": "2.0",
                "history_observations": "40",
                "history_fill_rate": "0.1",
            }
        ],
    )
    write_csv(
        history,
        [
            {
                "scan_ts_utc": "2026-07-09T00:00:00+00:00",
                "market_slug": "m",
                "question": "Question?",
                "daily_rate": "100",
                "market_competitiveness": "1",
                "quote_size": "10",
                "yes_bid_quote": "0.45",
                "yes_bid_reward_score": "10",
                "yes_mid": "0.50",
                "yes_best_ask": "0.47",
                "yes_best_bid": "0.44",
                "no_bid_quote": "0.45",
                "no_bid_reward_score": "10",
                "no_mid": "0.50",
                "no_best_ask": "0.47",
                "no_best_bid": "0.44",
            }
        ],
    )

    first = run_shadow_paper_trader(
        ShadowPaperTraderConfig(
            shadow_actions_csv=actions,
            maker_history_csv=history,
            output_dir=output,
            require_new_order_hours_to_end=False,
        )
    )
    assert first["new_orders_opened"] == 2
    assert first["overall"]["open_orders"] == 2

    write_csv(
        history,
        [
            {
                "scan_ts_utc": "2026-07-09T00:00:00+00:00",
                "market_slug": "m",
                "question": "Question?",
                "daily_rate": "100",
                "market_competitiveness": "1",
                "quote_size": "10",
                "yes_bid_quote": "0.45",
                "yes_bid_reward_score": "10",
                "yes_mid": "0.50",
                "yes_best_ask": "0.47",
                "yes_best_bid": "0.44",
                "no_bid_quote": "0.45",
                "no_bid_reward_score": "10",
                "no_mid": "0.50",
                "no_best_ask": "0.47",
                "no_best_bid": "0.44",
            },
            {
                "scan_ts_utc": "2026-07-09T00:10:00+00:00",
                "market_slug": "m",
                "question": "Question?",
                "daily_rate": "100",
                "market_competitiveness": "1",
                "quote_size": "10",
                "yes_bid_quote": "0.44",
                "yes_bid_reward_score": "10",
                "yes_mid": "0.43",
                "yes_best_ask": "0.45",
                "yes_best_bid": "0.42",
                "no_bid_quote": "0.54",
                "no_bid_reward_score": "10",
                "no_mid": "0.57",
                "no_best_ask": "0.60",
                "no_best_bid": "0.55",
            },
        ],
    )
    second = run_shadow_paper_trader(
        ShadowPaperTraderConfig(
            shadow_actions_csv=actions,
            maker_history_csv=history,
            output_dir=output,
            require_new_order_hours_to_end=False,
        )
    )
    assert second["orders_settled_this_cycle"] == 2
    assert second["overall"]["settled_orders"] == 2
    assert second["overall"]["filled_orders"] == 1
    assert second["overall"]["open_orders"] == 1
    assert second["overall"]["mark_to_mid_pnl_usd"] == -0.2


def test_shadow_paper_trader_closes_position_on_take_profit(tmp_path):
    actions = tmp_path / "shadow_actions.csv"
    history = tmp_path / "candidate_history.csv"
    output = tmp_path / "out"
    write_csv(
        actions,
        [
            {
                "run_ts_utc": "2026-07-09T00:00:01+00:00",
                "action": "paper_maker_quote",
                "confidence": "medium",
                "action_priority_score": "3.0",
                "market_slug": "m",
                "question": "Question?",
                "family": "fed_rates",
                "fair_value_probability_proxy": "",
                "fair_value_edge_proxy": "",
                "net_ev_pessimistic_usd": "1.0",
                "net_ev_base_usd": "2.0",
                "history_observations": "40",
                "history_fill_rate": "0.1",
            }
        ],
    )
    write_csv(
        history,
        [
            {
                "scan_ts_utc": "2026-07-09T00:00:00+00:00",
                "market_slug": "m",
                "question": "Question?",
                "daily_rate": "100",
                "market_competitiveness": "1",
                "quote_size": "10",
                "yes_bid_quote": "0.45",
                "yes_bid_reward_score": "10",
                "yes_mid": "0.50",
                "yes_best_ask": "0.47",
                "yes_best_bid": "0.44",
                "no_bid_quote": "0.45",
                "no_bid_reward_score": "10",
                "no_mid": "0.50",
                "no_best_ask": "0.47",
                "no_best_bid": "0.44",
            }
        ],
    )
    run_shadow_paper_trader(
        ShadowPaperTraderConfig(
            shadow_actions_csv=actions,
            maker_history_csv=history,
            output_dir=output,
            max_new_markets_per_cycle=1,
            require_new_order_hours_to_end=False,
        )
    )
    write_csv(
        history,
        [
            {
                "scan_ts_utc": "2026-07-09T00:00:00+00:00",
                "market_slug": "m",
                "question": "Question?",
                "daily_rate": "100",
                "market_competitiveness": "1",
                "quote_size": "10",
                "yes_bid_quote": "0.45",
                "yes_bid_reward_score": "10",
                "yes_mid": "0.50",
                "yes_best_ask": "0.47",
                "yes_best_bid": "0.44",
                "no_bid_quote": "0.45",
                "no_bid_reward_score": "10",
                "no_mid": "0.50",
                "no_best_ask": "0.47",
                "no_best_bid": "0.44",
            },
            {
                "scan_ts_utc": "2026-07-09T00:10:00+00:00",
                "market_slug": "m",
                "question": "Question?",
                "daily_rate": "100",
                "market_competitiveness": "1",
                "quote_size": "10",
                "yes_bid_quote": "0.45",
                "yes_bid_reward_score": "10",
                "yes_mid": "0.47",
                "yes_best_ask": "0.45",
                "yes_best_bid": "0.44",
                "no_bid_quote": "0.45",
                "no_bid_reward_score": "10",
                "no_mid": "0.50",
                "no_best_ask": "0.60",
                "no_best_bid": "0.55",
            },
            {
                "scan_ts_utc": "2026-07-09T00:20:00+00:00",
                "market_slug": "m",
                "question": "Question?",
                "daily_rate": "100",
                "market_competitiveness": "1",
                "quote_size": "10",
                "yes_bid_quote": "0.47",
                "yes_bid_reward_score": "10",
                "yes_mid": "0.49",
                "yes_best_ask": "0.50",
                "yes_best_bid": "0.48",
                "no_bid_quote": "0.50",
                "no_bid_reward_score": "10",
                "no_mid": "0.51",
                "no_best_ask": "0.52",
                "no_best_bid": "0.50",
            },
        ],
    )
    result = run_shadow_paper_trader(
        ShadowPaperTraderConfig(
            shadow_actions_csv=actions,
            maker_history_csv=history,
            output_dir=output,
            max_new_markets_per_cycle=1,
            take_profit_per_share=0.02,
            require_new_order_hours_to_end=False,
        )
    )
    assert result["positions_closed_this_cycle"] == 1
    assert result["active_exit"]["take_profit_closes"] == 1
    assert result["active_exit"]["realized_position_pnl_usd"] == 0.3
    assert result["active_exit"]["open_positions"] == 0


def test_shadow_paper_trader_blocks_fast_esports_other_market(tmp_path):
    actions = tmp_path / "shadow_actions.csv"
    history = tmp_path / "candidate_history.csv"
    output = tmp_path / "out"
    write_csv(
        actions,
        [
            {
                "run_ts_utc": "2026-07-09T00:00:01+00:00",
                "action": "paper_maker_quote",
                "confidence": "medium",
                "action_priority_score": "9.0",
                "market_slug": "dota2-gl-flc-2026-07-09-match-result-team2",
                "question": "Team Falcons to win 2-0?",
                "family": "other",
                "fair_value_probability_proxy": "",
                "fair_value_edge_proxy": "",
                "net_ev_pessimistic_usd": "10.0",
                "net_ev_base_usd": "20.0",
                "history_observations": "40000",
                "history_fill_rate": "0.07",
            }
        ],
    )
    write_csv(
        history,
        [
            {
                "scan_ts_utc": "2026-07-09T00:00:00+00:00",
                "market_slug": "dota2-gl-flc-2026-07-09-match-result-team2",
                "question": "Team Falcons to win 2-0?",
                "event_slug": "dota2-gl-flc-2026-07-09-match-result",
                "group_item_title": "",
                "end_date": "2026-07-10T00:00:00+00:00",
                "daily_rate": "100",
                "market_competitiveness": "1",
                "quote_size": "10",
                "yes_bid_quote": "0.45",
                "yes_bid_reward_score": "10",
                "yes_mid": "0.50",
                "yes_best_ask": "0.47",
                "yes_best_bid": "0.44",
                "no_bid_quote": "0.45",
                "no_bid_reward_score": "10",
                "no_mid": "0.50",
                "no_best_ask": "0.47",
                "no_best_bid": "0.44",
            }
        ],
    )

    result = run_shadow_paper_trader(
        ShadowPaperTraderConfig(
            shadow_actions_csv=actions,
            maker_history_csv=history,
            output_dir=output,
        )
    )

    assert result["new_orders_opened"] == 0
    assert result["overall"]["orders"] == 0
    assert result["new_order_filter"]["skipped_by_reason"] == {"blocked_fast_market_text": 1}


def test_shadow_paper_trader_cancels_open_quote_that_now_fails_filter(tmp_path):
    actions = tmp_path / "shadow_actions.csv"
    history = tmp_path / "candidate_history.csv"
    output = tmp_path / "out"
    write_csv(
        actions,
        [
            {
                "run_ts_utc": "2026-07-09T00:00:01+00:00",
                "action": "paper_maker_quote",
                "confidence": "medium",
                "action_priority_score": "3.0",
                "market_slug": "m",
                "question": "Question?",
                "family": "fed_rates",
                "fair_value_probability_proxy": "",
                "fair_value_edge_proxy": "",
                "net_ev_pessimistic_usd": "1.0",
                "net_ev_base_usd": "2.0",
                "history_observations": "40",
                "history_fill_rate": "0.1",
            }
        ],
    )
    write_csv(
        history,
        [
            {
                "scan_ts_utc": "2026-07-09T00:00:00+00:00",
                "market_slug": "m",
                "question": "Question?",
                "event_slug": "",
                "group_item_title": "",
                "end_date": "2026-07-20T00:00:00+00:00",
                "daily_rate": "100",
                "market_competitiveness": "1",
                "quote_size": "10",
                "yes_bid_quote": "0.45",
                "yes_bid_reward_score": "10",
                "yes_mid": "0.50",
                "yes_best_ask": "0.47",
                "yes_best_bid": "0.44",
                "no_bid_quote": "0.45",
                "no_bid_reward_score": "10",
                "no_mid": "0.50",
                "no_best_ask": "0.47",
                "no_best_bid": "0.44",
            }
        ],
    )
    first = run_shadow_paper_trader(
        ShadowPaperTraderConfig(
            shadow_actions_csv=actions,
            maker_history_csv=history,
            output_dir=output,
        )
    )
    assert first["new_orders_opened"] == 2

    second = run_shadow_paper_trader(
        ShadowPaperTraderConfig(
            shadow_actions_csv=actions,
            maker_history_csv=history,
            output_dir=output,
            allowed_new_order_families=("wti_daily",),
        )
    )

    assert second["open_quotes_canceled_by_filter_this_cycle"] == 2
    assert second["overall"]["open_quote_orders"] == 0


def test_shadow_paper_trader_respects_yes_only_action_side(tmp_path):
    actions = tmp_path / "shadow_actions.csv"
    history = tmp_path / "candidate_history.csv"
    output = tmp_path / "out"
    write_csv(
        actions,
        [
            {
                "run_ts_utc": "2026-07-09T00:00:01+00:00",
                "action": "paper_maker_quote",
                "confidence": "medium",
                "action_priority_score": "3.0",
                "market_slug": "m",
                "question": "Question?",
                "family": "fed_rates",
                "side": "YES_ONLY",
                "fair_value_probability_proxy": "0.7",
                "fair_value_edge_proxy": "0.1",
                "net_ev_pessimistic_usd": "1.0",
                "net_ev_base_usd": "2.0",
                "history_observations": "40",
                "history_fill_rate": "0.1",
            }
        ],
    )
    write_csv(
        history,
        [
            {
                "scan_ts_utc": "2026-07-09T00:00:00+00:00",
                "market_slug": "m",
                "question": "Question?",
                "end_date": "2026-07-20T00:00:00+00:00",
                "daily_rate": "100",
                "market_competitiveness": "1",
                "quote_size": "10",
                "yes_bid_quote": "0.45",
                "yes_bid_reward_score": "10",
                "yes_mid": "0.50",
                "yes_best_ask": "0.47",
                "yes_best_bid": "0.44",
                "no_bid_quote": "0.45",
                "no_bid_reward_score": "10",
                "no_mid": "0.50",
                "no_best_ask": "0.47",
                "no_best_bid": "0.44",
            }
        ],
    )

    result = run_shadow_paper_trader(
        ShadowPaperTraderConfig(
            shadow_actions_csv=actions,
            maker_history_csv=history,
            output_dir=output,
        )
    )

    assert result["new_orders_opened"] == 1
    rows = list(csv.DictReader((output / "paper_orders.csv").open(newline="", encoding="utf-8")))
    assert [row["outcome"] for row in rows] == ["yes"]


def test_shadow_paper_trader_opens_directional_action_as_position(tmp_path):
    actions = tmp_path / "shadow_actions.csv"
    history = tmp_path / "candidate_history.csv"
    output = tmp_path / "out"
    write_csv(
        actions,
        [
            {
                "run_ts_utc": "2026-07-09T00:00:01+00:00",
                "action": "paper_directional_yes",
                "confidence": "medium",
                "action_priority_score": "4.0",
                "market_slug": "m",
                "question": "Question?",
                "family": "fed_rates",
                "side": "YES",
                "fair_value_probability_proxy": "0.7",
                "fair_value_edge_proxy": "0.1",
                "net_ev_pessimistic_usd": "",
                "net_ev_base_usd": "",
                "history_observations": "",
                "history_fill_rate": "",
            }
        ],
    )
    write_csv(
        history,
        [
            {
                "scan_ts_utc": "2026-07-09T00:00:00+00:00",
                "market_slug": "m",
                "question": "Question?",
                "end_date": "2026-07-20T00:00:00+00:00",
                "daily_rate": "100",
                "market_competitiveness": "1",
                "quote_size": "10",
                "yes_bid_quote": "0.45",
                "yes_bid_reward_score": "10",
                "yes_mid": "0.50",
                "yes_best_ask": "0.52",
                "yes_best_bid": "0.49",
                "no_bid_quote": "0.45",
                "no_bid_reward_score": "10",
                "no_mid": "0.50",
                "no_best_ask": "0.52",
                "no_best_bid": "0.49",
            }
        ],
    )

    result = run_shadow_paper_trader(
        ShadowPaperTraderConfig(
            shadow_actions_csv=actions,
            maker_history_csv=history,
            output_dir=output,
            directional_size_override=20,
            directional_entry_mode="taker",
        )
    )

    assert result["new_orders_opened"] == 1
    assert result["active_exit"]["open_positions"] == 1
    rows = list(csv.DictReader((output / "paper_orders.csv").open(newline="", encoding="utf-8")))
    assert rows[0]["status"] == "POSITION_OPEN"
    assert rows[0]["quote_side"] == "taker_buy"
    assert rows[0]["outcome"] == "yes"
    assert rows[0]["mark_to_mid_pnl_usd"] == "-0.4"


def test_shadow_paper_trader_closes_stale_position_without_updates(tmp_path):
    actions = tmp_path / "shadow_actions.csv"
    history = tmp_path / "candidate_history.csv"
    output = tmp_path / "out"
    write_csv(
        actions,
        [
            {
                "run_ts_utc": "2026-07-09T00:00:01+00:00",
                "action": "paper_maker_quote",
                "confidence": "medium",
                "action_priority_score": "3.0",
                "market_slug": "m",
                "question": "Question?",
                "family": "fed_rates",
                "fair_value_probability_proxy": "",
                "fair_value_edge_proxy": "",
                "net_ev_pessimistic_usd": "1.0",
                "net_ev_base_usd": "2.0",
                "history_observations": "40",
                "history_fill_rate": "0.1",
            }
        ],
    )
    write_csv(
        history,
        [
            {
                "scan_ts_utc": "2026-07-09T00:00:00+00:00",
                "market_slug": "m",
                "question": "Question?",
                "end_date": "2026-07-20T00:00:00+00:00",
                "daily_rate": "100",
                "market_competitiveness": "1",
                "quote_size": "10",
                "yes_bid_quote": "0.45",
                "yes_bid_reward_score": "10",
                "yes_mid": "0.50",
                "yes_best_ask": "0.47",
                "yes_best_bid": "0.44",
                "no_bid_quote": "0.45",
                "no_bid_reward_score": "10",
                "no_mid": "0.50",
                "no_best_ask": "0.47",
                "no_best_bid": "0.44",
            }
        ],
    )
    run_shadow_paper_trader(
        ShadowPaperTraderConfig(
            shadow_actions_csv=actions,
            maker_history_csv=history,
            output_dir=output,
        )
    )
    write_csv(
        history,
        [
            {
                "scan_ts_utc": "2026-07-09T00:00:00+00:00",
                "market_slug": "m",
                "question": "Question?",
                "end_date": "2026-07-20T00:00:00+00:00",
                "daily_rate": "100",
                "market_competitiveness": "1",
                "quote_size": "10",
                "yes_bid_quote": "0.45",
                "yes_bid_reward_score": "10",
                "yes_mid": "0.50",
                "yes_best_ask": "0.47",
                "yes_best_bid": "0.44",
                "no_bid_quote": "0.45",
                "no_bid_reward_score": "10",
                "no_mid": "0.50",
                "no_best_ask": "0.47",
                "no_best_bid": "0.44",
            },
            {
                "scan_ts_utc": "2026-07-09T00:10:00+00:00",
                "market_slug": "m",
                "question": "Question?",
                "end_date": "2026-07-20T00:00:00+00:00",
                "daily_rate": "100",
                "market_competitiveness": "1",
                "quote_size": "10",
                "yes_bid_quote": "0.44",
                "yes_bid_reward_score": "10",
                "yes_mid": "0.47",
                "yes_best_ask": "0.45",
                "yes_best_bid": "0.44",
                "no_bid_quote": "0.45",
                "no_bid_reward_score": "10",
                "no_mid": "0.50",
                "no_best_ask": "0.60",
                "no_best_bid": "0.55",
            },
        ],
    )
    run_shadow_paper_trader(
        ShadowPaperTraderConfig(
            shadow_actions_csv=actions,
            maker_history_csv=history,
            output_dir=output,
            stop_loss_per_share=1.0,
            max_position_cycles=99,
            max_position_without_update_hours=999999.0,
        )
    )

    result = run_shadow_paper_trader(
        ShadowPaperTraderConfig(
            shadow_actions_csv=actions,
            maker_history_csv=history,
            output_dir=output,
            stop_loss_per_share=1.0,
            max_position_cycles=99,
            max_position_without_update_hours=-1.0,
        )
    )

    assert result["stale_positions_closed_this_cycle"] == 1
    assert result["active_exit"]["signal_loss_closes"] == 0
