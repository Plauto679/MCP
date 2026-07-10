import asyncio
import json
import math

from src.fair_value import (
    FairValueDecision,
    best_decision,
    blend_fair_yes_with_market,
    empirical_continuation_probability,
    estimate_fair_yes,
    taker_breakeven_probability,
    taker_fee_for_stake,
)
from src.orchestrator import CopyTrader


def make_fair_trader():
    trader = CopyTrader.__new__(CopyTrader)
    trader.logs = []
    trader.log = trader.logs.append
    trader.dry_run = True
    trader.strategy_mode = "fair_value"
    trader.app_state = {"pending_settlements": []}
    trader.fair_value_stake_usd = 3.0
    trader.fair_value_min_taker_edge = 0.10
    trader.fair_value_min_maker_edge = 0.10
    trader.fair_value_min_ev_per_usd = 0.30
    trader.fair_value_taker_guard_min_edge = 0.06
    trader.fair_value_taker_guard_min_ev_per_usd = 0.12
    trader.fair_value_entry_start_seconds = 0.0
    trader.fair_value_entry_end_seconds = 240.0
    trader.fair_value_min_price = 0.35
    trader.fair_value_max_price = 0.50
    trader.fair_value_core_min_price = 0.35
    trader.fair_value_core_max_price = 0.50
    trader.fair_value_maker_wait_seconds = 3.0
    trader.fair_value_sample_seconds = 1.0
    trader.fair_value_model_sensitivity_bps = 28.0
    trader.fair_value_prefer_maker = True
    trader.fair_value_live_trading_enabled = False
    trader.fair_value_core_max_entries_per_window = 2
    trader.fair_value_min_ev_improvement_per_entry = 0.03
    trader.fair_value_momentum_max_entries_per_window = 2
    trader.fair_value_momentum_min_ev_improvement_per_entry = 0.04
    trader.fair_value_late_continuation_enabled = True
    trader.fair_value_late_continuation_start_seconds = 250.0
    trader.fair_value_late_continuation_end_seconds = 300.0
    trader.fair_value_late_continuation_min_abs_delta_bps = 5.0
    trader.fair_value_late_continuation_min_probability = 0.85
    trader.fair_value_late_continuation_min_ev_per_usd = 0.015
    trader.fair_value_late_continuation_price_buffer = 0.01
    trader.fair_value_late_continuation_max_price = 0.95
    trader.fair_value_late_continuation_stake_multiplier = 1.0
    trader.fair_value_late_continuation_max_entries_per_window = 2
    trader.fair_value_late_continuation_min_ev_improvement_per_entry = 0.03
    trader.fair_value_late_continuation_entry_model_min_win_prob = 0.0
    trader.fair_value_giro_probe_enabled = True
    trader.fair_value_giro_probe_start_seconds = 0.0
    trader.fair_value_giro_probe_end_seconds = 300.0
    trader.fair_value_giro_probe_min_price = 0.15
    trader.fair_value_giro_probe_max_price = 0.42
    trader.fair_value_giro_probe_min_abs_delta_bps = 0.5
    trader.fair_value_giro_probe_min_probability = 0.40
    trader.fair_value_giro_probe_min_ev_per_usd = 0.05
    trader.fair_value_giro_probe_min_confidence = 0.10
    trader.fair_value_giro_probe_max_entries_per_window = 2
    trader.fair_value_giro_probe_min_price_step = 0.03
    trader.fair_value_giro_probe_stake_multiplier = 1.0
    trader.fair_value_giro_probe_model_enabled = False
    trader.fair_value_giro_probe_model_path = ""
    trader._cheap_reversal_model = None
    trader._cheap_reversal_model_loaded_path = None
    trader._cheap_reversal_model_loaded_mtime = None
    trader._last_cheap_reversal_model_warn_ts = 0.0
    trader.fair_value_entry_model_enabled = True
    trader.fair_value_core_entry_model_min_win_prob = 0.46
    trader.fair_value_core_entry_model_max_win_prob = 0.54
    trader.fair_value_momentum_entry_model_min_win_prob = 0.63
    trader.fair_value_shadow_dynamic_stake_enabled = True
    trader.fair_value_shadow_dynamic_stake_min_multiplier = 1.0
    trader.fair_value_shadow_dynamic_stake_max_multiplier = 2.0
    trader.fair_value_unresolved_release_seconds = 10.0
    trader.polymarket_min_market_buy_usd = 1.0
    trader.polymarket_min_limit_order_shares = 5.0
    trader.martingale_taker_fee_rate = 0.07
    trader.save_state = lambda: None
    return trader


def test_taker_fee_and_breakeven_probability_near_fifty_cents():
    assert taker_fee_for_stake(100, 0.50, 0.07) == 3.5
    assert math.isclose(taker_breakeven_probability(0.50, 0.07), 0.5175)


def test_shadow_dynamic_stake_scales_momentum_quality_without_changing_base():
    trader = make_fair_trader()

    shadow = trader._fair_value_shadow_dynamic_stake(
        tactic="momentum",
        base_stake_usd=5.0,
        edge_probability=0.08,
        ev_per_usd=0.20,
        entry_model_win_probability=0.78,
        price_margin=0.04,
        continuation_probability=0.0,
    )

    assert shadow["shadow_dynamic_stake_enabled"] is True
    assert shadow["shadow_stake_usd"] == 8.75
    assert shadow["shadow_stake_multiplier"] == 1.75


def test_shadow_dynamic_stake_uses_two_x_only_for_clear_signals():
    trader = make_fair_trader()

    shadow = trader._fair_value_shadow_dynamic_stake(
        tactic="momentum",
        base_stake_usd=5.0,
        edge_probability=0.12,
        ev_per_usd=0.24,
        entry_model_win_probability=0.82,
        price_margin=0.07,
        continuation_probability=0.0,
    )

    assert shadow["shadow_dynamic_stake_enabled"] is True
    assert shadow["shadow_stake_usd"] == 10.0
    assert shadow["shadow_stake_multiplier"] == 2.0


def test_shadow_dynamic_stake_leaves_core_as_fixed_stake():
    trader = make_fair_trader()

    shadow = trader._fair_value_shadow_dynamic_stake(
        tactic="core_edge",
        base_stake_usd=5.0,
        edge_probability=0.20,
        ev_per_usd=0.40,
        entry_model_win_probability=0.90,
        price_margin=0.10,
        continuation_probability=0.0,
    )

    assert shadow["shadow_dynamic_stake_enabled"] is False
    assert shadow["shadow_stake_usd"] == 5.0
    assert shadow["shadow_stake_multiplier"] == 1.0


def test_fair_estimate_moves_with_btc_delta():
    flat = estimate_fair_yes(60000, 60000, elapsed_seconds=120)
    up = estimate_fair_yes(60000, 60060, elapsed_seconds=120)
    down = estimate_fair_yes(60000, 59940, elapsed_seconds=120)

    assert flat["fair_yes"] == 0.5
    assert up["fair_yes"] > 0.5
    assert down["fair_yes"] < 0.5
    assert up["confidence"] > flat["confidence"]


def test_empirical_continuation_probability_strengthens_later_and_farther():
    early_small = empirical_continuation_probability(60, 1.0)
    late_small = empirical_continuation_probability(240, 1.0)
    early_far = empirical_continuation_probability(60, 8.0)

    assert 0.53 < early_small < 0.56
    assert late_small > early_small
    assert early_far > early_small


def test_empirical_fair_values_reversion_but_not_as_favorite():
    # BTC is only slightly below the opening price: Yes is contrarian, but it is
    # still below 50% historically. It may be a value bet only if the quote is cheap.
    small_down = estimate_fair_yes(60000, 59994, elapsed_seconds=60)
    larger_down = estimate_fair_yes(60000, 59970, elapsed_seconds=120)

    assert 0.40 < small_down["fair_yes"] < 0.50
    assert larger_down["fair_yes"] < small_down["fair_yes"]


def test_fair_estimate_blends_with_market_prior():
    raw = estimate_fair_yes(60000, 60300, elapsed_seconds=120)
    blended = blend_fair_yes_with_market(
        raw,
        yes_metrics={"best_bid": 0.35, "best_ask": 0.37},
        no_metrics={"best_bid": 0.63, "best_ask": 0.65},
    )

    assert raw["fair_yes"] > 0.5
    assert blended["market_fair_yes"] < 0.4
    assert blended["fair_yes"] < raw["fair_yes"]
    assert blended["fair_yes"] > blended["market_fair_yes"]


def test_blended_empirical_model_can_find_cheap_contrarian_value():
    raw = estimate_fair_yes(60000, 60004, elapsed_seconds=60)
    blended = blend_fair_yes_with_market(
        raw,
        yes_metrics={"best_bid": 0.60, "best_ask": 0.62},
        no_metrics={"best_bid": 0.37, "best_ask": 0.39},
    )

    # Current BTC is slightly above open, so No is contrarian. The empirical
    # fair No probability remains high enough to compare against a cheap quote.
    assert 1.0 - blended["fair_yes"] > 0.40


def test_best_decision_requires_edge_after_fee():
    no_edge = best_decision(
        fair_yes=0.52,
        yes_metrics={"best_ask": 0.50, "best_bid": 0.49},
        no_metrics={"best_ask": 0.51, "best_bid": 0.50},
        min_taker_edge=0.04,
        min_maker_edge=0.04,
        stake_usd=1.0,
    )
    strong = best_decision(
        fair_yes=0.62,
        yes_metrics={"best_ask": 0.50, "best_bid": 0.49},
        no_metrics={"best_ask": 0.51, "best_bid": 0.50},
        min_taker_edge=0.04,
        min_maker_edge=0.04,
        stake_usd=1.0,
        prefer_maker=False,
    )

    assert not no_edge.should_enter
    assert strong.should_enter
    assert strong.side == "Yes"
    assert strong.route == "taker"
    assert strong.ev_per_usd > 0


def test_best_decision_requires_min_ev_per_usd():
    weak_ev = best_decision(
        fair_yes=0.57,
        yes_metrics={"best_ask": 0.48, "best_bid": 0.47},
        no_metrics={"best_ask": 0.53, "best_bid": 0.52},
        min_taker_edge=0.01,
        min_maker_edge=0.01,
        min_ev_per_usd=0.25,
        stake_usd=3.0,
        prefer_maker=False,
    )
    strong_ev = best_decision(
        fair_yes=0.68,
        yes_metrics={"best_ask": 0.48, "best_bid": 0.47},
        no_metrics={"best_ask": 0.53, "best_bid": 0.52},
        min_taker_edge=0.01,
        min_maker_edge=0.01,
        min_ev_per_usd=0.25,
        stake_usd=3.0,
        prefer_maker=False,
    )

    assert not weak_ev.should_enter
    assert strong_ev.should_enter
    assert strong_ev.ev_per_usd >= 0.25


def test_best_decision_prefers_maker_when_size_allows():
    decision = best_decision(
        fair_yes=0.62,
        yes_metrics={"best_ask": 0.53, "best_bid": 0.50},
        no_metrics={"best_ask": 0.51, "best_bid": 0.48},
        min_taker_edge=0.01,
        min_maker_edge=0.01,
        stake_usd=3.0,
        prefer_maker=True,
        min_limit_shares=5.0,
    )

    assert decision.should_enter
    assert decision.route == "maker"
    assert decision.price == 0.50


def test_best_decision_can_disable_maker_for_momentum():
    decision = best_decision(
        fair_yes=0.62,
        yes_metrics={"best_ask": 0.53, "best_bid": 0.50},
        no_metrics={"best_ask": 0.51, "best_bid": 0.48},
        min_taker_edge=0.01,
        min_maker_edge=0.01,
        stake_usd=3.0,
        prefer_maker=True,
        min_limit_shares=5.0,
        allow_maker=False,
    )

    assert decision.should_enter
    assert decision.route == "taker"


def test_fair_value_taker_guard_blocks_weak_taker_but_keeps_maker():
    trader = make_fair_trader()
    trader.fair_value_min_taker_edge = 0.02
    trader.fair_value_min_maker_edge = 0.02
    trader.fair_value_min_ev_per_usd = 0.03
    state = {"active_slug": "btc-updown-5m-1800000000"}

    weak_taker = trader._fair_value_decision(
        fair_yes=0.56,
        yes_metrics={"best_ask": 0.50, "best_bid": 0.0},
        no_metrics={"best_ask": 0.0, "best_bid": 0.0},
        state=state,
    )
    strong_taker = trader._fair_value_decision(
        fair_yes=0.66,
        yes_metrics={"best_ask": 0.50, "best_bid": 0.0},
        no_metrics={"best_ask": 0.0, "best_bid": 0.0},
        state=state,
    )
    maker = trader._fair_value_decision(
        fair_yes=0.56,
        yes_metrics={"best_ask": 0.0, "best_bid": 0.50},
        no_metrics={"best_ask": 0.0, "best_bid": 0.0},
        state=state,
    )

    assert not weak_taker.should_enter
    assert weak_taker.reason in {"taker_guard_edge", "taker_guard_ev", "no_edge"}
    assert strong_taker.should_enter
    assert strong_taker.route == "taker"
    assert maker.should_enter
    assert maker.route == "maker"


def test_fair_value_state_accepts_multiple_tactics_in_one_window():
    trader = make_fair_trader()
    state = trader._new_fair_value_state()
    state["active_slug"] = "btc-updown-5m-1800000000"
    rows = []
    trader._record_fair_value_entry = rows.append

    trader._fair_value_apply_entry(
        state=state,
        slug=state["active_slug"],
        direction="Yes",
        token_id="yes-token",
        route="taker",
        price=0.50,
        stake_usd=3.0,
        fair_probability=0.58,
        edge_probability=0.06,
        ev_per_usd=0.12,
        elapsed=10.0,
        tactic="core_edge",
    )
    trader._fair_value_apply_entry(
        state=state,
        slug=state["active_slug"],
        direction="Yes",
        token_id="yes-token",
        route="taker",
        price=0.70,
        stake_usd=3.0,
        fair_probability=0.83,
        edge_probability=0.08,
        ev_per_usd=0.18,
        elapsed=250.0,
        tactic="momentum",
    )

    assert state["entry_done"] is True
    assert len(state["entries"]) == 2
    assert state["entries"][0]["tactic"] == "core_edge"
    assert state["entries"][1]["tactic"] == "momentum"
    assert rows[0]["entry_number_in_window"] == 1
    assert rows[1]["entry_number_in_window"] == 2


def test_fair_value_select_decision_returns_decision_then_tactic():
    trader = make_fair_trader()
    core = FairValueDecision(
        should_enter=True,
        side="Yes",
        route="taker",
        price=0.50,
        ev_per_usd=0.10,
    )
    momentum = FairValueDecision(
        should_enter=True,
        side="Yes",
        route="taker",
        price=0.80,
        ev_per_usd=0.20,
    )

    decision, tactic = trader._fair_value_select_decision(250.0, core, momentum)

    assert decision is momentum
    assert tactic == "momentum"


def test_fair_value_select_decision_respects_core_entry_limit():
    trader = make_fair_trader()
    core = FairValueDecision(
        should_enter=True,
        side="Yes",
        route="taker",
        price=0.50,
        ev_per_usd=1.00,
    )
    momentum = FairValueDecision(should_enter=False)
    state = trader._new_fair_value_state()
    state["entries"] = [
        {"tactic": "core_edge"},
        {"tactic": "core_edge"},
    ]

    decision, tactic = trader._fair_value_select_decision(
        100.0,
        core,
        momentum,
        state=state,
    )

    assert not decision.should_enter
    assert tactic == ""


def test_fair_value_select_decision_requires_better_ev_after_first_entry():
    trader = make_fair_trader()
    state = trader._new_fair_value_state()
    state["entries"] = [{"tactic": "core_edge", "ev_per_usd": 0.40}]
    weak = FairValueDecision(
        should_enter=True,
        side="Yes",
        route="taker",
        price=0.35,
        ev_per_usd=0.41,
    )
    strong = FairValueDecision(
        should_enter=True,
        side="Yes",
        route="taker",
        price=0.35,
        ev_per_usd=0.44,
    )

    decision, tactic = trader._fair_value_select_decision(
        100.0,
        weak,
        FairValueDecision(should_enter=False),
        state=state,
    )
    assert not decision.should_enter
    assert tactic == ""

    decision, tactic = trader._fair_value_select_decision(
        100.0,
        strong,
        FairValueDecision(should_enter=False),
        state=state,
    )
    assert decision is strong
    assert tactic == "core_edge"


def test_fair_value_late_continuation_enters_when_price_still_pays():
    trader = make_fair_trader()
    state = trader._new_fair_value_state()
    fair_data = {
        "delta_bps": 12.0,
        "continuation_probability": 0.92,
    }

    decision = trader._fair_value_late_continuation_decision(
        fair_data=fair_data,
        yes_metrics={"best_ask": 0.88, "best_bid": 0.87},
        no_metrics={"best_ask": 0.10, "best_bid": 0.09},
        state=state,
    )

    assert decision.should_enter
    assert decision.side == "Yes"
    assert decision.reason == "late_continuation"
    assert decision.max_acceptable_price > decision.price
    assert decision.continuation_probability == 0.92


def test_fair_value_late_continuation_blocks_overpriced_entries():
    trader = make_fair_trader()
    state = trader._new_fair_value_state()
    fair_data = {
        "delta_bps": -12.0,
        "continuation_probability": 0.91,
    }

    decision = trader._fair_value_late_continuation_decision(
        fair_data=fair_data,
        yes_metrics={"best_ask": 0.04, "best_bid": 0.03},
        no_metrics={"best_ask": 0.93, "best_bid": 0.92},
        state=state,
    )

    assert not decision.should_enter
    assert decision.reason == "late_continuation_no_price_edge"
    assert decision.max_acceptable_price < 0.93


def test_fair_value_giro_probe_rejects_cheap_contrarian_with_low_probability():
    trader = make_fair_trader()
    fair_data = {
        "delta_bps": 5.0,
        "fair_yes": 0.74,
        "fair_no": 0.26,
        "continuation_probability": 0.74,
        "contrarian_probability": 0.26,
        "confidence": 0.20,
    }

    decision = trader._fair_value_giro_probe_decision(
        fair_data=fair_data,
        yes_metrics={"best_ask": 0.75},
        no_metrics={"best_ask": 0.40},
        elapsed=275.0,
    )

    assert not decision.should_enter
    assert decision.reason == "giro_probe_probability_too_low"


def test_fair_value_giro_probe_records_cheap_contrarian_with_enough_probability():
    trader = make_fair_trader()
    probe_state = trader._new_fair_value_giro_probe_state()
    rows = []
    trader._record_fair_value_giro_probe_entry = rows.append
    fair_data = {
        "delta_bps": 5.0,
        "fair_yes": 0.56,
        "fair_no": 0.44,
        "continuation_probability": 0.56,
        "contrarian_probability": 0.44,
        "confidence": 0.20,
    }

    decision = trader._fair_value_giro_probe_decision(
        fair_data=fair_data,
        yes_metrics={"best_ask": 0.75},
        no_metrics={"best_ask": 0.40},
        elapsed=275.0,
    )
    recorded = trader._record_fair_value_giro_probe_signal(
        probe_state=probe_state,
        slug="btc-updown-5m-1800000000",
        decision=decision,
        fair_data=fair_data,
        token_ids=["yes-token", "no-token"],
        elapsed=275.0,
    )

    assert decision.should_enter
    assert decision.side == "No"
    assert decision.reason == "giro_probe_cheap_contrarian"
    assert decision.fair_probability >= trader.fair_value_giro_probe_min_probability
    assert decision.ev_per_usd >= trader.fair_value_giro_probe_min_ev_per_usd
    assert recorded is True
    assert rows[0]["tactic"] == "giro_probe"
    assert probe_state["windows"]["btc-updown-5m-1800000000"][0]["direction"] == "No"


def test_fair_value_giro_probe_uses_cheap_reversal_model(tmp_path):
    trader = make_fair_trader()
    trader.fair_value_giro_probe_min_price = 0.01
    trader.fair_value_giro_probe_max_price = 0.42
    trader.fair_value_giro_probe_min_probability = 0.16
    trader.fair_value_giro_probe_min_ev_per_usd = 0.10
    trader.fair_value_giro_probe_min_confidence = 0.0
    model_path = tmp_path / "cheap_reversal_model.json"
    model_path.write_text(json.dumps({
        "model_type": "logistic_regression_numpy",
        "model_version": "cheap_reversal_test",
        "features": ["price"],
        "medians": {"price": 0.20},
        "scales": {"price": 1.0},
        "weights": {"price": 0.0},
        "bias": -1.0986122886681098,
        "training_metrics": {"rows": 10, "auc": 0.6, "brier": 0.2},
    }), encoding="utf-8")
    trader.fair_value_giro_probe_model_enabled = True
    trader.fair_value_giro_probe_model_path = str(model_path)
    fair_data = {
        "delta_bps": 10.0,
        "fair_yes": 0.95,
        "fair_no": 0.05,
        "raw_fair_yes": 0.95,
        "market_fair_yes": 0.80,
        "continuation_probability": 0.95,
        "contrarian_probability": 0.05,
        "confidence": 0.05,
    }

    decision = trader._fair_value_giro_probe_decision(
        fair_data=fair_data,
        yes_metrics={"best_ask": 0.80},
        no_metrics={"best_ask": 0.20, "best_bid": 0.19, "spread": 0.01, "mid": 0.195},
        elapsed=90.0,
    )

    assert decision.should_enter
    assert decision.reason == "cheap_reversal_model"
    assert decision.cheap_reversal_model_enabled is True
    assert decision.cheap_reversal_model_version == "cheap_reversal_test"
    assert math.isclose(decision.fair_probability, 0.25, abs_tol=1e-6)
    assert math.isclose(decision.cheap_reversal_raw_probability, 0.05, abs_tol=1e-6)


def test_fair_value_giro_probe_records_cheap_reversal_model_fields():
    trader = make_fair_trader()
    probe_state = trader._new_fair_value_giro_probe_state()
    rows = []
    trader._record_fair_value_giro_probe_entry = rows.append
    decision = FairValueDecision(
        should_enter=True,
        side="No",
        route="paper_taker",
        price=0.20,
        fair_probability=0.25,
        edge_probability=0.04,
        ev_per_usd=0.18,
        cheap_reversal_model_probability=0.25,
        cheap_reversal_model_enabled=True,
        cheap_reversal_model_version="cheap_reversal_test",
        cheap_reversal_raw_probability=0.05,
        reason="cheap_reversal_model",
    )

    recorded = trader._record_fair_value_giro_probe_signal(
        probe_state=probe_state,
        slug="btc-updown-5m-1800000000",
        decision=decision,
        fair_data={"delta_bps": 10.0, "confidence": 0.05},
        token_ids=["yes-token", "no-token"],
        elapsed=90.0,
    )

    assert recorded is True
    assert rows[0]["cheap_model_enabled"] is True
    assert rows[0]["cheap_model_probability"] == 0.25
    assert rows[0]["cheap_raw_probability"] == 0.05
    assert rows[0]["cheap_model_version"] == "cheap_reversal_test"


def test_fair_value_giro_probe_is_dry_run_only():
    trader = make_fair_trader()
    trader.dry_run = False

    decision = trader._fair_value_giro_probe_decision(
        fair_data={"delta_bps": -8.0, "fair_yes": 0.30, "fair_no": 0.70},
        yes_metrics={"best_ask": 0.30},
        no_metrics={"best_ask": 0.70},
        elapsed=120.0,
    )

    assert not decision.should_enter
    assert decision.reason == "giro_probe_dry_run_only"


def test_fair_value_state_resets_on_dry_run_change():
    trader = make_fair_trader()
    trader.app_state["fair_value_state"] = {
        "active_slug": "btc-updown-5m-1800000000",
        "entry_done": True,
        "dry_run": True,
    }
    trader.dry_run = False

    trader._reset_fair_value_for_mode_change(True, False)

    state = trader.app_state["fair_value_state"]
    assert state["active_slug"] is None
    assert state["entry_done"] is False
    assert state["dry_run"] is False
    assert any("DryRun changed True -> False" in line for line in trader.logs)


def test_fair_value_reconcile_releases_very_stale_unresolved_window():
    trader = make_fair_trader()
    state = trader._new_fair_value_state()
    state["active_slug"] = "btc-updown-5m-1800000000"
    state["entries"] = [{
        "entry_id": 1,
        "entry_number_in_window": 1,
        "tactic": "core_edge",
        "direction": "Yes",
        "entry_route": "taker",
        "entry_price": 0.35,
        "entry_shares": 8.5714,
        "stake_usd": 3.0,
        "entry_fee_usd": 0.1365,
    }]
    rows = []
    trader._record_fair_value_outcome = rows.append

    async def unresolved(*_args, **_kwargs):
        return False

    trader._settle_active_fair_value_trade = unresolved

    can_continue = asyncio.run(
        trader._reconcile_stale_fair_value_state(
            state,
            "btc-updown-5m-1800000600",
            1800000000 + 300 + 11,
        )
    )

    assert can_continue is True
    assert state["active_slug"] is None
    assert rows[0]["result"] == "UNRESOLVED"
    assert "unresolved_after_10s" in rows[0]["settlement_source"]


def test_fair_value_reconcile_holds_recent_unresolved_window():
    trader = make_fair_trader()
    state = trader._new_fair_value_state()
    state["active_slug"] = "btc-updown-5m-1800000000"
    state["entries"] = [{"entry_id": 1, "direction": "No", "stake_usd": 3.0}]

    async def unresolved(*_args, **_kwargs):
        return False

    trader._settle_active_fair_value_trade = unresolved

    can_continue = asyncio.run(
        trader._reconcile_stale_fair_value_state(
            state,
            "btc-updown-5m-1800000300",
            1800000000 + 300 + 5,
        )
    )

    assert can_continue is False
    assert state["active_slug"] == "btc-updown-5m-1800000000"


def test_fair_value_settlement_uses_chainlink_over_binance_fallback():
    trader = make_fair_trader()
    state = trader._new_fair_value_state()
    state["active_slug"] = "btc-updown-5m-1800000000"
    state["entries"] = [{
        "entry_id": 1,
        "entry_number_in_window": 1,
        "tactic": "core_edge",
        "direction": "Yes",
        "target_token_id": "yes-token",
        "entry_route": "taker",
        "entry_price": 0.50,
        "entry_shares": 2.0,
        "stake_usd": 1.0,
        "entry_fee_usd": 0.0,
    }]
    rows = []
    trader._record_fair_value_outcome = rows.append

    async def official_unavailable(*_args, **_kwargs):
        return None, None

    async def binance_wrong(*_args, **_kwargs):
        return {"open": 60000.0, "close": 59900.0}

    async def chainlink_result(*_args, **_kwargs):
        trader._last_chainlink_settlement_detail = {"source": "chainlink_boundary"}
        return True, 60000.0, 60010.0

    trader._infer_settlement_result = official_unavailable
    trader._binance_btc_5m_context = binance_wrong
    trader._infer_chainlink_window_result = chainlink_result

    settled = asyncio.run(
        trader._settle_active_fair_value_trade(
            state,
            "btc-updown-5m-1800000000",
            1800000301,
        )
    )

    assert settled is True
    assert rows[0]["result"] == "WIN"
    assert rows[0]["settlement_source"] == "chainlink_boundary"


def test_fair_value_settlement_waits_when_official_and_chainlink_missing():
    trader = make_fair_trader()
    state = trader._new_fair_value_state()
    state["active_slug"] = "btc-updown-5m-1800000000"
    state["entries"] = [{
        "entry_id": 1,
        "entry_number_in_window": 1,
        "tactic": "core_edge",
        "direction": "Yes",
        "target_token_id": "yes-token",
        "entry_route": "taker",
        "entry_price": 0.50,
        "entry_shares": 2.0,
        "stake_usd": 1.0,
        "entry_fee_usd": 0.0,
    }]
    rows = []
    trader._record_fair_value_outcome = rows.append

    async def official_unavailable(*_args, **_kwargs):
        return None, None

    async def binance_available(*_args, **_kwargs):
        return {"open": 60000.0, "close": 60100.0}

    async def chainlink_unavailable(*_args, **_kwargs):
        return None, None, None

    trader._infer_settlement_result = official_unavailable
    trader._binance_btc_5m_context = binance_available
    trader._infer_chainlink_window_result = chainlink_unavailable

    settled = asyncio.run(
        trader._settle_active_fair_value_trade(
            state,
            "btc-updown-5m-1800000000",
            1800000301,
        )
    )

    assert settled is False
    assert rows == []
    assert state["active_slug"] == "btc-updown-5m-1800000000"
    assert any("Waiting for official/Chainlink settlement" in line for line in trader.logs)


class _FakeToolContent:
    def __init__(self, text):
        self.text = text


class _FakeToolResponse:
    def __init__(self, text):
        self.content = [_FakeToolContent(text)]


class _FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def call_tool(self, name, arguments=None):
        self.calls.append((name, arguments or {}))
        if not self.responses:
            raise AssertionError(f"unexpected call_tool({name})")
        return _FakeToolResponse(self.responses.pop(0))


def test_fair_value_live_maker_order_records_real_fill():
    trader = make_fair_trader()
    trader.dry_run = False
    trader.fair_value_live_trading_enabled = True
    trader.fair_value_maker_wait_seconds = 0.0
    events = []
    trader._record_fair_value_maker_event = events.append
    session = _FakeSession([
        '{"orderID":"0xabc","status":"filled","price":"0.50"}',
    ])

    ok, text, filled_usd, filled_shares, avg_price, route = asyncio.run(
        trader._fair_value_try_live_entry(
            session=session,
            slug="btc-updown-5m-1800000000",
            token_id="token-yes",
            direction="Yes",
            route="maker",
            price=0.50,
            stake_usd=5.0,
            fair_probability=0.56,
            edge_probability=0.06,
            ev_per_usd=0.12,
            tactic="core_edge",
            elapsed=12.0,
        )
    )

    assert ok is True
    assert route == "maker_live"
    assert filled_usd == 5.0
    assert filled_shares == 10.0
    assert avg_price == 0.5
    assert session.calls[0][0] == "place_order"
    assert session.calls[0][1]["post_only"] is True
    assert session.calls[0][1]["order_type"] == "GTC"
    assert [event["event"] for event in events] == ["created_live", "filled_live"]


def test_fair_value_live_maker_order_cancels_unfilled_order():
    trader = make_fair_trader()
    trader.dry_run = False
    trader.fair_value_live_trading_enabled = True
    trader.fair_value_maker_wait_seconds = 0.0
    events = []
    trader._record_fair_value_maker_event = events.append
    session = _FakeSession([
        '{"orderID":"0xabc","status":"live","price":"0.50"}',
        '{"success":true}',
    ])

    ok, text, filled_usd, filled_shares, avg_price, route = asyncio.run(
        trader._fair_value_try_live_entry(
            session=session,
            slug="btc-updown-5m-1800000000",
            token_id="token-yes",
            direction="Yes",
            route="maker",
            price=0.50,
            stake_usd=5.0,
            fair_probability=0.56,
            edge_probability=0.06,
            ev_per_usd=0.12,
            tactic="core_edge",
            elapsed=12.0,
        )
    )

    assert ok is False
    assert "not filled" in text
    assert filled_usd is None
    assert filled_shares is None
    assert avg_price is None
    assert route == "maker"
    assert [name for name, _ in session.calls] == ["place_order", "cancel_order"]
    assert [event["event"] for event in events] == ["created_live", "expired_live"]


def test_fair_value_request_exception_registers_fast_retry_without_consuming_maker():
    trader = make_fair_trader()
    state = trader._new_fair_value_state()

    trader._fair_value_register_order_error(
        state,
        "btc-updown-5m-1800000000",
        "Error placing order: PolyApiException[status_code=None, error_message=Request exception!]",
    )

    assert state["consecutive_order_errors"] == 1
    assert state["maker_attempted_slug"] != "btc-updown-5m-1800000000"
    assert state["order_error_cooldown_until"] > 0
    assert any("Order retry delay 1s" in line for line in trader.logs)
