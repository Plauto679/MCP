import asyncio
import datetime
import json
import time
from unittest.mock import patch

from src.orchestrator import CopyTrader


def make_trader():
    trader = CopyTrader.__new__(CopyTrader)
    trader.logs = []
    trader.log = trader.logs.append
    trader.martingale_initial_amount = 1.0
    trader._chainlink_price_samples = {}
    trader._martingale_entry_plans = {}
    trader._martingale_prefetch_attempts = {}
    trader._martingale_market_cache = {}
    trader._background_tasks = set()
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


def test_chainlink_result_never_uses_a_nearby_second():
    trader = make_trader()
    slug = "btc-updown-5m-1800000000"
    trader._record_chainlink_price(1_800_000_000_000, 60_000.0)
    trader._record_chainlink_price(1_800_000_299_000, 60_100.0)

    result = asyncio.run(
        trader._infer_chainlink_window_result(slug, "Yes", wait_seconds=0)
    )

    assert result == (None, 60_000.0, None)


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
    assert plan["loss"] == {
        "direction": "No",
        "streak": 3,
        "loss_bank": 3.5,
        "recovery_target": 4.5,
    }


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
    assert state["loss_bank"] == 3.5
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


def test_boundary_settlement_and_next_entry_happen_in_same_tick():
    trader = make_trader()
    trader.running = True
    trader.dry_run = True
    trader.debug_logs = False
    trader.martingale_entry_start_seconds = 0
    trader.martingale_entry_end_seconds = 15
    trader.martingale_recovery_entry_end_seconds = 269
    trader.martingale_min_entry_price = 0.4
    trader.martingale_max_entry_price = 0.6
    trader.martingale_recovery_min_entry_price = 0.2
    trader.martingale_recovery_max_entry_price = 0.8
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
    assert state["loss_bank"] == 1.0
    assert state["current_amount"] == 2.0
    assert state["entry_shares"] == 4.0
    exit_index = next(i for i, line in enumerate(trader.logs) if "[Martingale] EXIT" in line)
    entry_index = next(i for i, line in enumerate(trader.logs) if "[Martingale] ENTRY" in line)
    assert exit_index < entry_index
