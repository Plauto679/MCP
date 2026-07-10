from __future__ import annotations

import csv
from pathlib import Path

from src.reward_fair_value_evaluator import (
    RewardFairValueEvaluatorConfig,
    _combined_watchlist,
    build_shadow_actions,
    fair_value_probability_proxy,
    latest_rows_by_market,
    run_reward_fair_value_evaluator,
)


def test_latest_rows_by_market_keeps_newest_row() -> None:
    rows = [
        {"condition_id": "c1", "scan_ts_utc": "2026-07-08T00:00:00+00:00", "value": "old"},
        {"condition_id": "c1", "scan_ts_utc": "2026-07-08T01:00:00+00:00", "value": "new"},
    ]

    latest = latest_rows_by_market(rows)

    assert len(latest) == 1
    assert latest[0]["value"] == "new"


def test_fair_value_probability_proxy_uses_wti_distance_and_time() -> None:
    row = {
        "family": "wti_daily",
        "external_distance_to_threshold": "1.0",
        "hours_to_end": "12",
    }

    assert fair_value_probability_proxy(row) > 0.5


def test_combined_watchlist_keeps_external_signal_when_maker_market_matches() -> None:
    combined = _combined_watchlist(
        maker_rankings=[
            {
                "condition_id": "condition",
                "market_slug": "same-market",
                "question": "Same market?",
                "family": "wti_daily",
                "combined_priority_score": -2.0,
                "net_ev_base_usd": "0.1",
            }
        ],
        external_rankings=[
            {
                "condition_id": "condition",
                "market_slug": "same-market",
                "question": "Same market?",
                "family": "wti_daily",
                "external_priority_score": 20.0,
                "fair_value_edge_proxy": "-0.20",
                "external_signal": "wti_yes_overpriced_proxy",
            }
        ],
    )

    assert [row["source"] for row in combined] == ["maker_rewards", "external_fair_value"]


def test_build_shadow_actions_emits_wti_directional_and_maker_actions(tmp_path: Path) -> None:
    config = RewardFairValueEvaluatorConfig(
        maker_history_csv=tmp_path / "maker.csv",
        external_history_csv=tmp_path / "external.csv",
        maker_paper_events_csv=tmp_path / "events.csv",
        output_dir=tmp_path,
    )

    actions = build_shadow_actions(
        maker_rankings=[
            {
                "market_slug": "maker",
                "question": "Maker?",
                "family": "fed_rates",
                "combined_priority_score": "3",
                "net_ev_pessimistic_usd": "0.50",
                "net_ev_base_usd": "1.50",
                "history_observations": "100",
                "history_fill_rate": "0.10",
                "fair_value_edge_proxy": "0.06",
                "external_probability_proxy": "0.56",
            }
        ],
        external_rankings=[
            {
                "market_slug": "wti",
                "question": "WTI hit high?",
                "family": "wti_daily",
                "external_priority_score": "10",
                "yes_mid": "0.45",
                "fair_value_probability_proxy": "0.25",
                "fair_value_edge_proxy": "-0.20",
                "wti_signal_stable": "True",
                "external_signal": "wti_yes_overpriced_proxy",
            }
        ],
        config=config,
    )

    assert actions[0]["action"] == "paper_directional_no"
    assert {action["action"] for action in actions} == {"paper_directional_no", "paper_maker_quote"}
    assert [action for action in actions if action["action"] == "paper_maker_quote"][0]["side"] == "YES_ONLY"


def test_build_shadow_actions_marks_wti_unstable_as_watch(tmp_path: Path) -> None:
    config = RewardFairValueEvaluatorConfig(
        maker_history_csv=tmp_path / "maker.csv",
        external_history_csv=tmp_path / "external.csv",
        maker_paper_events_csv=tmp_path / "events.csv",
        output_dir=tmp_path,
    )

    actions = build_shadow_actions(
        maker_rankings=[],
        external_rankings=[
            {
                "market_slug": "wti",
                "question": "WTI hit high?",
                "family": "wti_daily",
                "external_priority_score": "10",
                "yes_mid": "0.45",
                "fair_value_probability_proxy": "0.60",
                "fair_value_edge_proxy": "0.15",
                "wti_signal_stable": "False",
                "external_signal": "wti_yes_underpriced_proxy",
            }
        ],
        config=config,
    )

    assert actions[0]["action"] == "watch_wti_vol_sensitive"


def test_build_shadow_actions_emits_fed_directional_when_edge_is_available(tmp_path: Path) -> None:
    config = RewardFairValueEvaluatorConfig(
        maker_history_csv=tmp_path / "maker.csv",
        external_history_csv=tmp_path / "external.csv",
        maker_paper_events_csv=tmp_path / "events.csv",
        output_dir=tmp_path,
    )

    actions = build_shadow_actions(
        maker_rankings=[],
        external_rankings=[
            {
                "market_slug": "fed",
                "question": "Will the Fed increase interest rates by 25 bps after the July 2026 meeting?",
                "family": "fed_rates",
                "external_priority_score": "10",
                "yes_mid": "0.25",
                "fair_value_probability_proxy": "0.40",
                "fair_value_edge_proxy": "0.15",
                "external_signal": "fed_yes_underpriced_proxy",
            }
        ],
        config=config,
    )

    assert actions[0]["action"] == "paper_directional_yes"
    assert actions[0]["side"] == "YES"


def test_reward_fair_value_evaluator_writes_rankings(tmp_path: Path) -> None:
    maker_history = tmp_path / "maker_history.csv"
    external_history = tmp_path / "external_history.csv"
    paper_events = tmp_path / "paper_events.csv"
    _write_csv(
        maker_history,
        [
            {
                "scan_ts_utc": "2026-07-08T00:00:00+00:00",
                "condition_id": "condition",
                "market_slug": "market",
                "question": "WTI Crude Oil closes above $70?",
                "candidate_ok": "True",
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
                "no_mid": "0.50",
            }
        ],
    )
    _write_csv(
        external_history,
        [
            {
                "scan_ts_utc": "2026-07-08T00:00:00+00:00",
                "condition_id": "condition",
                "market_slug": "market",
                "question": "WTI Crude Oil closes above $70?",
                "family": "wti_daily",
                "modelability_score": "5",
                "candidate_ok": "True",
                "volume24hr": "1000",
                "hours_to_end": "12",
                "yes_mid": "0.50",
                "yes_spread": "0.04",
                "threshold_direction": "above",
                "threshold": "70",
                "external_value": "71",
                "external_distance_to_threshold": "1",
            }
        ],
    )
    _write_csv(
        paper_events,
        [
            {
                "market_slug": "market",
                "outcome": "yes",
                "quote_side": "bid",
                "filled_by_next_snapshot": "true",
                "mark_to_mid_pnl_usd": "-0.50",
            },
            {
                "market_slug": "market",
                "outcome": "yes",
                "quote_side": "ask",
                "filled_by_next_snapshot": "false",
                "mark_to_mid_pnl_usd": "0",
            },
        ],
    )

    report = run_reward_fair_value_evaluator(
        RewardFairValueEvaluatorConfig(
            maker_history_csv=maker_history,
            external_history_csv=external_history,
            maker_paper_events_csv=paper_events,
            output_dir=tmp_path / "out",
            max_snapshot_age_hours=0,
        )
    )

    assert report["maker_markets"] == 1
    assert report["external_markets"] == 1
    assert Path(report["outputs"]["combined_watchlist"]).exists()
    assert report["top_maker"][0]["external_market_match"] is True
    assert Path(report["outputs"]["shadow_actions"]).exists()
    assert Path(report["outputs"]["shadow_action_history"]).exists()


def _write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    fieldnames: list[str] = []
    for row in rows:
        for key in row:
            if key not in fieldnames:
                fieldnames.append(key)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
