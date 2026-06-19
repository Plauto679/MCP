import asyncio
import datetime
import json
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from src.orchestrator import CopyTrader


def make_trader():
    trader = CopyTrader.__new__(CopyTrader)
    trader.logs = []
    trader.log = trader.logs.append
    trader.strategy_mode = "martingale"
    trader.martingale_initial_amount = 1.0
    trader.martingale_entry_start_seconds = 0
    trader.martingale_entry_end_seconds = 265
    trader.martingale_min_entry_price = 0.4
    trader.martingale_max_entry_price = 0.6
    trader.martingale_mid_entry_start_seconds = 120
    trader.martingale_mid_min_entry_price = 0.45
    trader.martingale_mid_max_entry_price = 0.55
    trader.martingale_late_entry_start_seconds = 240
    trader.martingale_late_min_entry_price = 0.48
    trader.martingale_late_max_entry_price = 0.52
    trader.martingale_recovery_entry_end_seconds = 285
    trader.martingale_recovery_min_entry_price = 0.4
    trader.martingale_recovery_max_entry_price = 0.6
    trader.martingale_late_recovery_start_seconds = 240
    trader.martingale_late_recovery_min_entry_price = 0.48
    trader.martingale_late_recovery_max_entry_price = 0.52
    trader._chainlink_price_samples = {}
    trader._martingale_entry_plans = {}
    trader._martingale_prefetch_attempts = {}
    trader._martingale_market_cache = {}
    trader._background_tasks = set()
    trader.martingale_taker_fee_rate = 0.07
    trader.martingale_balance_floor_fraction = 0.50
    trader.martingale_order_type = "FOK"
    trader.martingale_chainlink_boundary_tolerance_seconds = 1.5
    trader.martingale_chainlink_min_decision_margin = 10.0
    trader.martingale_maker_wait_seconds = 5.0
    trader.martingale_maker_price_offset = 0.01
    trader.martingale_maker_sample_seconds = 0.5
    trader.martingale_maker_record_seconds = 15.0
    trader.martingale_maker_blind_price = 0.50
    trader.martingale_maker_blind_end_seconds = 15.0
    trader.data_dir = "."
    trader.polymarket_min_market_buy_usd = 1.0
    trader.polymarket_min_limit_order_shares = 5.0
    trader.dry_run = True
    return trader


def test_chainlink_messages_keep_exact_boundary_samples():
    trader = make_trader()
    trader._ingest_chainlink_message(json.dumps({
        "payload": {
            "data": [
                {"timestamp": 1_800_000_000_000, "value": 60_000.0},
                {"timestamp": 1_800_000_300_000, "value": 60_010.0},
            ],
        },
    }))
    trader._ingest_chainlink_message(json.dumps({
        "topic": "crypto_prices_chainlink",
        "payload": {
            "symbol": "btc/usd",
            "timestamp": 1_800_000_301_000,
            "value": 60_011.0,
        },
    }))

    assert trader._chainlink_boundary_price(1_800_000_000) == 60_000.0
    assert trader._chainlink_boundary_price(1_800_000_300) == 60_010.0
    assert trader._chainlink_boundary_price(1_800_000_301) == 60_011.0


def test_chainlink_result_uses_exact_open_and_close():
    trader = make_trader()
    slug = "btc-updown-5m-1800000000"
    trader._record_chainlink_price(1_800_000_000_000, 60_000.0)
    trader._record_chainlink_price(1_800_000_300_000, 60_000.0)

    up_result = asyncio.run(
        trader._infer_chainlink_window_result(slug, "Yes", wait_seconds=0)
    )
    down_result = asyncio.run(
        trader._infer_chainlink_window_result(slug, "No", wait_seconds=0)
    )

    assert up_result == (True, 60_000.0, 60_000.0)
    assert down_result == (False, 60_000.0, 60_000.0)


def test_chainlink_result_uses_nearby_boundary_sample_when_clear():
    trader = make_trader()
    slug = "btc-updown-5m-1800000000"
    trader._record_chainlink_price(1_800_000_000_000, 60_000.0)
    trader._record_chainlink_price(1_800_000_299_000, 60_100.0)

    result = asyncio.run(
        trader._infer_chainlink_window_result(slug, "Yes", wait_seconds=0)
    )

    assert result == (True, 60_000.0, 60_100.0)
    assert trader._last_chainlink_settlement_detail["source"] == "chainlink_tolerant"
    assert trader._last_chainlink_settlement_detail["closing_offset_seconds"] == -1.0


def test_chainlink_result_does_not_guess_near_tie_from_nearby_sample():
    trader = make_trader()
    slug = "btc-updown-5m-1800000000"
    trader._record_chainlink_price(1_800_000_000_000, 60_000.0)
    trader._record_chainlink_price(1_800_000_299_000, 60_004.0)

    result = asyncio.run(
        trader._infer_chainlink_window_result(slug, "Yes", wait_seconds=0)
    )

    assert result == (None, 60_000.0, 60_004.0)
    assert trader._last_chainlink_settlement_detail["source"] == "chainlink_ambiguous"


def test_chainlink_result_uses_latest_post_close_when_boundary_missing():
    trader = make_trader()
    slug = "btc-updown-5m-1800000000"
    trader._record_chainlink_price(1_800_000_000_000, 60_000.0)
    trader._record_chainlink_price(1_800_000_302_000, 60_020.0)

    result = asyncio.run(
        trader._infer_chainlink_window_result(slug, "Yes", wait_seconds=0)
    )

    assert result == (True, 60_000.0, 60_020.0)
    assert trader._last_chainlink_settlement_detail["source"] == "chainlink_latest"
    assert trader._last_chainlink_settlement_detail["closing_offset_seconds"] == 2.0


def test_chainlink_result_uses_latest_post_close_over_ambiguous_nearby_sample():
    trader = make_trader()
    slug = "btc-updown-5m-1800000000"
    trader._record_chainlink_price(1_800_000_000_000, 60_000.0)
    trader._record_chainlink_price(1_800_000_299_000, 60_004.0)
    trader._record_chainlink_price(1_800_000_302_000, 59_990.0)

    result = asyncio.run(
        trader._infer_chainlink_window_result(slug, "Yes", wait_seconds=0)
    )

    assert result == (False, 60_000.0, 59_990.0)
    assert trader._last_chainlink_settlement_detail["source"] == "chainlink_latest"
    assert trader._last_chainlink_settlement_detail["closing_offset_seconds"] == 2.0


def test_next_window_plan_prepares_win_and_loss_paths():
    trader = make_trader()
    market_data = {
        "markets": [{
            "clobTokenIds": json.dumps(["up-token", "down-token"]),
        }],
    }

    async def get_market_by_slug(*args, **kwargs):
        return market_data

    trader.get_market_by_slug = get_market_by_slug
    state = {
        "direction": "No",
        "streak": 2,
        "loss_bank": 2.0,
        "entry_shares": 3.0,
        "entry_price": 0.5,
    }

    plan = asyncio.run(
        trader._prepare_martingale_entry_plan(
            state,
            "btc-updown-5m-1800000300",
            now_ts=1_800_000_290,
        )
    )

    assert plan["token_ids"] == ["up-token", "down-token"]
    assert plan["win"] == {
        "direction": None,
        "streak": 0,
        "loss_bank": 0.0,
        "recovery_target": 1.0,
    }
    assert plan["loss"]["direction"] == "No"
    assert plan["loss"]["streak"] == 3
    assert plan["loss"]["loss_bank"] == pytest.approx(3.5525)
    assert plan["loss"]["recovery_target"] == pytest.approx(4.5525)


def test_fast_settlement_prefers_chainlink_and_updates_recovery():
    trader = make_trader()
    trader.save_state = lambda: None
    trader._record_martingale_trade_row = lambda **kwargs: None

    async def chainlink_result(*args, **kwargs):
        return False, 60_000.0, 59_990.0

    async def official_result(*args, **kwargs):
        raise AssertionError("Official fallback should not run when Chainlink is available")

    trader._infer_chainlink_window_result = chainlink_result
    trader._infer_settlement_result = official_result
    state = {
        "active_slug": "btc-updown-5m-1800000000",
        "entry_done": True,
        "streak": 1,
        "current_amount": 1.5,
        "direction": "Yes",
        "entry_price": 0.5,
        "target_token_id": "up-token",
        "entry_shares": 3.0,
        "loss_bank": 2.0,
        "entry_retry_after": 0.0,
        "entry_attempts": 0,
        "skip_logged": False,
        "opened_at": None,
    }

    settled = asyncio.run(
        trader._settle_active_martingale_trade(
            state,
            state["active_slug"],
            trader._start_time_from_btc_slug(state["active_slug"]),
            now_ts=1_800_000_300,
        )
    )

    assert settled is True
    assert state["streak"] == 2
    assert state["loss_bank"] == pytest.approx(3.5525)
    assert state["direction"] == "Yes"
    assert state["entry_done"] is False
    assert any("settlement=chainlink_boundary" in line for line in trader.logs)


def test_excel_recording_does_not_block_fast_settlement():
    trader = make_trader()
    trader.save_state = lambda: None

    def slow_record(**kwargs):
        time.sleep(0.25)

    async def chainlink_result(*args, **kwargs):
        return True, 60_000.0, 60_010.0

    trader._record_martingale_trade_row = slow_record
    trader._infer_chainlink_window_result = chainlink_result
    state = {
        "active_slug": "btc-updown-5m-1800000000",
        "entry_done": True,
        "streak": 0,
        "current_amount": 1.0,
        "direction": "Yes",
        "entry_price": 0.5,
        "target_token_id": "up-token",
        "entry_shares": 2.0,
        "loss_bank": 0.0,
        "entry_retry_after": 0.0,
        "entry_attempts": 0,
        "skip_logged": False,
        "opened_at": None,
    }

    async def run_settlement():
        started = time.perf_counter()
        settled = await trader._settle_active_martingale_trade(
            state,
            state["active_slug"],
            trader._start_time_from_btc_slug(state["active_slug"]),
            now_ts=1_800_000_300,
        )
        elapsed = time.perf_counter() - started
        await trader._drain_background_tasks()
        return settled, elapsed

    settled, elapsed = asyncio.run(run_settlement())

    assert settled is True
    assert elapsed < 0.1


def test_unresolved_settlement_does_not_guess_or_mutate_state():
    trader = make_trader()

    async def no_chainlink(*args, **kwargs):
        return None, 60_000.0, None

    async def no_official(*args, **kwargs):
        return None, None

    trader._infer_chainlink_window_result = no_chainlink
    trader._infer_settlement_result = no_official
    state = {
        "entry_done": True,
        "direction": "Yes",
        "target_token_id": "up-token",
        "streak": 4,
        "loss_bank": 12.0,
    }
    before = state.copy()

    settled = asyncio.run(
        trader._settle_active_martingale_trade(
            state,
            "btc-updown-5m-1800000000",
            trader._start_time_from_btc_slug("btc-updown-5m-1800000000"),
            now_ts=1_800_000_300,
        )
    )

    assert settled is False
    assert state == before


def test_recovery_uses_normal_price_range_after_early_window():
    trader = make_trader()
    trader.martingale_entry_end_seconds = 15
    trader.martingale_min_entry_price = 0.4
    trader.martingale_max_entry_price = 0.6
    trader.martingale_recovery_min_entry_price = 0.4
    trader.martingale_recovery_max_entry_price = 0.6
    state = {
        "streak": 2,
        "loss_bank": 3.0,
    }

    assert trader._entry_price_bounds(state, elapsed=60) == (0.4, 0.6)


def test_normal_entry_price_bounds_tighten_by_elapsed():
    trader = make_trader()
    state = {
        "streak": 0,
        "loss_bank": 0.0,
    }

    assert trader._entry_price_bounds(state, elapsed=0) == (0.4, 0.6)
    assert trader._entry_price_bounds(state, elapsed=119.9) == (0.4, 0.6)
    assert trader._entry_price_bounds(state, elapsed=120) == (0.45, 0.55)
    assert trader._entry_price_bounds(state, elapsed=239.9) == (0.45, 0.55)
    assert trader._entry_price_bounds(state, elapsed=240) == (0.48, 0.52)
    assert trader._entry_price_bounds(state, elapsed=264.9) == (0.48, 0.52)


def test_late_recovery_uses_tighter_price_range():
    trader = make_trader()
    trader.martingale_entry_end_seconds = 15
    state = {
        "streak": 2,
        "loss_bank": 3.0,
    }

    assert trader._entry_price_bounds(state, elapsed=239) == (0.4, 0.6)
    assert trader._entry_price_bounds(state, elapsed=240) == (0.48, 0.52)
    assert trader._entry_price_bounds({"streak": 0, "loss_bank": 0.0}, elapsed=240) == (0.48, 0.52)


def test_recovery_cutoff_preserves_losses_and_direction_for_next_window():
    trader = make_trader()
    trader.running = True
    trader.dry_run = True
    trader.debug_logs = False
    trader.poll_interval = 1
    trader.martingale_entry_start_seconds = 0
    trader.martingale_entry_end_seconds = 15
    trader.martingale_recovery_entry_end_seconds = 120
    trader.martingale_min_entry_price = 0.4
    trader.martingale_max_entry_price = 0.6
    trader.martingale_recovery_min_entry_price = 0.4
    trader.martingale_recovery_max_entry_price = 0.6
    trader.polymarket_min_market_buy_usd = 1.0
    trader._ensure_chainlink_price_feed = lambda: None
    trader.save_state = lambda: None

    current_slug = "btc-updown-5m-1800000300"
    current_start = datetime.datetime.fromtimestamp(
        1_800_000_300,
        datetime.timezone.utc,
    )
    market_data = {
        "start_time": current_start,
        "markets": [{"clobTokenIds": ["up-token", "down-token"]}],
    }

    async def get_market_by_slug(slug, **kwargs):
        assert slug == current_slug
        return market_data

    async def fetch_buy_price(token_id):
        raise AssertionError("Price should not be fetched after the recovery cutoff")

    trader.get_market_by_slug = get_market_by_slug
    trader.fetch_buy_price = fetch_buy_price
    trader.app_state = {
        "pending_settlements": [],
        "martingale_state": {
            "active_slug": current_slug,
            "entry_done": False,
            "streak": 2,
            "current_amount": 0.0,
            "direction": "No",
            "entry_price": 0.0,
            "target_token_id": None,
            "entry_shares": 0.0,
            "loss_bank": 3.0,
            "entry_retry_after": 0.0,
            "entry_attempts": 0,
            "skip_logged": False,
            "opened_at": None,
        },
    }

    with patch("src.orchestrator.time.time", return_value=1_800_000_421):
        asyncio.run(trader.run_martingale_strategy(session=None))

    state = trader.app_state["martingale_state"]
    assert state["entry_done"] is False
    assert state["streak"] == 2
    assert state["loss_bank"] == 3.0
    assert state["direction"] == "No"
    assert state["skip_logged"] is True
    assert any("Recovery entry missed" in line for line in trader.logs)


def test_boundary_settlement_and_next_entry_happen_in_same_tick():
    trader = make_trader()
    trader.running = True
    trader.dry_run = True
    trader.debug_logs = False
    trader.martingale_entry_start_seconds = 0
    trader.martingale_entry_end_seconds = 15
    trader.martingale_recovery_entry_end_seconds = 120
    trader.martingale_min_entry_price = 0.4
    trader.martingale_max_entry_price = 0.6
    trader.martingale_recovery_min_entry_price = 0.4
    trader.martingale_recovery_max_entry_price = 0.6
    trader.polymarket_min_market_buy_usd = 1.0
    trader._last_unresolved_martingale_slug = None
    trader._ensure_chainlink_price_feed = lambda: None
    trader.save_state = lambda: None
    trader._record_martingale_trade_row = lambda **kwargs: None

    previous_slug = "btc-updown-5m-1800000000"
    current_slug = "btc-updown-5m-1800000300"
    current_start = datetime.datetime.fromtimestamp(
        1_800_000_300,
        datetime.timezone.utc,
    )
    market_data = {
        "start_time": current_start,
        "markets": [{"clobTokenIds": ["up-token", "down-token"]}],
    }

    async def get_market_by_slug(slug, **kwargs):
        assert slug == current_slug
        return market_data

    async def fetch_buy_price(token_id):
        assert token_id == "up-token"
        return 0.5

    trader.get_market_by_slug = get_market_by_slug
    trader.fetch_buy_price = fetch_buy_price
    trader._record_chainlink_price(1_800_000_000_000, 60_000.0)
    trader._record_chainlink_price(1_800_000_300_000, 59_990.0)
    trader.app_state = {
        "pending_settlements": [],
        "martingale_session_stats": {
            "trades": 0,
            "wins": 0,
            "losses": 0,
            "cum_pnl_usd": 0.0,
            "started_at": "2026-06-10T00:00:00+00:00",
        },
        "martingale_state": {
            "active_slug": previous_slug,
            "entry_done": True,
            "streak": 0,
            "current_amount": 1.0,
            "direction": "Yes",
            "entry_price": 0.5,
            "target_token_id": "up-token",
            "entry_shares": 2.0,
            "loss_bank": 0.0,
            "entry_retry_after": 0.0,
            "entry_attempts": 0,
            "skip_logged": False,
            "opened_at": None,
        },
    }

    with patch("src.orchestrator.time.time", return_value=1_800_000_300.5):
        asyncio.run(trader.run_martingale_strategy(session=None))

    state = trader.app_state["martingale_state"]
    assert state["active_slug"] == current_slug
    assert state["entry_done"] is True
    assert state["streak"] == 1
    assert state["loss_bank"] == pytest.approx(1.035)
    assert state["current_amount"] == 2.11
    assert state["entry_shares"] == 4.22
    exit_index = next(i for i, line in enumerate(trader.logs) if "[Martingale] EXIT" in line)
    entry_index = next(i for i, line in enumerate(trader.logs) if "[Martingale] ENTRY" in line)
    assert exit_index < entry_index


def test_fee_aware_stake_targets_net_profit():
    trader = make_trader()
    state = {"loss_bank": 1.035}

    stake = trader._martingale_stake_for_price(state, price=0.5)
    shares = stake / 0.5
    fee = trader._martingale_fee_for_fill(shares, 0.5)
    net_profit = trader._martingale_win_net_profit(shares, stake, fee)

    assert stake == 2.11
    assert net_profit == pytest.approx(2.035, abs=0.01)


def test_dry_run_change_resets_martingale_state():
    trader = make_trader()
    trader.target_wallets = []
    trader.poll_interval = 1
    trader.size_mode = "fixed"
    trader.size_value = 1.0
    trader.winning_strategy_enabled = False
    trader.winning_price_threshold = 0.75
    trader.winning_entry_time = 3.0
    trader.winning_exit_time = 4.5
    trader.strategy_mode = "martingale"
    trader.winning_size_mode = "fixed"
    trader.winning_size_value = 10.0
    trader.history = []
    trader.save_state = lambda: None
    trader.app_state = {
        "pending_settlements": [{"is_martingale": True}, {"is_martingale": False}],
        "martingale_state": {
            "active_slug": "btc-updown-5m-1800000000",
            "entry_done": True,
            "streak": 4,
            "loss_bank": 15.0,
            "direction": "Yes",
            "dry_run": True,
        },
    }

    trader.update_config(dry_run=False)

    state = trader.app_state["martingale_state"]
    assert state["dry_run"] is False
    assert state["active_slug"] is None
    assert state["streak"] == 0
    assert state["loss_bank"] == 0.0
    assert trader.app_state["pending_settlements"] == [{"is_martingale": False}]
    assert any("DryRun changed True -> False" in line for line in trader.logs)


def test_strategy_change_to_martingale_maker_resets_previous_cycle():
    trader = make_trader()
    trader.target_wallets = []
    trader.poll_interval = 1
    trader.size_mode = "fixed"
    trader.size_value = 1.0
    trader.winning_strategy_enabled = False
    trader.winning_price_threshold = 0.75
    trader.winning_entry_time = 3.0
    trader.winning_exit_time = 4.5
    trader.strategy_mode = "martingale"
    trader.winning_size_mode = "fixed"
    trader.winning_size_value = 10.0
    trader.history = []
    trader.save_state = lambda: None
    trader.app_state = {
        "pending_settlements": [{"is_martingale": True}],
        "martingale_state": {
            "active_slug": "btc-updown-5m-1800000000",
            "entry_done": True,
            "streak": 3,
            "loss_bank": 7.0,
            "direction": "No",
            "dry_run": True,
        },
    }

    trader.update_config(strategy_mode="martingale_maker")

    state = trader.app_state["martingale_state"]
    assert state["active_slug"] is None
    assert state["streak"] == 0
    assert state["loss_bank"] == 0.0
    assert trader.app_state["pending_settlements"] == []
    assert any("Strategy changed martingale -> martingale_maker" in line for line in trader.logs)


def test_balance_guard_blocks_stake_below_cycle_floor():
    trader = make_trader()
    trader.dry_run = False
    state = {"cycle_start_balance": 2100.0}

    class FakeSession:
        async def call_tool(self, name):
            assert name == "get_balance"
            return SimpleNamespace(
                content=[SimpleNamespace(text=json.dumps({"balance": 1600.0}))]
            )

    allowed, balance, fee, floor, projected = asyncio.run(
        trader._martingale_balance_guard(
            session=FakeSession(),
            state=state,
            stake_usd=600.0,
            entry_price=0.5,
        )
    )

    assert allowed is False
    assert balance == 1600.0
    assert fee == pytest.approx(21.0)
    assert floor == 1050.0
    assert projected == pytest.approx(979.0)
    assert any("Safety stop" in line for line in trader.logs)


def test_maker_candidate_price_stays_post_only():
    trader = make_trader()
    metrics = {"best_bid": 0.49, "best_ask": 0.52}

    price = trader._maker_candidate_price(
        metrics,
        reference_price=0.52,
        min_price=0.4,
        max_price=0.6,
    )

    assert price == 0.5


def test_martingale_maker_dry_run_simulates_maker_fill_on_touch():
    trader = make_trader()
    trader.strategy_mode = "martingale_maker"
    trader.dry_run = True
    trader.martingale_maker_wait_seconds = 0.2
    trader.martingale_maker_sample_seconds = 0.05
    rows = []
    trader._schedule_csv_row = lambda filename, row: rows.append((filename, row))

    calls = {"up-token": 0, "down-token": 0}

    async def fetch_order_book(token_id):
        calls[token_id] += 1
        if token_id == "up-token" and calls[token_id] == 1:
            return {
                "bids": [{"price": "0.49", "size": "100"}],
                "asks": [{"price": "0.52", "size": "100"}],
            }
        if token_id == "up-token":
            return {
                "bids": [{"price": "0.49", "size": "100"}],
                "asks": [{"price": "0.50", "size": "10"}],
            }
        return {
            "bids": [{"price": "0.48", "size": "100"}],
            "asks": [{"price": "0.53", "size": "100"}],
        }

    trader.fetch_order_book = fetch_order_book
    state = {"direction": "Yes"}
    start_time = datetime.datetime.fromtimestamp(1_800_000_000, datetime.timezone.utc)

    result = asyncio.run(
        trader._try_maker_entry_probe(
            session=None,
            slug="btc-updown-5m-1800000000",
            start_time=start_time,
            elapsed=0.0,
            entry_end_seconds=15,
            state=state,
            token_ids=["up-token", "down-token"],
            target_token_id="up-token",
            entry_price=0.52,
            usd_to_buy=3.0,
            min_entry_price=0.4,
            max_entry_price=0.6,
        )
    )

    assert result["attempted"] is True
    assert result["filled"] is True
    assert result["route"] == "maker_simulated"
    assert result["avg_entry_price"] == 0.5
    assert result["entry_fee_usd"] == 0.0
    assert any(filename == "martingale_maker_orderbook_samples.csv" for filename, _ in rows)
    assert any(filename == "martingale_maker_entry_events.csv" for filename, _ in rows)


def test_martingale_maker_blind_probe_can_fill_without_taker_price():
    trader = make_trader()
    trader.strategy_mode = "martingale_maker"
    trader.dry_run = True
    trader.martingale_maker_blind_end_seconds = 0.05
    trader.martingale_maker_sample_seconds = 0.01
    trader._schedule_csv_row = lambda *_args, **_kwargs: None
    trader.fetch_buy_price = lambda _token_id: asyncio.sleep(0, result=0.0)

    async def fetch_order_book(token_id):
        if token_id == "up-token":
            return {
                "bids": [{"price": "0.49", "size": "100"}],
                "asks": [{"price": "0.50", "size": "10"}],
            }
        return {
            "bids": [{"price": "0.49", "size": "100"}],
            "asks": [{"price": "0.52", "size": "100"}],
        }

    trader.fetch_order_book = fetch_order_book

    result = asyncio.run(
        trader._try_maker_entry_probe(
            session=None,
            slug="btc-updown-5m-1800000000",
            start_time=datetime.datetime.fromtimestamp(
                1_800_000_000,
                datetime.timezone.utc,
            ),
            elapsed=0.0,
            entry_end_seconds=15,
            state={"direction": "Yes"},
            token_ids=["up-token", "down-token"],
            target_token_id="up-token",
            entry_price=0.0,
            usd_to_buy=3.0,
            min_entry_price=0.4,
            max_entry_price=0.6,
            blind=True,
        )
    )

    assert result["attempted"] is True
    assert result["blind"] is True
    assert result["filled"] is True
    assert result["fallback"] is False
    assert result["avg_entry_price"] == 0.5
    assert result["entry_fee_usd"] == 0.0


def test_martingale_maker_blind_probe_waits_when_taker_price_stays_missing():
    trader = make_trader()
    trader.strategy_mode = "martingale_maker"
    trader.dry_run = True
    trader.martingale_maker_blind_end_seconds = 0.02
    trader.martingale_maker_sample_seconds = 0.01
    trader._schedule_csv_row = lambda *_args, **_kwargs: None
    trader.fetch_buy_price = lambda _token_id: asyncio.sleep(0, result=0.0)

    async def fetch_order_book(_token_id):
        return {
            "bids": [{"price": "0.30", "size": "100"}],
            "asks": [{"price": "0.80", "size": "100"}],
        }

    trader.fetch_order_book = fetch_order_book

    result = asyncio.run(
        trader._try_maker_entry_probe(
            session=None,
            slug="btc-updown-5m-1800000000",
            start_time=datetime.datetime.fromtimestamp(
                1_800_000_000,
                datetime.timezone.utc,
            ),
            elapsed=0.0,
            entry_end_seconds=15,
            state={"direction": "Yes"},
            token_ids=["up-token", "down-token"],
            target_token_id="up-token",
            entry_price=0.0,
            usd_to_buy=3.0,
            min_entry_price=0.4,
            max_entry_price=0.6,
            blind=True,
        )
    )

    assert result["attempted"] is True
    assert result["blind"] is True
    assert result["filled"] is False
    assert result["fallback_entry_price"] is None
    assert any("taker price is still unavailable" in line for line in trader.logs)


def test_martingale_maker_blind_probe_uses_taker_fallback_when_price_appears():
    trader = make_trader()
    trader.strategy_mode = "martingale_maker"
    trader.dry_run = True
    trader.martingale_maker_blind_end_seconds = 0.05
    trader.martingale_maker_sample_seconds = 0.01
    trader._schedule_csv_row = lambda *_args, **_kwargs: None

    async def fetch_order_book(_token_id):
        return {
            "bids": [{"price": "0.30", "size": "100"}],
            "asks": [{"price": "0.80", "size": "100"}],
        }

    async def fetch_buy_price(_token_id):
        return 0.49

    trader.fetch_order_book = fetch_order_book
    trader.fetch_buy_price = fetch_buy_price

    result = asyncio.run(
        trader._try_maker_entry_probe(
            session=None,
            slug="btc-updown-5m-1800000000",
            start_time=datetime.datetime.fromtimestamp(
                1_800_000_000,
                datetime.timezone.utc,
            ),
            elapsed=0.0,
            entry_end_seconds=15,
            state={"direction": "Yes"},
            token_ids=["up-token", "down-token"],
            target_token_id="up-token",
            entry_price=0.0,
            usd_to_buy=3.0,
            min_entry_price=0.4,
            max_entry_price=0.6,
            blind=True,
        )
    )

    assert result["attempted"] is True
    assert result["filled"] is False
    assert result["fallback_entry_price"] == 0.49
    assert any("using normal fallback" in line for line in trader.logs)


def test_martingale_maker_skips_maker_when_limit_size_is_too_small():
    trader = make_trader()
    trader.strategy_mode = "martingale_maker"
    trader.dry_run = False
    trader.martingale_maker_wait_seconds = 5.0
    rows = []
    trader._schedule_csv_row = lambda filename, row: rows.append((filename, row))

    async def fetch_order_book(_token_id):
        return {
            "bids": [{"price": "0.47", "size": "100"}],
            "asks": [{"price": "0.50", "size": "100"}],
        }

    trader.fetch_order_book = fetch_order_book

    result = asyncio.run(
        trader._try_maker_entry_probe(
            session=None,
            slug="btc-updown-5m-1800000000",
            start_time=datetime.datetime.fromtimestamp(
                1_800_000_000,
                datetime.timezone.utc,
            ),
            elapsed=0.0,
            entry_end_seconds=265,
            state={"direction": "No"},
            token_ids=["up-token", "down-token"],
            target_token_id="down-token",
            entry_price=0.48,
            usd_to_buy=1.0,
            min_entry_price=0.4,
            max_entry_price=0.6,
        )
    )

    assert result["attempted"] is False
    assert result["filled"] is False
    assert result["route"] == "taker_maker_min_size"
    assert any("below min=5.00" in line for line in trader.logs)
    assert rows[-1][1]["route"] == "taker_maker_min_size"


def test_martingale_maker_live_places_post_only_and_detects_fill():
    trader = make_trader()
    trader.strategy_mode = "martingale_maker"
    trader.dry_run = False
    trader.martingale_maker_wait_seconds = 0.05
    trader.martingale_maker_sample_seconds = 0.01
    trader._schedule_csv_row = lambda *_args, **_kwargs: None

    async def fetch_order_book(_token_id):
        return {
            "bids": [{"price": "0.49", "size": "100"}],
            "asks": [{"price": "0.52", "size": "100"}],
        }

    trader.fetch_order_book = fetch_order_book

    calls = []

    class FakeSession:
        async def call_tool(self, name, arguments=None):
            calls.append((name, arguments or {}))
            if name == "place_order":
                return SimpleNamespace(content=[SimpleNamespace(text=json.dumps({
                    "orderID": "0xmaker",
                    "status": "live",
                }))])
            if name == "get_order":
                return SimpleNamespace(content=[SimpleNamespace(text=json.dumps({
                    "id": "0xmaker",
                    "status": "filled",
                    "size_matched": "6.0",
                    "price": "0.50",
                }))])
            raise AssertionError(f"unexpected tool {name}")

    result = asyncio.run(
        trader._try_maker_entry_probe(
            session=FakeSession(),
            slug="btc-updown-5m-1800000000",
            start_time=datetime.datetime.fromtimestamp(
                1_800_000_000,
                datetime.timezone.utc,
            ),
            elapsed=0.0,
            entry_end_seconds=265,
            state={"direction": "Yes"},
            token_ids=["up-token", "down-token"],
            target_token_id="up-token",
            entry_price=0.5,
            usd_to_buy=3.0,
            min_entry_price=0.4,
            max_entry_price=0.6,
        )
    )

    assert result["attempted"] is True
    assert result["filled"] is True
    assert result["fallback"] is False
    assert result["route"] == "maker_live"
    assert result["filled_usd"] == pytest.approx(3.0)
    assert result["entry_fee_usd"] == 0.0
    place_call = calls[0]
    assert place_call[0] == "place_order"
    assert place_call[1]["post_only"] is True
    assert place_call[1]["order_type"] == "GTC"
    assert not any(name == "cancel_order" for name, _ in calls)


def test_martingale_maker_live_cancels_unfilled_order_for_taker_fallback():
    trader = make_trader()
    trader.strategy_mode = "martingale_maker"
    trader.dry_run = False
    trader.martingale_maker_wait_seconds = 0.02
    trader.martingale_maker_sample_seconds = 0.01
    trader._schedule_csv_row = lambda *_args, **_kwargs: None

    async def fetch_order_book(_token_id):
        return {
            "bids": [{"price": "0.49", "size": "100"}],
            "asks": [{"price": "0.52", "size": "100"}],
        }

    trader.fetch_order_book = fetch_order_book
    calls = []

    class FakeSession:
        async def call_tool(self, name, arguments=None):
            calls.append((name, arguments or {}))
            if name == "place_order":
                return SimpleNamespace(content=[SimpleNamespace(text=json.dumps({
                    "orderID": "0xmaker",
                    "status": "live",
                }))])
            if name == "get_order":
                return SimpleNamespace(content=[SimpleNamespace(text=json.dumps({
                    "id": "0xmaker",
                    "status": "live",
                    "size_matched": "0",
                    "price": "0.50",
                }))])
            if name == "cancel_order":
                return SimpleNamespace(content=[SimpleNamespace(text=json.dumps({"success": True}))])
            raise AssertionError(f"unexpected tool {name}")

    result = asyncio.run(
        trader._try_maker_entry_probe(
            session=FakeSession(),
            slug="btc-updown-5m-1800000000",
            start_time=datetime.datetime.fromtimestamp(
                1_800_000_000,
                datetime.timezone.utc,
            ),
            elapsed=0.0,
            entry_end_seconds=265,
            state={"direction": "Yes"},
            token_ids=["up-token", "down-token"],
            target_token_id="up-token",
            entry_price=0.5,
            usd_to_buy=3.0,
            min_entry_price=0.4,
            max_entry_price=0.6,
        )
    )

    assert result["attempted"] is True
    assert result["filled"] is False
    assert result["fallback"] is True
    assert result["route"] == "taker"
    assert any(name == "cancel_order" for name, _ in calls)


def test_martingale_maker_live_partial_fill_reports_remaining():
    trader = make_trader()
    trader.strategy_mode = "martingale_maker"
    trader.dry_run = False
    trader.martingale_maker_wait_seconds = 0.02
    trader.martingale_maker_sample_seconds = 0.01
    trader._schedule_csv_row = lambda *_args, **_kwargs: None

    async def fetch_order_book(_token_id):
        return {
            "bids": [{"price": "0.49", "size": "100"}],
            "asks": [{"price": "0.52", "size": "100"}],
        }

    trader.fetch_order_book = fetch_order_book

    class FakeSession:
        async def call_tool(self, name, arguments=None):
            if name == "place_order":
                return SimpleNamespace(content=[SimpleNamespace(text=json.dumps({
                    "orderID": "0xmaker",
                    "status": "live",
                }))])
            if name == "get_order":
                return SimpleNamespace(content=[SimpleNamespace(text=json.dumps({
                    "id": "0xmaker",
                    "status": "live",
                    "size_matched": "1.0",
                    "price": "0.50",
                }))])
            if name == "cancel_order":
                return SimpleNamespace(content=[SimpleNamespace(text=json.dumps({"success": True}))])
            raise AssertionError(f"unexpected tool {name}")

    result = asyncio.run(
        trader._try_maker_entry_probe(
            session=FakeSession(),
            slug="btc-updown-5m-1800000000",
            start_time=datetime.datetime.fromtimestamp(
                1_800_000_000,
                datetime.timezone.utc,
            ),
            elapsed=0.0,
            entry_end_seconds=265,
            state={"direction": "Yes"},
            token_ids=["up-token", "down-token"],
            target_token_id="up-token",
            entry_price=0.5,
            usd_to_buy=3.0,
            min_entry_price=0.4,
            max_entry_price=0.6,
        )
    )

    assert result["filled"] is True
    assert result["partial"] is True
    assert result["fallback"] is True
    assert result["route"] == "maker_partial"
    assert result["filled_usd"] == pytest.approx(0.5)
    assert result["remaining_usd"] == pytest.approx(2.5)
