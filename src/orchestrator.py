import asyncio
import os
import sys
import math
import json
import csv
import time
import datetime
import threading
from dataclasses import replace
from typing import List, Dict, Optional, Tuple
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

# We need to run the server as a subprocess for MCP connection
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from .fair_value import (
    FairValueDecision,
    best_decision,
    blend_fair_yes_with_market,
    estimate_fair_yes,
    taker_breakeven_probability,
    taker_fee_for_stake,
    taker_ev_per_usd,
)
from .cheap_reversal_model import load_cheap_reversal_model, score_cheap_reversal_candidate
from .fair_value_entry_model import load_entry_model, score_entry_candidate
from .kalman_features import kalman_step

load_dotenv()

SERVER_SCRIPT = os.path.join(os.path.dirname(__file__), "server.py")
PYTHON_EXE = sys.executable

class CopyTrader:
    def __init__(self, target_wallets: List[str] = None, dry_run: bool = False, poll_interval: int = 10, size_mode: str = 'fixed', size_value: float = 10.0, target_wallet: str = None):
        # Backward compatibility for single wallet arg
        self.target_wallets = target_wallets
            
        self.dry_run = dry_run
        self.poll_interval = poll_interval
        self.size_mode = size_mode  # 'fixed' or 'percentage'
        self.size_value = size_value
        
        # Winning Strategy Config
        self.winning_strategy_enabled = True # Default enabled if using this mode? Or just toggle.
        self.winning_price_threshold = 0.75
        self.winning_entry_time = 3.0
        self.winning_exit_time = 4.5
        
        # Strategy Mode & Sizing
        self.strategy_mode = 'copy' # 'copy', 'winning', 'martingale', or 'martingale_maker'
        self.winning_size_mode = 'fixed' # 'fixed' or 'percent'
        self.winning_size_value = 10.0 # $10 or 10%
        self.martingale_initial_amount = 5.0
        
        # Market Cache
        self.market_cache = {} # asset_id -> { start_time: datetime, end_time: datetime, is_5m: bool }
        self.running = False
        self.app_state = {
            "positions": {},  # target_address -> { asset_id -> size }
            "prices": {},     # asset_id -> current price (float) using global price cache
            "multipliers": {}, # asset_id -> ratio (my_shares / target_shares)
            "pending_settlements": []
        }
        self.initial_sync_complete = False
        self.logs: List[str] = []
        try:
            self.log_buffer_limit = min(max(int(os.getenv("LOG_BUFFER_LIMIT", "5000")), 300), 50000)
        except ValueError:
            self.log_buffer_limit = 5000
        self.log_total_count = 0
        self._active_events_failures = 0
        self._last_active_events_warn_ts: Optional[float] = None
        self._last_heartbeat_ts: Optional[float] = None
        self._last_reconcile_ts: Optional[float] = None
        self._last_excel_lock_warn_ts: Optional[float] = None
        self.debug_logs = os.getenv("DEBUG_LOGS", "false").lower() == "true"
        self._http_session = None
        self._http_session_loop = None
        self._martingale_market_cache = {}
        self._polymarket_http_backoff = {}
        self._martingale_prefetch_attempts = {}
        self._martingale_entry_plans = {}
        self._martingale_maker_recorder_tasks = {}
        self._martingale_maker_recorder_context = {}
        self._chainlink_price_samples = {}
        self._chainlink_feed_task: Optional[asyncio.Task] = None
        self._chainlink_connected = False
        self._last_chainlink_warn_ts: Optional[float] = None
        self._binance_kline_cache = {}
        self._last_supervisor_review_ts: Optional[float] = None
        self._last_supervisor_disabled_log_ts: Optional[float] = None
        self._background_tasks = set()
        self._csv_lock = threading.Lock()
        self.http_headers = {
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131 Safari/537.36",
        }
        self.martingale_decision_threshold = 0.5
        self.martingale_entry_start_seconds = 0
        self.martingale_entry_end_seconds = 265
        self.martingale_min_entry_price = 0.40
        self.martingale_max_entry_price = 0.60
        self.martingale_mid_entry_start_seconds = 120
        self.martingale_mid_min_entry_price = 0.45
        self.martingale_mid_max_entry_price = 0.55
        self.martingale_late_entry_start_seconds = 240
        self.martingale_late_min_entry_price = 0.48
        self.martingale_late_max_entry_price = 0.52
        self.martingale_recovery_entry_end_seconds = 285
        self.martingale_recovery_min_entry_price = 0.40
        self.martingale_recovery_max_entry_price = 0.60
        self.martingale_late_recovery_start_seconds = 240
        self.martingale_late_recovery_min_entry_price = 0.48
        self.martingale_late_recovery_max_entry_price = 0.52
        self.martingale_limit_slippage = 0.05
        self.martingale_taker_fee_rate = 0.07
        self.martingale_balance_floor_fraction = 0.50
        self.martingale_order_type = "FOK"
        self.martingale_chainlink_boundary_tolerance_seconds = 1.5
        self.martingale_chainlink_min_decision_margin = 10.0
        self.martingale_maker_wait_seconds = 5.0
        self.martingale_maker_price_offset = 0.01
        self.martingale_maker_sample_seconds = 0.5
        self.martingale_maker_record_seconds = 15.0
        self.martingale_maker_blind_price = 0.50
        self.martingale_maker_blind_end_seconds = 15.0
        self.fair_value_stake_usd = 1.0
        self.fair_value_min_taker_edge = 0.10
        self.fair_value_min_maker_edge = 0.10
        self.fair_value_min_ev_per_usd = 0.30
        self.fair_value_taker_guard_min_edge = 0.06
        self.fair_value_taker_guard_min_ev_per_usd = 0.12
        self.fair_value_entry_start_seconds = 0.0
        self.fair_value_entry_end_seconds = 240.0
        self.fair_value_min_price = 0.35
        self.fair_value_max_price = 0.50
        self.fair_value_core_min_price = 0.35
        self.fair_value_core_max_price = 0.50
        self.fair_value_maker_wait_seconds = 3.0
        self.fair_value_sample_seconds = 1.0
        self.fair_value_model_sensitivity_bps = 28.0
        self.fair_value_prefer_maker = True
        self.fair_value_live_trading_enabled = False
        self.fair_value_order_type = "FOK"
        self.fair_value_core_enabled = True
        self.fair_value_core_contrarian_only = False
        self.fair_value_core_max_entries_per_window = 2
        self.fair_value_min_ev_improvement_per_entry = 0.03
        self.fair_value_momentum_enabled = True
        self.fair_value_momentum_start_seconds = 240.0
        self.fair_value_momentum_end_seconds = 300.0
        self.fair_value_momentum_min_edge = 0.02
        self.fair_value_momentum_min_ev_per_usd = 0.07
        self.fair_value_momentum_min_confidence = 0.08
        self.fair_value_momentum_max_price = 0.90
        self.fair_value_momentum_stake_multiplier = 1.0
        self.fair_value_momentum_max_entries_per_window = 2
        self.fair_value_momentum_min_ev_improvement_per_entry = 0.04
        self.fair_value_late_continuation_enabled = True
        self.fair_value_late_continuation_start_seconds = 250.0
        self.fair_value_late_continuation_end_seconds = 300.0
        self.fair_value_late_continuation_min_abs_delta_bps = 5.0
        self.fair_value_late_continuation_min_probability = 0.87
        self.fair_value_late_continuation_max_probability = 0.95
        self.fair_value_late_continuation_min_ev_per_usd = 0.015
        self.fair_value_late_continuation_price_buffer = 0.01
        self.fair_value_late_continuation_max_price = 0.95
        self.fair_value_late_continuation_stake_multiplier = 1.0
        self.fair_value_late_continuation_max_entries_per_window = 2
        self.fair_value_late_continuation_min_ev_improvement_per_entry = 0.03
        self.fair_value_late_continuation_entry_model_min_win_prob = 0.0
        self.fair_value_giro_probe_enabled = True
        self.fair_value_giro_probe_start_seconds = 0.0
        self.fair_value_giro_probe_end_seconds = 300.0
        self.fair_value_giro_probe_min_price = 0.15
        self.fair_value_giro_probe_max_price = 0.42
        self.fair_value_giro_probe_min_abs_delta_bps = 0.5
        self.fair_value_giro_probe_min_probability = 0.45
        self.fair_value_giro_probe_min_ev_per_usd = 0.05
        self.fair_value_giro_probe_min_confidence = 0.10
        self.fair_value_giro_probe_max_entries_per_window = 2
        self.fair_value_giro_probe_min_price_step = 0.03
        self.fair_value_giro_probe_stake_multiplier = 1.0
        self.fair_value_giro_probe_model_enabled = True
        self.fair_value_giro_probe_model_path = ""
        self._cheap_reversal_model = None
        self._cheap_reversal_model_loaded_path = None
        self._cheap_reversal_model_loaded_mtime = None
        self._last_cheap_reversal_model_warn_ts = 0.0
        self.fair_value_entry_model_enabled = True
        self.fair_value_core_entry_model_min_win_prob = 0.46
        self.fair_value_core_entry_model_max_win_prob = 0.54
        self.fair_value_momentum_entry_model_min_win_prob = 0.63
        self.fair_value_shadow_dynamic_stake_enabled = True
        self.fair_value_shadow_dynamic_stake_min_multiplier = 1.0
        self.fair_value_shadow_dynamic_stake_max_multiplier = 2.0
        self.fair_value_entry_model_path = ""
        self._fair_value_entry_model = None
        self._fair_value_entry_model_loaded_path = None
        self._fair_value_entry_model_loaded_mtime = None
        self._last_fair_value_entry_model_warn_ts = 0.0
        self.fair_value_supervisor_enabled = True
        self.fair_value_supervisor_interval_seconds = 2 * 60 * 60
        self.fair_value_supervisor_lookback_seconds = 2 * 60 * 60
        self.fair_value_entry_model_auto_retrain_enabled = True
        self.fair_value_entry_model_auto_retrain_interval_seconds = 2 * 60 * 60
        self.fair_value_entry_model_auto_retrain_min_holdout_rows = 80
        self._last_entry_model_retrain_ts = time.time()
        self._entry_model_retrain_running = False
        self.polymarket_min_market_buy_usd = 1.0
        self.polymarket_min_limit_order_shares = 5.0
        self.data_dir = os.path.join(os.path.dirname(__file__), "..", "data")
        self.fair_value_entry_model_path = os.path.join(self.data_dir, "fair_value_entry_model.json")
        self.fair_value_giro_probe_model_path = os.path.join(
            self.data_dir,
            "cheap_reversal",
            "cheap_reversal_model.json",
        )
        self.runtime_log_file = os.path.join(self.data_dir, "runtime.log")
        self._task: Optional[asyncio.Task] = None
        
        # Persistence
        self.data_file = os.path.join(os.path.dirname(__file__), "..", "user_data.json")
        self.history: List[Dict] = []
        self.data_file = os.path.join(os.path.dirname(__file__), "..", "user_data.json")
        self.history: List[Dict] = []
        self.stop_time: Optional[float] = None # Timestamp when bot should stop automatically
        
        # Identify My Wallet Address (for checking own positions)
        self.my_address = os.getenv("POLYMARKET_PROXY_ADDRESS")
        if not self.my_address:
            # If no proxy, we might be EOA. For now, we assume Proxy as per recent config.
            # If needed, we could derive EOA from PRIVATE_KEY using web3/eth_account
            pass

        self.load_state()

    def _usd_to_shares(self, usd_amount: float, price: float) -> float:
        """
        Convert target USD notional into shares at a given price.
        Keeps 2 decimals to match order sizing constraints in server.
        """
        p = max(float(price), 0.01)
        shares = round(float(usd_amount) / p, 2)
        if shares <= 0:
            shares = 0.01
        return shares

    def _is_martingale_mode(self) -> bool:
        return self.strategy_mode in ("martingale", "martingale_maker")

    def _is_martingale_maker_mode(self) -> bool:
        return self.strategy_mode == "martingale_maker"

    def _is_fair_value_mode(self) -> bool:
        return self.strategy_mode == "fair_value"

    def _new_fair_value_state(self) -> Dict:
        return {
            "active_slug": None,
            "entry_done": False,
            "entries": [],
            "next_entry_id": 1,
            "direction": None,
            "target_token_id": None,
            "entry_price": 0.0,
            "entry_shares": 0.0,
            "stake_usd": 0.0,
            "entry_fee_usd": 0.0,
            "entry_route": "",
            "fair_probability": 0.0,
            "edge_probability": 0.0,
            "ev_per_usd": 0.0,
            "opened_at": None,
            "maker_candidate": None,
            "maker_attempted_slug": None,
            "order_error_cooldown_until": 0.0,
            "consecutive_order_errors": 0,
            "last_signal_sample_at": 0.0,
            "last_settlement_attempt_at": 0.0,
            "skip_logged_slug": None,
            "kalman": None,
            "dry_run": bool(self.dry_run),
        }

    def _new_fair_value_giro_probe_state(self) -> Dict:
        return {
            "windows": {},
            "next_entry_id": 1,
            "settlement_attempts": {},
            "dry_run": bool(self.dry_run),
            "last_disabled_log_at": 0.0,
        }

    def _fair_value_entries(self, state: Dict) -> List[Dict]:
        entries = state.get("entries")
        if not isinstance(entries, list):
            entries = []

        if not entries and state.get("entry_done") and state.get("direction") in ("Yes", "No"):
            entries = [{
                "entry_id": int(state.get("next_entry_id", 1) or 1),
                "entry_number_in_window": 1,
                "tactic": state.get("tactic", "core_edge") or "core_edge",
                "slug": state.get("active_slug"),
                "direction": state.get("direction"),
                "target_token_id": state.get("target_token_id"),
                "entry_price": float(state.get("entry_price", 0.0) or 0.0),
                "entry_shares": float(state.get("entry_shares", 0.0) or 0.0),
                "stake_usd": float(state.get("stake_usd", 0.0) or 0.0),
                "entry_fee_usd": float(state.get("entry_fee_usd", 0.0) or 0.0),
                "entry_route": state.get("entry_route", ""),
                "fair_probability": float(state.get("fair_probability", 0.0) or 0.0),
                "edge_probability": float(state.get("edge_probability", 0.0) or 0.0),
                "ev_per_usd": float(state.get("ev_per_usd", 0.0) or 0.0),
                "break_even_price": float(state.get("break_even_price", 0.0) or 0.0),
                "max_acceptable_price": float(state.get("max_acceptable_price", 0.0) or 0.0),
                "price_margin": float(state.get("price_margin", 0.0) or 0.0),
                "continuation_probability": float(state.get("continuation_probability", 0.0) or 0.0),
                "abs_delta_bps": float(state.get("abs_delta_bps", 0.0) or 0.0),
                "opened_at": state.get("opened_at"),
                "elapsed_s": float(state.get("elapsed_s", 0.0) or 0.0),
            }]
            state["next_entry_id"] = 2

        state["entries"] = entries
        state["entry_done"] = bool(entries)
        return entries

    def _normalize_fair_value_state(self, state: Dict) -> Dict:
        if not isinstance(state, dict):
            return self._new_fair_value_state()
        saved_dry_run = state.get("dry_run")
        if saved_dry_run is not None and bool(saved_dry_run) != bool(self.dry_run):
            self.log(
                "[FairValue] DryRun mode changed since saved state. "
                "Resetting paper/live state before continuing."
            )
            return self._new_fair_value_state()
        defaults = self._new_fair_value_state()
        for key, value in defaults.items():
            state.setdefault(key, value)
        state["dry_run"] = bool(self.dry_run)
        self._fair_value_entries(state)
        return state

    def _normalize_fair_value_giro_probe_state(self, state: Dict) -> Dict:
        if not isinstance(state, dict):
            return self._new_fair_value_giro_probe_state()
        saved_dry_run = state.get("dry_run")
        if saved_dry_run is not None and bool(saved_dry_run) != bool(self.dry_run):
            self.log(
                "[GiroProbe] DryRun mode changed since saved state. "
                "Resetting giro probe paper state."
            )
            return self._new_fair_value_giro_probe_state()
        defaults = self._new_fair_value_giro_probe_state()
        for key, value in defaults.items():
            state.setdefault(key, value)
        if not isinstance(state.get("windows"), dict):
            state["windows"] = {}
        if not isinstance(state.get("settlement_attempts"), dict):
            state["settlement_attempts"] = {}
        state["dry_run"] = bool(self.dry_run)
        return state

    def _reset_fair_value_for_mode_change(self, previous_dry_run: bool, new_dry_run: bool):
        self.app_state["fair_value_state"] = self._new_fair_value_state()
        self.app_state["fair_value_giro_probe_state"] = self._new_fair_value_giro_probe_state()
        self.log(
            f"[FairValue] DryRun changed {previous_dry_run} -> {new_dry_run}. "
            "Resetting state so simulated entries never carry into live trading."
        )

    def _reset_fair_value_for_strategy_change(self, previous_strategy: str, new_strategy: str):
        if previous_strategy == new_strategy or new_strategy != "fair_value":
            return
        self.app_state["fair_value_state"] = self._new_fair_value_state()
        self.app_state["fair_value_giro_probe_state"] = self._new_fair_value_giro_probe_state()
        self.log(
            f"[FairValue] Strategy changed {previous_strategy} -> {new_strategy}. "
            "Starting with a clean Fair Value state."
        )

    def _new_martingale_state(self) -> Dict:
        return {
            "active_slug": None,
            "entry_done": False,
            "streak": 0,
            "current_amount": self.martingale_initial_amount,
            "direction": None,
            "entry_price": 0.0,
            "target_token_id": None,
            "entry_shares": 0.0,
            "loss_bank": 0.0,
            "entry_fee_usd": 0.0,
            "entry_route": "",
            "maker_price": None,
            "maker_waited_seconds": 0.0,
            "maker_touch_signal": False,
            "cycle_start_balance": None,
            "entry_retry_after": 0.0,
            "entry_attempts": 0,
            "skip_logged": False,
            "opened_at": None,
            "dry_run": bool(self.dry_run),
        }

    def _normalize_martingale_state(self, state: Dict) -> Dict:
        if not isinstance(state, dict):
            return self._new_martingale_state()

        saved_dry_run = state.get("dry_run")
        if saved_dry_run is not None and bool(saved_dry_run) != bool(self.dry_run):
            self.log(
                "[Martingale] DryRun mode changed since saved state. "
                "Resetting martingale cycle before continuing."
            )
            return self._new_martingale_state()

        defaults = self._new_martingale_state()
        for key, value in defaults.items():
            state.setdefault(key, value)
        state["dry_run"] = bool(self.dry_run)
        return state

    def _reset_martingale_for_mode_change(self, previous_dry_run: bool, new_dry_run: bool):
        self.app_state["martingale_state"] = self._new_martingale_state()
        self.app_state["pending_settlements"] = [
            item for item in self.app_state.get("pending_settlements", [])
            if not item.get("is_martingale")
        ]
        self._martingale_entry_plans.clear()
        self._martingale_prefetch_attempts.clear()
        self.log(
            f"[Martingale] DryRun changed {previous_dry_run} -> {new_dry_run}. "
            "Resetting cycle so simulated state never carries into live trading."
        )

    def _reset_martingale_for_strategy_change(self, previous_strategy: str, new_strategy: str):
        martingale_modes = {"martingale", "martingale_maker"}
        if previous_strategy == new_strategy or new_strategy not in martingale_modes:
            return
        if previous_strategy not in martingale_modes and "martingale_state" not in self.app_state:
            return
        self.app_state["martingale_state"] = self._new_martingale_state()
        self.app_state["pending_settlements"] = [
            item for item in self.app_state.get("pending_settlements", [])
            if not item.get("is_martingale")
        ]
        self._martingale_entry_plans.clear()
        self._martingale_prefetch_attempts.clear()
        self.log(
            f"[Martingale] Strategy changed {previous_strategy} -> {new_strategy}. "
            "Resetting cycle before starting this strategy."
        )

    async def _get_http_session(self):
        import aiohttp

        loop = asyncio.get_running_loop()
        if (
            self._http_session is None
            or self._http_session.closed
            or self._http_session_loop is not loop
        ):
            if self._http_session is not None and not self._http_session.closed:
                await self._http_session.close()
            connector = aiohttp.TCPConnector(ssl=False, ttl_dns_cache=300, limit=20)
            timeout = aiohttp.ClientTimeout(total=8)
            self._http_session = aiohttp.ClientSession(
                headers=self.http_headers,
                connector=connector,
                timeout=timeout,
            )
            self._http_session_loop = loop
        return self._http_session

    async def _close_http_session(self):
        if self._http_session is not None and not self._http_session.closed:
            await self._http_session.close()
        self._http_session = None
        self._http_session_loop = None

    def _polymarket_host_from_url(self, url: str) -> str:
        try:
            from urllib.parse import urlparse

            return urlparse(url).netloc or "polymarket"
        except Exception:
            return "polymarket"

    def _polymarket_backoff_active(self, url: str) -> bool:
        host = self._polymarket_host_from_url(url)
        item = self._polymarket_http_backoff.get(host, {})
        return time.time() < float(item.get("next_ts", 0.0) or 0.0)

    def _mark_polymarket_http_success(self, url: str):
        self._polymarket_http_backoff.pop(self._polymarket_host_from_url(url), None)

    def _mark_polymarket_http_failure(self, url: str, exc: Exception):
        host = self._polymarket_host_from_url(url)
        now_ts = time.time()
        item = self._polymarket_http_backoff.get(host, {})
        failures = int(item.get("failures", 0) or 0) + 1
        delay = min(60.0, max(2.0, 2 ** min(failures, 5)))
        item.update({"failures": failures, "next_ts": now_ts + delay})
        self._polymarket_http_backoff[host] = item

        last_warn = float(item.get("last_warn_ts", 0.0) or 0.0)
        if now_ts - last_warn >= 60.0:
            item["last_warn_ts"] = now_ts
            self.log(
                f"[Warn] Polymarket HTTP unavailable for {host}: "
                f"{type(exc).__name__}: {str(exc)[:120]}. Backing off {delay:.0f}s."
            )

    def _sync_polymarket_get_json_via_curl(self, url: str, timeout: float = 8.0):
        from curl_cffi import requests as curl_requests

        response = curl_requests.get(
            url,
            timeout=timeout,
            impersonate="chrome131",
            http_version="v3",
            headers=self.http_headers,
        )
        response.raise_for_status()
        return response.json()

    async def _polymarket_get_json_fallback(self, url: str, timeout: float = 8.0):
        if self._polymarket_backoff_active(url):
            return None
        try:
            data = await asyncio.to_thread(self._sync_polymarket_get_json_via_curl, url, timeout)
            self._mark_polymarket_http_success(url)
            return data
        except Exception as exc:
            self._mark_polymarket_http_failure(url, exc)
            return None

    def _sync_binance_btc_5m_kline(self, window_start_ts: float, timeout: float = 6.0):
        import requests

        start_ms = int(float(window_start_ts) * 1000)
        url = (
            "https://api.binance.com/api/v3/klines"
            f"?symbol=BTCUSDT&interval=5m&startTime={start_ms}&limit=1"
        )
        response = requests.get(url, timeout=timeout)
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, list) or not data:
            return None
        item = data[0]
        return {
            "open_time": int(item[0]),
            "open": float(item[1]),
            "close": float(item[4]),
            "close_time": int(item[6]),
        }

    async def _binance_btc_5m_context(self, window_start_ts: float, max_age_seconds: float = 1.0):
        cache_key = int(float(window_start_ts))
        now_ts = time.time()
        cached = self._binance_kline_cache.get(cache_key)
        if cached and now_ts - float(cached.get("fetched_at", 0.0) or 0.0) <= max(float(max_age_seconds), 0.0):
            return cached.get("data")
        try:
            data = await asyncio.to_thread(self._sync_binance_btc_5m_kline, window_start_ts)
            self._binance_kline_cache[cache_key] = {"fetched_at": now_ts, "data": data}
            stale_keys = [key for key in self._binance_kline_cache if key < cache_key - 3600]
            for key in stale_keys:
                self._binance_kline_cache.pop(key, None)
            return data
        except Exception as exc:
            now_ts = time.time()
            last_warn = float(getattr(self, "_last_binance_warn_ts", 0.0) or 0.0)
            if now_ts - last_warn >= 60.0:
                self._last_binance_warn_ts = now_ts
                self.log(f"[Warn] Binance BTC fallback unavailable: {type(exc).__name__}: {str(exc)[:120]}")
            return None

    async def _fair_value_price_context(self, window_start_ts: float, now_ts: float):
        opening_price, _, _, _ = self._chainlink_boundary_sample(window_start_ts, tolerance_seconds=2.0)
        latest_price, _, latest_offset = self._fair_value_latest_price_after(window_start_ts)
        if opening_price and latest_price:
            return opening_price, latest_price, latest_offset, "chainlink"

        kline = await self._binance_btc_5m_context(window_start_ts)
        if kline and kline.get("open") and kline.get("close"):
            return (
                float(kline["open"]),
                float(kline["close"]),
                round(max(float(now_ts) - float(window_start_ts), 0.0), 3),
                "binance_5m_signal_fallback",
            )

        return opening_price, latest_price, latest_offset, "chainlink_unavailable"

    def _record_chainlink_price(self, timestamp_ms, value):
        try:
            ts = int(timestamp_ms)
            price = float(value)
        except (TypeError, ValueError):
            return
        if ts <= 0 or price <= 0:
            return

        self._chainlink_price_samples[ts] = price
        cutoff = ts - (15 * 60 * 1000)
        stale = [sample_ts for sample_ts in self._chainlink_price_samples if sample_ts < cutoff]
        for sample_ts in stale:
            self._chainlink_price_samples.pop(sample_ts, None)

    def _ingest_chainlink_message(self, message: str):
        try:
            data = json.loads(message)
        except (TypeError, json.JSONDecodeError):
            return

        payload = data.get("payload", {}) if isinstance(data, dict) else {}
        if not isinstance(payload, dict):
            return

        snapshot = payload.get("data")
        if isinstance(snapshot, list):
            for sample in snapshot:
                if isinstance(sample, dict):
                    self._record_chainlink_price(sample.get("timestamp"), sample.get("value"))
            return

        symbol = str(payload.get("symbol") or "").lower()
        if symbol and symbol != "btc/usd":
            return
        self._record_chainlink_price(payload.get("timestamp"), payload.get("value"))

    async def _chainlink_price_feed_loop(self):
        import websockets

        subscription = json.dumps({
            "action": "subscribe",
            "subscriptions": [{
                "topic": "crypto_prices_chainlink",
                "type": "*",
                "filters": "{\"symbol\":\"btc/usd\"}",
            }],
        })

        while self.running:
            try:
                async with websockets.connect(
                    "wss://ws-live-data.polymarket.com",
                    ping_interval=None,
                    close_timeout=2,
                ) as websocket:
                    await websocket.send(subscription)
                    self._chainlink_connected = True
                    self.log("[Martingale] Chainlink BTC/USD feed connected.")
                    while self.running:
                        try:
                            message = await asyncio.wait_for(websocket.recv(), timeout=5.0)
                        except asyncio.TimeoutError:
                            await websocket.send("PING")
                            continue
                        if isinstance(message, str):
                            self._ingest_chainlink_message(message)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._chainlink_connected = False
                now_ts = time.time()
                if (
                    self._last_chainlink_warn_ts is None
                    or now_ts - self._last_chainlink_warn_ts >= 60
                ):
                    self.log(f"[Warn] Chainlink feed disconnected: {exc}. Reconnecting...")
                    self._last_chainlink_warn_ts = now_ts
                await asyncio.sleep(1.0)
            finally:
                self._chainlink_connected = False

    def _ensure_chainlink_price_feed(self):
        if self._chainlink_feed_task is None or self._chainlink_feed_task.done():
            self._chainlink_feed_task = asyncio.create_task(self._chainlink_price_feed_loop())

    async def _stop_chainlink_price_feed(self):
        if self._chainlink_feed_task is None:
            return
        self._chainlink_feed_task.cancel()
        try:
            await self._chainlink_feed_task
        except asyncio.CancelledError:
            pass
        self._chainlink_feed_task = None
        self._chainlink_connected = False

    def _schedule_martingale_trade_record(self, **record_kwargs):
        async def write_record():
            try:
                await asyncio.to_thread(self._record_martingale_trade_row, **record_kwargs)
            except Exception as exc:
                self.log(f"[Error] Failed saving martingale settlement to excel: {exc}")

        task = asyncio.create_task(write_record())
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def _drain_background_tasks(self):
        if not self._background_tasks:
            return
        await asyncio.gather(*list(self._background_tasks), return_exceptions=True)

    def _chainlink_boundary_price(self, boundary_ts: float) -> Optional[float]:
        boundary_ms = int(round(float(boundary_ts) * 1000))
        return self._chainlink_price_samples.get(boundary_ms)

    def _chainlink_boundary_sample(
        self,
        boundary_ts: float,
        tolerance_seconds: Optional[float] = None,
    ) -> Tuple[Optional[float], Optional[int], Optional[float], bool]:
        boundary_ms = int(round(float(boundary_ts) * 1000))
        exact_price = self._chainlink_price_samples.get(boundary_ms)
        if exact_price is not None:
            return exact_price, boundary_ms, 0.0, True

        tolerance = (
            self.martingale_chainlink_boundary_tolerance_seconds
            if tolerance_seconds is None
            else tolerance_seconds
        )
        tolerance_ms = int(max(float(tolerance), 0.0) * 1000)
        if tolerance_ms <= 0:
            return None, None, None, False

        best_ts = None
        best_price = None
        best_diff = None
        for sample_ts, price in self._chainlink_price_samples.items():
            diff = abs(int(sample_ts) - boundary_ms)
            if diff > tolerance_ms:
                continue
            # Prefer the closest sample; if tied, prefer the one after the boundary.
            if (
                best_diff is None
                or diff < best_diff
                or (diff == best_diff and int(sample_ts) >= boundary_ms > int(best_ts or 0))
            ):
                best_ts = int(sample_ts)
                best_price = price
                best_diff = diff

        if best_ts is None:
            return None, None, None, False
        offset_seconds = (best_ts - boundary_ms) / 1000.0
        return best_price, best_ts, offset_seconds, False

    def _chainlink_latest_sample_after(
        self,
        boundary_ts: float,
    ) -> Tuple[Optional[float], Optional[int], Optional[float]]:
        boundary_ms = int(round(float(boundary_ts) * 1000))
        latest_ts = None
        latest_price = None
        for sample_ts, price in self._chainlink_price_samples.items():
            sample_ts = int(sample_ts)
            if sample_ts < boundary_ms:
                continue
            if latest_ts is None or sample_ts > latest_ts:
                latest_ts = sample_ts
                latest_price = price
        if latest_ts is None:
            return None, None, None
        return latest_price, latest_ts, (latest_ts - boundary_ms) / 1000.0

    async def _infer_chainlink_window_result(
        self,
        slug: str,
        direction: str,
        wait_seconds: float = 2.5,
    ) -> Tuple[Optional[bool], Optional[float], Optional[float]]:
        start_time = self._start_time_from_btc_slug(slug)
        if start_time is None or direction not in ("Yes", "No"):
            return None, None, None

        start_ts = start_time.timestamp()
        end_ts = start_ts + 300
        deadline = asyncio.get_running_loop().time() + max(float(wait_seconds), 0.0)
        self._last_chainlink_settlement_detail = {}
        while True:
            (
                opening_price,
                opening_sample_ts,
                opening_offset,
                opening_exact,
            ) = self._chainlink_boundary_sample(start_ts)
            (
                closing_price,
                closing_sample_ts,
                closing_offset,
                closing_exact,
            ) = self._chainlink_boundary_sample(end_ts)

            exact_pair = (
                opening_price is not None
                and closing_price is not None
                and opening_exact
                and closing_exact
            )
            deadline_reached = asyncio.get_running_loop().time() >= deadline
            if exact_pair or (deadline_reached and opening_price is not None and closing_price is not None):
                price_delta = float(closing_price) - float(opening_price)
                exact_enough = bool(opening_exact and closing_exact)
                min_margin = 0.0 if exact_enough else max(
                    float(getattr(self, "martingale_chainlink_min_decision_margin", 10.0)),
                    0.0,
                )
                if exact_enough or abs(price_delta) >= min_margin:
                    self._last_chainlink_settlement_detail = {
                        "source": "chainlink_boundary" if exact_enough else "chainlink_tolerant",
                        "opening_sample_ts": opening_sample_ts,
                        "closing_sample_ts": closing_sample_ts,
                        "opening_offset_seconds": opening_offset,
                        "closing_offset_seconds": closing_offset,
                        "price_delta": price_delta,
                        "min_margin": min_margin,
                    }
                    up_won = closing_price >= opening_price
                    is_win = up_won if direction == "Yes" else not up_won
                    return is_win, opening_price, closing_price

                (
                    latest_close_price,
                    latest_close_sample_ts,
                    latest_close_offset,
                ) = self._chainlink_latest_sample_after(end_ts)
                if latest_close_price is not None:
                    price_delta = float(latest_close_price) - float(opening_price)
                    self._last_chainlink_settlement_detail = {
                        "source": "chainlink_latest",
                        "opening_sample_ts": opening_sample_ts,
                        "closing_sample_ts": latest_close_sample_ts,
                        "opening_offset_seconds": opening_offset,
                        "closing_offset_seconds": latest_close_offset,
                        "price_delta": price_delta,
                        "min_margin": 0.0,
                    }
                    up_won = latest_close_price >= opening_price
                    is_win = up_won if direction == "Yes" else not up_won
                    return is_win, opening_price, latest_close_price

                self._last_chainlink_settlement_detail = {
                    "source": "chainlink_ambiguous",
                    "opening_sample_ts": opening_sample_ts,
                    "closing_sample_ts": closing_sample_ts,
                    "opening_offset_seconds": opening_offset,
                    "closing_offset_seconds": closing_offset,
                    "price_delta": price_delta,
                    "min_margin": min_margin,
                }
                return None, opening_price, closing_price

            (
                latest_close_price,
                latest_close_sample_ts,
                latest_close_offset,
            ) = self._chainlink_latest_sample_after(end_ts)
            if opening_price is not None and latest_close_price is not None:
                price_delta = float(latest_close_price) - float(opening_price)
                self._last_chainlink_settlement_detail = {
                    "source": "chainlink_latest",
                    "opening_sample_ts": opening_sample_ts,
                    "closing_sample_ts": latest_close_sample_ts,
                    "opening_offset_seconds": opening_offset,
                    "closing_offset_seconds": latest_close_offset,
                    "price_delta": price_delta,
                    "min_margin": 0.0,
                }
                up_won = latest_close_price >= opening_price
                is_win = up_won if direction == "Yes" else not up_won
                return is_win, opening_price, latest_close_price

            if deadline_reached:
                self._last_chainlink_settlement_detail = {
                    "source": "chainlink_unavailable",
                    "opening_sample_ts": opening_sample_ts,
                    "closing_sample_ts": closing_sample_ts,
                    "opening_offset_seconds": opening_offset,
                    "closing_offset_seconds": closing_offset,
                }
                return None, opening_price, closing_price
            await asyncio.sleep(0.05)

    def _martingale_stake_for_price(self, state: Dict, price: float) -> float:
        target_profit = max(float(self.martingale_initial_amount), 0.01)
        accumulated_losses = max(float(state.get("loss_bank", 0.0)), 0.0)
        p = min(max(float(price), 0.01), 0.99)
        fee_rate = max(float(getattr(self, "martingale_taker_fee_rate", 0.0)), 0.0)
        net_profit_per_usd = (1.0 / p) - 1.0 - (fee_rate * (1.0 - p))
        if net_profit_per_usd <= 0:
            return round(max(accumulated_losses + target_profit, 0.01), 2)
        stake = (accumulated_losses + target_profit) / net_profit_per_usd
        return round(max(stake, 0.01), 2)

    def _martingale_fee_for_fill(self, shares: float, price: float) -> float:
        fee_rate = max(float(getattr(self, "martingale_taker_fee_rate", 0.0)), 0.0)
        p = min(max(float(price), 0.01), 0.99)
        return round(max(float(shares), 0.0) * fee_rate * p * (1.0 - p), 4)

    def _martingale_fee_for_stake(self, stake_usd: float, price: float) -> float:
        p = min(max(float(price), 0.01), 0.99)
        shares = max(float(stake_usd), 0.0) / p
        return self._martingale_fee_for_fill(shares, p)

    def _martingale_win_net_profit(self, shares: float, entry_notional_usd: float, entry_fee_usd: float) -> float:
        return round(max(float(shares), 0.0) - max(float(entry_notional_usd), 0.0) - max(float(entry_fee_usd), 0.0), 4)

    def _entry_fee_from_state(self, state: Dict, shares: float, entry_price: float) -> float:
        if "entry_fee_usd" in state and state.get("entry_fee_usd") is not None:
            try:
                fee = float(state.get("entry_fee_usd"))
                route = str(state.get("entry_route") or "")
                if fee > 0 or route.startswith("maker"):
                    return fee
            except Exception:
                pass
        return self._martingale_fee_for_fill(shares, entry_price)

    def _marketable_limit_price(self, entry_price: float, shares: float) -> float:
        p = min(max(float(entry_price), 0.01), 0.99)
        limit_price = min(0.99, math.ceil((p + self.martingale_limit_slippage) * 100) / 100)
        if float(shares) * limit_price < 1.0:
            return 0.99
        return round(limit_price, 2)

    def _shares_for_limit_minimum(self, shares: float, limit_price: float) -> float:
        rounded = round(max(float(shares), 0.01), 2)
        if rounded * float(limit_price) < 1.0:
            rounded = math.ceil((1.0 / float(limit_price)) * 100.0) / 100.0
        return round(max(rounded, 0.01), 2)

    def _parse_order_response(self, order_text: str, allow_live: bool = False) -> Tuple[bool, str, Dict]:
        import json

        text = (order_text or "").strip()
        if not text:
            return False, "empty order response", {}
        if text.lower().startswith("error"):
            return False, text, {}

        try:
            data = json.loads(text)
        except Exception:
            return True, "", {"raw": text}

        if isinstance(data, list):
            failures = []
            for item in data:
                ok, reason, _ = self._parse_order_response(json.dumps(item), allow_live=allow_live)
                if not ok:
                    failures.append(reason)
            if failures:
                return False, "; ".join(failures), {"items": data}
            return True, "", {"items": data}

        if not isinstance(data, dict):
            return True, "", {"raw": data}

        error_msg = str(data.get("errorMsg") or data.get("error") or "").strip()
        if error_msg:
            return False, error_msg, data
        if data.get("success") is False:
            return False, "order response success=false", data

        status = str(data.get("status") or "").lower()
        if status == "unmatched":
            return False, "order was unmatched; no fill was confirmed", data
        if status == "live" and not allow_live:
            return False, "order is live/resting; no immediate fill was confirmed", data

        return True, "", data

    def _parse_order_amount(self, value) -> float:
        if value in (None, ""):
            return 0.0
        text = str(value).strip()
        try:
            if "." in text:
                return float(text)
            integer_value = int(text)
            if abs(integer_value) < 10_000:
                return float(integer_value)
            return integer_value / 1_000_000.0
        except Exception:
            try:
                return float(text)
            except Exception:
                return 0.0

    def _extract_order_id(self, order_data) -> Optional[str]:
        keys = (
            "orderID",
            "orderId",
            "order_id",
            "id",
            "hash",
        )

        def scan(value):
            if isinstance(value, dict):
                for key in keys:
                    candidate = value.get(key)
                    if candidate not in (None, ""):
                        return str(candidate)
                for nested in value.values():
                    found = scan(nested)
                    if found:
                        return found
            elif isinstance(value, list):
                for item in value:
                    found = scan(item)
                    if found:
                        return found
            return None

        return scan(order_data)

    def _maker_fill_from_order_data(
        self,
        order_data,
        requested_shares: float,
        maker_price: float,
    ) -> Tuple[float, float, float, str]:
        rows = order_data.get("items") if isinstance(order_data, dict) else None
        if not rows:
            rows = [order_data] if isinstance(order_data, dict) else []

        filled_usd = 0.0
        filled_shares = 0.0
        status = ""

        for row in rows:
            if not isinstance(row, dict):
                continue
            if not status:
                status = str(row.get("status") or row.get("state") or "").lower()
            price = self._parse_order_amount(row.get("price")) or float(maker_price)

            making_amount = self._parse_order_amount(row.get("makingAmount") or row.get("makerAmount"))
            taking_amount = self._parse_order_amount(row.get("takingAmount") or row.get("takerAmount"))
            if making_amount > 0 and taking_amount > 0:
                filled_usd += making_amount
                filled_shares += taking_amount
                continue

            matched_shares = 0.0
            for key in (
                "size_matched",
                "sizeMatched",
                "matched_size",
                "matchedSize",
                "filled_size",
                "filledSize",
                "sizeFilled",
                "filled",
            ):
                matched_shares = self._parse_order_amount(row.get(key))
                if matched_shares > 0:
                    break

            if matched_shares > 0:
                filled_shares += matched_shares
                filled_usd += matched_shares * price
            elif status in ("filled", "matched") and float(requested_shares) > 0:
                filled_shares += float(requested_shares)
                filled_usd += float(requested_shares) * price

        avg_price = round(filled_usd / filled_shares, 6) if filled_shares > 0 else float(maker_price)
        return round(filled_usd, 4), round(filled_shares, 4), avg_price, status

    def _order_fill_from_response(self, order_data: Dict, fallback_usd: float, fallback_price: float) -> Tuple[float, float, float]:
        rows = order_data.get("items") if isinstance(order_data, dict) else None
        if not rows:
            rows = [order_data] if isinstance(order_data, dict) else []

        filled_usd = 0.0
        filled_shares = 0.0
        for row in rows:
            if not isinstance(row, dict):
                continue
            filled_usd += self._parse_order_amount(row.get("makingAmount"))
            filled_shares += self._parse_order_amount(row.get("takingAmount"))

        if filled_usd <= 0:
            filled_usd = round(float(fallback_usd), 4)
        if filled_shares <= 0:
            filled_shares = round(float(fallback_usd) / max(float(fallback_price), 0.01), 4)

        avg_price = round(filled_usd / filled_shares, 6) if filled_shares > 0 else float(fallback_price)
        return round(filled_usd, 4), round(filled_shares, 4), avg_price

    def _normalize_usdc_amount(self, value) -> Optional[float]:
        try:
            amount = float(value)
        except Exception:
            return None
        if amount < 0:
            return None
        if amount > 1_000_000 and abs(amount - round(amount)) < 0.001:
            amount = amount / 1_000_000.0
        return float(amount)

    def _parse_balance_response(self, balance_text: str) -> Optional[float]:
        import ast
        import re

        def find_balance(obj) -> Optional[float]:
            if isinstance(obj, dict):
                preferred_keys = (
                    "balance",
                    "available",
                    "availableBalance",
                    "collateral",
                    "buyingPower",
                    "cash",
                )
                for key in preferred_keys:
                    if key in obj:
                        parsed = self._normalize_usdc_amount(obj.get(key))
                        if parsed is not None:
                            return parsed
                for value in obj.values():
                    parsed = find_balance(value)
                    if parsed is not None:
                        return parsed
            elif isinstance(obj, list):
                for value in obj:
                    parsed = find_balance(value)
                    if parsed is not None:
                        return parsed
            else:
                return self._normalize_usdc_amount(obj)
            return None

        text = (balance_text or "").strip()
        if not text or text.lower().startswith("error"):
            return None

        for parser in (json.loads, ast.literal_eval):
            try:
                parsed = parser(text)
                balance = find_balance(parsed)
                if balance is not None:
                    return balance
            except Exception:
                pass

        balance_match = re.search(r"balance['\"]?\s*[:=]\s*['\"]?([0-9]+(?:\.[0-9]+)?)", text, re.IGNORECASE)
        if balance_match:
            return self._normalize_usdc_amount(balance_match.group(1))

        return self._normalize_usdc_amount(text)

    async def _fetch_usdc_balance(self, session: ClientSession) -> Optional[float]:
        if session is None:
            return None
        balance_res = await session.call_tool("get_balance")
        balance_text = balance_res.content[0].text if balance_res and balance_res.content else ""
        return self._parse_balance_response(balance_text)

    async def _martingale_balance_guard(
        self,
        session: ClientSession,
        state: Dict,
        stake_usd: float,
        entry_price: float,
    ) -> Tuple[bool, Optional[float], float, Optional[float], Optional[float]]:
        estimated_fee = self._martingale_fee_for_stake(stake_usd, entry_price)
        if self.dry_run:
            return True, None, estimated_fee, None, None

        balance = await self._fetch_usdc_balance(session)
        if balance is None:
            self.log("[Martingale] Safety stop: could not read USDC balance before live order.")
            return False, None, estimated_fee, None, None

        cycle_start_balance = state.get("cycle_start_balance")
        if cycle_start_balance is None or float(cycle_start_balance or 0.0) <= 0:
            cycle_start_balance = balance
            state["cycle_start_balance"] = round(cycle_start_balance, 4)
            self.log(f"[Martingale] Cycle balance anchor set at ${cycle_start_balance:.2f}.")

        floor_fraction = min(max(float(getattr(self, "martingale_balance_floor_fraction", 0.50)), 0.0), 1.0)
        balance_floor = float(cycle_start_balance) * floor_fraction
        projected_if_loss = balance - float(stake_usd) - estimated_fee
        if projected_if_loss < balance_floor:
            self.log(
                f"[Martingale] Safety stop: ${stake_usd:.2f} stake + ${estimated_fee:.2f} est. fee "
                f"would leave ${projected_if_loss:.2f}, below {floor_fraction:.0%} cycle floor "
                f"(${balance_floor:.2f}). Abandoning recovery cycle."
            )
            return False, balance, estimated_fee, balance_floor, projected_if_loss

        return True, balance, estimated_fee, balance_floor, projected_if_loss

    def _parse_orderbook_levels(self, levels, reverse: bool = False) -> List[Tuple[float, float]]:
        parsed = []
        for level in levels or []:
            try:
                price = float(level.get("price", 0))
                size = float(level.get("size", 0))
                if price > 0 and size > 0:
                    parsed.append((price, size))
            except Exception:
                continue
        return sorted(parsed, key=lambda item: item[0], reverse=reverse)

    def _orderbook_metrics(self, book: Dict) -> Dict:
        bids = self._parse_orderbook_levels(book.get("bids", []), reverse=True) if isinstance(book, dict) else []
        asks = self._parse_orderbook_levels(book.get("asks", []), reverse=False) if isinstance(book, dict) else []
        best_bid, best_bid_size = bids[0] if bids else (0.0, 0.0)
        best_ask, best_ask_size = asks[0] if asks else (0.0, 0.0)

        def depth(levels, lower, upper):
            selected = [(price, size) for price, size in levels if lower <= price <= upper]
            return (
                round(sum(size for _, size in selected), 4),
                round(sum(price * size for price, size in selected), 4),
            )

        bid_depth_40_60, bid_notional_40_60 = depth(bids, 0.40, 0.60)
        ask_depth_40_60, ask_notional_40_60 = depth(asks, 0.40, 0.60)
        bid_depth_49_51, bid_notional_49_51 = depth(bids, 0.49, 0.51)
        ask_depth_49_51, ask_notional_49_51 = depth(asks, 0.49, 0.51)
        spread = round(best_ask - best_bid, 4) if best_bid > 0 and best_ask > 0 else None
        mid = round((best_bid + best_ask) / 2, 4) if best_bid > 0 and best_ask > 0 else None
        return {
            "best_bid": round(best_bid, 4),
            "best_bid_size": round(best_bid_size, 4),
            "best_ask": round(best_ask, 4),
            "best_ask_size": round(best_ask_size, 4),
            "spread": spread,
            "mid": mid,
            "bid_depth_40_60": bid_depth_40_60,
            "bid_notional_40_60": bid_notional_40_60,
            "ask_depth_40_60": ask_depth_40_60,
            "ask_notional_40_60": ask_notional_40_60,
            "bid_depth_49_51": bid_depth_49_51,
            "bid_notional_49_51": bid_notional_49_51,
            "ask_depth_49_51": ask_depth_49_51,
            "ask_notional_49_51": ask_notional_49_51,
        }

    def _maker_candidate_price(
        self,
        book_metrics: Dict,
        reference_price: float,
        min_price: float,
        max_price: float,
    ) -> Optional[float]:
        best_bid = float(book_metrics.get("best_bid") or 0.0)
        best_ask = float(book_metrics.get("best_ask") or 0.0)
        offset = max(float(getattr(self, "martingale_maker_price_offset", 0.01)), 0.0)
        tick = 0.01

        if best_bid <= 0 and best_ask <= 0:
            return None

        if best_bid > 0:
            candidate = best_bid + offset
        else:
            candidate = float(reference_price)

        if best_ask > 0:
            candidate = min(candidate, best_ask - tick)
        candidate = min(max(candidate, float(min_price)), float(max_price))
        candidate = math.floor(candidate * 100.0) / 100.0
        if candidate <= 0 or (best_ask > 0 and candidate >= best_ask):
            return None
        return round(candidate, 2)

    def _csv_append_row(self, filename: str, row: Dict):
        import csv

        os.makedirs(self.data_dir, exist_ok=True)
        path = os.path.join(self.data_dir, filename)
        lock = getattr(self, "_csv_lock", None)
        if lock is None:
            self._csv_lock = threading.Lock()
            lock = self._csv_lock

        with lock:
            file_exists = os.path.exists(path) and os.path.getsize(path) > 0
            row_fields = list(row.keys())
            if not file_exists:
                with open(path, "w", newline="", encoding="utf-8") as f:
                    writer = csv.DictWriter(f, fieldnames=row_fields)
                    writer.writeheader()
                    writer.writerow(row)
                return

            with open(path, "r", newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                existing_fields = list(reader.fieldnames or [])
                missing_fields = [field for field in row_fields if field not in existing_fields]
                if not missing_fields:
                    fieldnames = existing_fields
                    existing_rows = None
                else:
                    fieldnames = existing_fields + missing_fields
                    existing_rows = list(reader)

            if existing_rows is None:
                with open(path, "a", newline="", encoding="utf-8") as f:
                    writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
                    writer.writerow(row)
                return

            with open(path, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
                writer.writeheader()
                for existing_row in existing_rows:
                    writer.writerow(existing_row)
                writer.writerow(row)

    def _schedule_csv_row(self, filename: str, row: Dict):
        async def write_record():
            try:
                await asyncio.to_thread(self._csv_append_row, filename, row)
            except Exception as exc:
                self.log(f"[Warn] Failed writing {filename}: {exc}")

        task = asyncio.create_task(write_record())
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    def _safe_float(self, value, default: float = 0.0) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return default
        return number if math.isfinite(number) else default

    def _parse_iso_timestamp(self, value: str) -> Optional[float]:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            return datetime.datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None

    def _read_csv_rows(self, filename: str) -> List[Dict]:
        path = os.path.join(self.data_dir, filename)
        if not os.path.exists(path):
            return []
        try:
            with self._csv_lock:
                with open(path, "r", newline="", encoding="utf-8") as f:
                    return list(csv.DictReader(f))
        except Exception:
            return []

    def _fair_value_supervisor_allowed(self) -> Tuple[bool, str]:
        if not bool(getattr(self, "fair_value_supervisor_enabled", True)):
            return False, "disabled"
        if not self._is_fair_value_mode():
            return False, "not_fair_value"
        if not bool(self.dry_run):
            return False, "live_dry_run_false"
        if bool(getattr(self, "fair_value_live_trading_enabled", False)):
            return False, "live_orders_enabled"
        return True, "dry_run_recommendation_only"

    def _build_fair_value_supervisor_review(self, now_ts: float) -> Dict:
        lookback = max(float(getattr(self, "fair_value_supervisor_lookback_seconds", 7200.0)), 300.0)
        cutoff_ts = float(now_ts) - lookback
        entries = [
            row for row in self._read_csv_rows("fair_value_entries.csv")
            if (self._parse_iso_timestamp(row.get("event_ts_utc")) or 0.0) >= cutoff_ts
        ]
        outcomes = self._read_csv_rows("fair_value_outcomes.csv")
        outcome_by_key = {}
        for row in outcomes:
            key = f"{row.get('slug', '')}|{row.get('entry_id', '')}"
            if row.get("slug") and row.get("entry_id"):
                outcome_by_key[key] = row

        closed = []
        open_entries = []
        wins = 0
        losses = 0
        pnl = 0.0
        core_entries = 0
        core_pnl = 0.0
        momentum_entries = 0
        momentum_pnl = 0.0
        maker_entries = 0
        taker_entries = 0
        ev_sum = 0.0
        edge_sum = 0.0
        ev_count = 0
        max_stake = 0.0

        for entry in entries:
            key = f"{entry.get('slug', '')}|{entry.get('entry_id', '')}"
            outcome = outcome_by_key.get(key)
            tactic = entry.get("tactic") or "core_edge"
            route = entry.get("route") or ""
            stake = self._safe_float(entry.get("stake_usd"))
            max_stake = max(max_stake, stake)
            ev = self._safe_float(entry.get("ev_per_usd"), default=float("nan"))
            edge = self._safe_float(entry.get("edge_probability"), default=float("nan"))
            if math.isfinite(ev):
                ev_sum += ev
                ev_count += 1
            if math.isfinite(edge):
                edge_sum += edge
            if tactic == "momentum":
                momentum_entries += 1
            else:
                core_entries += 1
            if "maker" in route.lower():
                maker_entries += 1
            if "taker" in route.lower():
                taker_entries += 1

            result = str((outcome or {}).get("result") or "").upper()
            if result in ("WIN", "LOSS"):
                closed.append(entry)
                row_pnl = self._safe_float((outcome or {}).get("pnl_usd"))
                pnl += row_pnl
                if tactic == "momentum":
                    momentum_pnl += row_pnl
                else:
                    core_pnl += row_pnl
                if result == "WIN":
                    wins += 1
                else:
                    losses += 1
            else:
                open_entries.append(entry)

        signals = [
            row for row in self._read_csv_rows("fair_value_signals.csv")
            if (self._parse_iso_timestamp(row.get("sample_ts_utc")) or 0.0) >= cutoff_ts
        ]
        candidate_signals = [
            row for row in signals
            if str(row.get("decision_reason") or "") not in ("", "no_edge")
        ]
        best_signal_ev = 0.0
        if signals:
            best_signal_ev = max(
                self._safe_float(row.get("decision_ev_per_usd"))
                for row in signals
            )

        closed_count = len(closed)
        entry_count = len(entries)
        win_rate = (wins / closed_count * 100.0) if closed_count else None
        avg_ev = (ev_sum / ev_count) if ev_count else 0.0
        avg_edge = (edge_sum / ev_count) if ev_count else 0.0
        maker_rate = (maker_entries / entry_count * 100.0) if entry_count else 0.0

        recommendations = []
        if entry_count == 0:
            recommendations.append(
                "Sin entradas en la ventana: mantener recogida o relajar ligeramente core si buscamos mas muestra."
            )
        elif closed_count < 5:
            recommendations.append("Muestra pequena: no ajustar todavia salvo que siga varias horas sin entradas.")
        if closed_count >= 5 and pnl < 0 and (win_rate or 0.0) < 45.0:
            recommendations.append("PnL y win-rate flojos: subir min_ev/edge antes de pensar en production.")
        if closed_count >= 5 and pnl > 0 and maker_rate >= 50.0:
            recommendations.append("Dry run saludable: mantener parametros y ampliar muestra.")
        if momentum_entries == 0:
            recommendations.append("Momentum sigue sin muestra: revisar manana si conviene bajar su confianza/EV otro poco.")
        elif momentum_entries >= 3 and momentum_pnl < 0:
            recommendations.append("Momentum negativo: endurecerlo o pausarlo.")
        if taker_entries > maker_entries and pnl <= 0:
            recommendations.append("Demasiado taker sin PnL positivo: priorizar maker o subir EV taker.")
        if best_signal_ev < float(getattr(self, "fair_value_min_ev_per_usd", 0.0)) and entry_count == 0:
            recommendations.append("El mejor EV observado no llega al umbral actual.")

        recommendation = " ".join(recommendations) if recommendations else "Sin cambios recomendados; seguir acumulando datos."
        return {
            "event_ts_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "mode": "recommendation_only",
            "safety_status": "dry_run_only_no_autochanges",
            "lookback_hours": round(lookback / 3600.0, 3),
            "strategy_mode": self.strategy_mode,
            "dry_run": bool(self.dry_run),
            "live_trading_enabled": bool(getattr(self, "fair_value_live_trading_enabled", False)),
            "stake_usd": round(float(getattr(self, "fair_value_stake_usd", 0.0)), 4),
            "core_edge": round(float(getattr(self, "fair_value_min_taker_edge", 0.0)), 6),
            "core_ev": round(float(getattr(self, "fair_value_min_ev_per_usd", 0.0)), 6),
            "momentum_edge": round(float(getattr(self, "fair_value_momentum_min_edge", 0.0)), 6),
            "momentum_ev": round(float(getattr(self, "fair_value_momentum_min_ev_per_usd", 0.0)), 6),
            "entries": entry_count,
            "closed": closed_count,
            "open": len(open_entries),
            "wins": wins,
            "losses": losses,
            "win_rate": round(win_rate, 2) if win_rate is not None else "",
            "pnl_usd": round(pnl, 4),
            "core_entries": core_entries,
            "core_pnl": round(core_pnl, 4),
            "momentum_entries": momentum_entries,
            "momentum_pnl": round(momentum_pnl, 4),
            "maker_entries": maker_entries,
            "taker_entries": taker_entries,
            "maker_rate": round(maker_rate, 2),
            "avg_ev_per_usd": round(avg_ev, 6),
            "avg_edge_probability": round(avg_edge, 6),
            "max_stake": round(max_stake, 4),
            "signals": len(signals),
            "candidate_signals": len(candidate_signals),
            "best_signal_ev_per_usd": round(best_signal_ev, 6),
            "recommendation": recommendation,
        }

    async def _fair_value_supervisor_if_due(self, now_ts: float):
        allowed, reason = self._fair_value_supervisor_allowed()
        interval = max(float(getattr(self, "fair_value_supervisor_interval_seconds", 7200.0)), 300.0)
        if not allowed:
            last_disabled = float(getattr(self, "_last_supervisor_disabled_log_ts", 0.0) or 0.0)
            if reason.startswith("live") and now_ts - last_disabled >= interval:
                self.log(f"[Supervisor] Recommendation supervisor disabled for safety: {reason}.")
                self._last_supervisor_disabled_log_ts = now_ts
            return
        last_review = self._last_supervisor_review_ts
        if last_review is not None and now_ts - float(last_review) < interval:
            return
        self._last_supervisor_review_ts = now_ts
        review = await asyncio.to_thread(self._build_fair_value_supervisor_review, now_ts)
        self._schedule_csv_row("fair_value_supervisor_reviews.csv", review)
        self.log(
            "[Supervisor] Last "
            f"{review['lookback_hours']:.1f}h | entries={review['entries']} "
            f"closed={review['closed']} W/L={review['wins']}/{review['losses']} "
            f"pnl=${review['pnl_usd']:.2f} | core={review['core_entries']} "
            f"momentum={review['momentum_entries']} | maker={review['maker_entries']} "
            f"taker={review['taker_entries']} | rec={review['recommendation']}"
        )

    def _fair_value_entry_model_retrain_allowed(self) -> Tuple[bool, str]:
        if not bool(getattr(self, "fair_value_entry_model_auto_retrain_enabled", True)):
            return False, "disabled"
        if not bool(getattr(self, "fair_value_entry_model_enabled", True)):
            return False, "entry_model_disabled"
        if not self._is_fair_value_mode():
            return False, "not_fair_value"
        if not bool(self.dry_run):
            return False, "live_dry_run_false"
        if bool(getattr(self, "fair_value_live_trading_enabled", False)):
            return False, "live_orders_enabled"
        script_path = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", "scripts", "retrain_fair_value_entry_model.py")
        )
        if not os.path.exists(script_path):
            return False, "script_missing"
        return True, "dry_run_validated_promotion"

    async def _fair_value_entry_model_retrain_if_due(self, now_ts: float):
        allowed, reason = self._fair_value_entry_model_retrain_allowed()
        interval = max(
            float(getattr(self, "fair_value_entry_model_auto_retrain_interval_seconds", 7200.0)),
            1800.0,
        )
        if not allowed:
            if reason.startswith("live"):
                last_disabled = float(getattr(self, "_last_supervisor_disabled_log_ts", 0.0) or 0.0)
                if now_ts - last_disabled >= interval:
                    self.log(f"[FairValueModel] Auto retrain disabled for safety: {reason}.")
                    self._last_supervisor_disabled_log_ts = now_ts
            return
        if bool(getattr(self, "_entry_model_retrain_running", False)):
            return
        last_retrain = float(getattr(self, "_last_entry_model_retrain_ts", 0.0) or 0.0)
        if last_retrain and now_ts - last_retrain < interval:
            return
        self._last_entry_model_retrain_ts = now_ts
        self._entry_model_retrain_running = True
        task = asyncio.create_task(self._run_fair_value_entry_model_retrain())
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def _run_fair_value_entry_model_retrain(self):
        script_path = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", "scripts", "retrain_fair_value_entry_model.py")
        )
        root_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        min_holdout = int(getattr(self, "fair_value_entry_model_auto_retrain_min_holdout_rows", 80) or 80)
        args = [
            PYTHON_EXE,
            script_path,
            "--promote",
            "--min-holdout-rows",
            str(max(min_holdout, 20)),
        ]
        self.log(
            "[FairValueModel] Auto retrain started | "
            f"interval={float(getattr(self, 'fair_value_entry_model_auto_retrain_interval_seconds', 7200.0)) / 3600.0:.1f}h | "
            "dry-run-only validated promotion."
        )
        try:
            process = await asyncio.create_subprocess_exec(
                *args,
                cwd=root_dir,
                env=os.environ.copy(),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout_bytes, stderr_bytes = await process.communicate()
            stdout = stdout_bytes.decode("utf-8", errors="replace").strip()
            stderr = stderr_bytes.decode("utf-8", errors="replace").strip()
            if process.returncode != 0:
                details = stderr or stdout or f"exit_code={process.returncode}"
                self.log(f"[FairValueModel] Auto retrain failed: {details[-300:]}")
                return

            report_path = os.path.join(self.data_dir, "fair_value_entry_model_retrain_report.json")
            promoted = False
            candidate_auc = current_auc = candidate_brier = current_brier = None
            try:
                with open(report_path, "r", encoding="utf-8") as f:
                    report = json.load(f)
                promoted = bool(report.get("promoted"))
                candidate_auc = (report.get("candidate_holdout") or {}).get("auc")
                current_auc = (report.get("current_holdout") or {}).get("auc")
                candidate_brier = (report.get("candidate_holdout") or {}).get("brier")
                current_brier = (report.get("current_holdout") or {}).get("brier")
            except Exception:
                pass

            self._fair_value_entry_model = None
            self._fair_value_entry_model_loaded_path = None
            self._fair_value_entry_model_loaded_mtime = None
            summary = stdout.splitlines()[0] if stdout else "completed"
            if candidate_auc is not None and current_auc is not None:
                summary = (
                    f"candidate_auc={float(candidate_auc):.4f} current_auc={float(current_auc):.4f} "
                    f"candidate_brier={float(candidate_brier):.4f} current_brier={float(current_brier):.4f}"
                )
            self.log(
                "[FairValueModel] Auto retrain completed | "
                f"promoted={promoted} | {summary}"
            )
        finally:
            self._entry_model_retrain_running = False

    def _maker_touch_signal(self, book_metrics: Dict, maker_price: Optional[float]) -> bool:
        if maker_price is None:
            return False
        best_ask = float(book_metrics.get("best_ask") or 0.0)
        return best_ask > 0 and best_ask <= float(maker_price)

    def _build_orderbook_sample_row(
        self,
        slug: str,
        elapsed: float,
        token_ids: List[str],
        yes_book: Dict,
        no_book: Dict,
        target_direction: Optional[str] = None,
        maker_price: Optional[float] = None,
    ) -> Dict:
        import datetime

        yes_metrics = self._orderbook_metrics(yes_book)
        no_metrics = self._orderbook_metrics(no_book)
        target_metrics = yes_metrics if target_direction == "Yes" else no_metrics if target_direction == "No" else {}
        row = {
            "sample_ts_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "strategy_mode": self.strategy_mode,
            "dry_run": self.dry_run,
            "slug": slug,
            "elapsed_s": round(float(elapsed), 3),
            "target_direction": target_direction or "",
            "maker_price": maker_price if maker_price is not None else "",
            "maker_touch_signal": self._maker_touch_signal(target_metrics, maker_price) if target_metrics else False,
            "yes_token_id": token_ids[0] if len(token_ids) > 0 else "",
            "no_token_id": token_ids[1] if len(token_ids) > 1 else "",
        }
        for prefix, metrics in (("yes", yes_metrics), ("no", no_metrics)):
            for key, value in metrics.items():
                row[f"{prefix}_{key}"] = value
        return row

    async def fetch_order_book(self, asset_id: str) -> Dict:
        import aiohttp

        http_session = await self._get_http_session()
        url = f"https://clob.polymarket.com/book?token_id={asset_id}"
        data = None
        last_exc = None
        try:
            if not self._polymarket_backoff_active(url):
                async with http_session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as response:
                    if response.status == 200:
                        data = await response.json()
                        self._mark_polymarket_http_success(url)
        except Exception as exc:
            last_exc = exc
        if not data and last_exc is not None:
            data = await self._polymarket_get_json_fallback(url, timeout=6.0)
        if isinstance(data, dict):
            return data
        return {"bids": [], "asks": []}

    async def _record_maker_orderbook_snapshot(
        self,
        slug: str,
        start_ts: float,
        token_ids: List[str],
        target_direction: Optional[str] = None,
        maker_price: Optional[float] = None,
    ) -> Optional[Dict]:
        if len(token_ids) != 2:
            return None
        yes_book, no_book = await asyncio.gather(
            self.fetch_order_book(token_ids[0]),
            self.fetch_order_book(token_ids[1]),
            return_exceptions=True,
        )
        if isinstance(yes_book, Exception):
            yes_book = {"bids": [], "asks": []}
        if isinstance(no_book, Exception):
            no_book = {"bids": [], "asks": []}
        elapsed = time.time() - float(start_ts)
        row = self._build_orderbook_sample_row(
            slug=slug,
            elapsed=elapsed,
            token_ids=token_ids,
            yes_book=yes_book,
            no_book=no_book,
            target_direction=target_direction,
            maker_price=maker_price,
        )
        self._schedule_csv_row("martingale_maker_orderbook_samples.csv", row)
        return row

    async def _maker_orderbook_recorder_loop(self, slug: str, start_ts: float, token_ids: List[str]):
        sample_seconds = max(float(getattr(self, "martingale_maker_sample_seconds", 0.5)), 0.1)
        record_seconds = max(float(getattr(self, "martingale_maker_record_seconds", 15.0)), sample_seconds)
        try:
            while self.running:
                elapsed = time.time() - float(start_ts)
                if elapsed > record_seconds:
                    break
                context = self._martingale_maker_recorder_context.get(slug, {})
                await self._record_maker_orderbook_snapshot(
                    slug,
                    start_ts,
                    token_ids,
                    target_direction=context.get("target_direction"),
                    maker_price=context.get("maker_price"),
                )
                await asyncio.sleep(sample_seconds)
        finally:
            self._martingale_maker_recorder_tasks.pop(slug, None)
            self._martingale_maker_recorder_context.pop(slug, None)

    def _ensure_maker_orderbook_recorder(
        self,
        slug: str,
        start_ts: float,
        token_ids: List[str],
        target_direction: Optional[str] = None,
        maker_price: Optional[float] = None,
    ):
        if not self._is_martingale_maker_mode() or len(token_ids) != 2:
            return
        context = self._martingale_maker_recorder_context.setdefault(slug, {})
        if target_direction in ("Yes", "No"):
            context["target_direction"] = target_direction
        if maker_price is not None:
            context["maker_price"] = maker_price
        existing = self._martingale_maker_recorder_tasks.get(slug)
        if existing and not existing.done():
            return
        task = asyncio.create_task(self._maker_orderbook_recorder_loop(slug, start_ts, token_ids))
        self._martingale_maker_recorder_tasks[slug] = task
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    def _record_maker_entry_event(self, row: Dict):
        self._schedule_csv_row("martingale_maker_entry_events.csv", row)

    def _blind_maker_price(self, min_entry_price: float, max_entry_price: float) -> float:
        configured = float(getattr(self, "martingale_maker_blind_price", 0.50) or 0.50)
        configured = min(max(configured, float(min_entry_price)), float(max_entry_price))
        return round(configured, 2)

    def _can_try_blind_maker(self, elapsed: float, entry_end_seconds: float) -> bool:
        if not self._is_martingale_maker_mode():
            return False
        blind_end = min(
            float(getattr(self, "martingale_maker_blind_end_seconds", 15.0) or 15.0),
            float(entry_end_seconds),
        )
        return float(elapsed) <= blind_end

    async def _try_maker_entry_probe(
        self,
        session: ClientSession,
        slug: str,
        start_time,
        elapsed: float,
        entry_end_seconds: float,
        state: Dict,
        token_ids: List[str],
        target_token_id: str,
        entry_price: float,
        usd_to_buy: float,
        min_entry_price: float,
        max_entry_price: float,
        blind: bool = False,
    ) -> Dict:
        import datetime

        result = {
            "attempted": False,
            "filled": False,
            "fallback": True,
            "filled_usd": None,
            "filled_shares": None,
            "avg_entry_price": None,
            "entry_fee_usd": None,
            "maker_price": None,
            "maker_waited_seconds": 0.0,
            "maker_touch_signal": False,
            "order_id": None,
            "remaining_usd": None,
            "partial": False,
            "fallback_entry_price": None,
            "blind": bool(blind),
            "route": "taker",
        }
        if not self._is_martingale_maker_mode():
            return result

        maker_window_seconds = (
            min(
                float(getattr(self, "martingale_maker_blind_end_seconds", 15.0) or 15.0),
                float(entry_end_seconds),
            )
            if blind
            else max(float(getattr(self, "martingale_maker_wait_seconds", 5.0)), 0.0)
        )
        if float(elapsed) > maker_window_seconds:
            return result
        wait_seconds = min(
            max(maker_window_seconds - float(elapsed), 0.0),
            max(float(entry_end_seconds) - float(elapsed), 0.0),
        )
        if wait_seconds <= 0:
            return result

        if blind:
            maker_price = self._blind_maker_price(min_entry_price, max_entry_price)
        else:
            target_book = await self.fetch_order_book(target_token_id)
            target_metrics = self._orderbook_metrics(target_book)
            maker_price = self._maker_candidate_price(
                target_metrics,
                reference_price=entry_price,
                min_price=min_entry_price,
                max_price=max_entry_price,
            )
        result["maker_price"] = maker_price
        if maker_price is None:
            self.log(
                f"[MartingaleMaker] No post-only maker price available for {slug}; "
                "using taker fallback."
            )
            self._record_maker_entry_event({
                "event_ts_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "slug": slug,
                "dry_run": self.dry_run,
                "direction": state.get("direction"),
                "route": "taker_no_maker_price",
                "elapsed_s": round(float(elapsed), 3),
                "maker_price": "",
                "taker_reference_price": round(float(entry_price), 4),
                "usd": round(float(usd_to_buy), 4),
                "shares": round(float(usd_to_buy) / max(float(entry_price), 0.01), 4),
                "maker_waited_seconds": 0.0,
                "maker_touch_signal": False,
                "hypothetical_taker_fee": self._martingale_fee_for_stake(usd_to_buy, entry_price),
                "hypothetical_maker_fee": 0.0,
            })
            return result

        maker_shares = self._shares_for_limit_minimum(
            float(usd_to_buy) / max(float(maker_price), 0.01),
            maker_price,
        )
        min_limit_shares = float(getattr(self, "polymarket_min_limit_order_shares", 5.0) or 5.0)
        if maker_shares < min_limit_shares:
            self.log(
                f"[MartingaleMaker] Skipping maker for {slug}: "
                f"size={maker_shares:.2f} shares below min={min_limit_shares:.2f}. "
                "Using taker directly."
            )
            self._record_maker_entry_event({
                "event_ts_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "slug": slug,
                "dry_run": self.dry_run,
                "direction": state.get("direction"),
                "route": "taker_maker_min_size",
                "elapsed_s": round(float(elapsed), 3),
                "maker_price": maker_price,
                "taker_reference_price": round(float(entry_price), 4),
                "usd": round(float(usd_to_buy), 4),
                "shares": maker_shares,
                "maker_waited_seconds": 0.0,
                "maker_touch_signal": False,
                "hypothetical_taker_fee": self._martingale_fee_for_stake(usd_to_buy, entry_price),
                "hypothetical_maker_fee": 0.0,
            })
            result["route"] = "taker_maker_min_size"
            return result

        result["attempted"] = True
        maker_kind = "blind maker" if blind else "maker"
        self.log(
            f"[MartingaleMaker] {maker_kind.capitalize()} {'probe' if self.dry_run else 'order'} | "
            f"slug={slug} | direction={state.get('direction')} | "
            f"maker_px={maker_price:.2f} | usd=${usd_to_buy:.2f} | wait={wait_seconds:.1f}s"
        )

        sample_seconds = max(float(getattr(self, "martingale_maker_sample_seconds", 0.5)), 0.1)
        deadline = time.time() + wait_seconds
        touch_elapsed = None

        async def maybe_find_taker_price() -> Optional[float]:
            if not blind:
                return None
            try:
                fresh_price = float(await self.fetch_buy_price(target_token_id))
            except Exception:
                return None
            if fresh_price > 0 and self._entry_price_allowed(fresh_price, min_entry_price, max_entry_price):
                return round(fresh_price, 4)
            return None

        if self.dry_run:
            while True:
                snapshot = await self._record_maker_orderbook_snapshot(
                    slug=slug,
                    start_ts=start_time.timestamp(),
                    token_ids=token_ids,
                    target_direction=state.get("direction"),
                    maker_price=maker_price,
                )
                if snapshot and snapshot.get("maker_touch_signal"):
                    result["maker_touch_signal"] = True
                    touch_elapsed = float(snapshot.get("elapsed_s") or 0.0)
                    break
                fallback_price = await maybe_find_taker_price()
                if fallback_price is not None:
                    result["fallback_entry_price"] = fallback_price
                    self.log(
                        f"[MartingaleMaker] Blind maker saw taker price {fallback_price:.4f}; "
                        "using normal fallback."
                    )
                    break
                now = time.time()
                if now >= deadline:
                    break
                await asyncio.sleep(min(sample_seconds, max(deadline - now, 0.0)))

            result["maker_waited_seconds"] = round(max(time.time() - (deadline - wait_seconds), 0.0), 3)
            if result["maker_touch_signal"]:
                result.update({
                    "filled": True,
                    "fallback": False,
                    "filled_usd": round(float(usd_to_buy), 4),
                    "filled_shares": maker_shares,
                    "avg_entry_price": maker_price,
                    "entry_fee_usd": 0.0,
                    "remaining_usd": 0.0,
                    "route": "maker_simulated",
                })
                self.log(
                    f"[MartingaleMaker] Dry Run maker touch observed at elapsed={touch_elapsed:.2f}s. "
                    f"Simulating maker fill with fee=$0.00."
                )
            else:
                if blind and result.get("fallback_entry_price") is None:
                    self.log("[MartingaleMaker] Blind maker not filled and taker price is still unavailable.")
                else:
                    self.log("[MartingaleMaker] Maker probe not filled in dry run; using taker fallback.")
        else:
            if session is None:
                self.log("[MartingaleMaker] No MCP session available for live maker order; using taker fallback.")
            else:
                order_res = await session.call_tool("place_order", arguments={
                    "market_slug": slug,
                    "side": "BUY",
                    "size": maker_shares,
                    "price": maker_price,
                    "token_id": target_token_id,
                    "order_type": "GTC",
                    "post_only": True,
                    "defer_exec": False,
                })
                order_text = order_res.content[0].text if order_res and order_res.content else ""
                order_ok, order_error, order_data = self._parse_order_response(order_text, allow_live=True)
                if not order_ok:
                    self.log(f"[MartingaleMaker] Maker order rejected: {order_error[:220]}. Using taker fallback.")
                else:
                    order_id = self._extract_order_id(order_data)
                    result["order_id"] = order_id
                    if not order_id:
                        self.log("[MartingaleMaker] Maker order response had no order_id; using taker fallback.")
                    else:
                        filled_usd, filled_shares, avg_price, status = self._maker_fill_from_order_data(
                            order_data,
                            requested_shares=maker_shares,
                            maker_price=maker_price,
                        )
                        while True:
                            if filled_shares >= maker_shares * 0.98 or status in ("filled", "matched"):
                                result["maker_touch_signal"] = True
                                break
                            now = time.time()
                            if now >= deadline:
                                break
                            await asyncio.sleep(min(sample_seconds, max(deadline - now, 0.0)))
                            check_res = await session.call_tool("get_order", arguments={"order_id": order_id})
                            check_text = check_res.content[0].text if check_res and check_res.content else ""
                            check_ok, check_error, check_data = self._parse_order_response(check_text, allow_live=True)
                            if not check_ok:
                                self.log(f"[MartingaleMaker] Maker status check failed: {check_error[:180]}")
                                continue
                            filled_usd, filled_shares, avg_price, status = self._maker_fill_from_order_data(
                                check_data,
                                requested_shares=maker_shares,
                                maker_price=maker_price,
                            )
                            if filled_shares > 0:
                                result["maker_touch_signal"] = True
                            fallback_price = await maybe_find_taker_price()
                            if fallback_price is not None and filled_shares <= 0:
                                result["fallback_entry_price"] = fallback_price
                                self.log(
                                    f"[MartingaleMaker] Blind maker saw taker price {fallback_price:.4f}; "
                                    "cancelling maker and using normal fallback."
                                )
                                break

                        result["maker_waited_seconds"] = round(max(time.time() - (deadline - wait_seconds), 0.0), 3)
                        if filled_shares < maker_shares * 0.98:
                            cancel_res = await session.call_tool("cancel_order", arguments={"order_id": order_id})
                            cancel_text = cancel_res.content[0].text if cancel_res and cancel_res.content else ""
                            cancel_ok, cancel_error, cancel_data = self._parse_order_response(cancel_text, allow_live=True)
                            not_canceled = (
                                cancel_data.get("not_canceled")
                                or cancel_data.get("notCanceled")
                                if isinstance(cancel_data, dict)
                                else None
                            )
                            if not_canceled:
                                cancel_ok = False
                                cancel_error = f"not_canceled={not_canceled}"
                            if cancel_ok:
                                self.log(f"[MartingaleMaker] Cancelled unfilled maker remainder for order {order_id[:10]}...")
                            else:
                                self.log(f"[MartingaleMaker] Maker cancel warning: {cancel_error[:180]}")

                        if filled_shares > 0:
                            filled_usd = round(filled_shares * maker_price if filled_usd <= 0 else filled_usd, 4)
                            remaining_usd = max(round(float(usd_to_buy) - filled_usd, 4), 0.0)
                            complete = filled_shares >= maker_shares * 0.98 or remaining_usd <= 0.01
                            result.update({
                                "filled": True,
                                "fallback": not complete,
                                "partial": not complete,
                                "filled_usd": filled_usd,
                                "filled_shares": filled_shares,
                                "avg_entry_price": avg_price,
                                "entry_fee_usd": 0.0,
                                "remaining_usd": 0.0 if complete else remaining_usd,
                                "route": "maker_live" if complete else "maker_partial",
                            })
                            self.log(
                                f"[MartingaleMaker] Maker fill | usd=${filled_usd:.2f} | "
                                f"px={avg_price:.4f} | shares={filled_shares:.4f} | "
                                f"status={status or 'unknown'} | remaining=${result['remaining_usd'] or 0.0:.2f}"
                            )
                        else:
                            self.log("[MartingaleMaker] Maker order not filled; using taker fallback.")

        if not result["maker_waited_seconds"]:
            result["maker_waited_seconds"] = round(max(time.time() - (deadline - wait_seconds), 0.0), 3)

        self._record_maker_entry_event({
            "event_ts_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "slug": slug,
            "dry_run": self.dry_run,
            "direction": state.get("direction"),
            "route": result["route"] if result["filled"] else "taker_fallback",
            "elapsed_s": round(float(elapsed), 3),
            "maker_price": maker_price,
            "maker_order_id": result.get("order_id") or "",
            "taker_reference_price": round(float(entry_price), 4),
            "usd": round(float(usd_to_buy), 4),
            "shares": result.get("filled_shares") or maker_shares,
            "remaining_usd": result.get("remaining_usd") if result.get("remaining_usd") is not None else "",
            "maker_waited_seconds": result["maker_waited_seconds"],
            "maker_touch_signal": result["maker_touch_signal"],
            "hypothetical_taker_fee": self._martingale_fee_for_stake(usd_to_buy, entry_price),
            "hypothetical_maker_fee": 0.0,
        })
        return result

    def _record_fair_value_signal(self, row: Dict):
        self._schedule_csv_row("fair_value_signals.csv", row)

    def _record_fair_value_entry(self, row: Dict):
        self._schedule_csv_row("fair_value_entries.csv", row)

    def _record_fair_value_maker_event(self, row: Dict):
        self._schedule_csv_row("fair_value_maker_events.csv", row)

    def _record_fair_value_outcome(self, row: Dict):
        self._schedule_csv_row("fair_value_outcomes.csv", row)

    def _record_fair_value_giro_probe_entry(self, row: Dict):
        self._schedule_csv_row("fair_value_giro_probe_entries.csv", row)

    def _record_fair_value_giro_probe_outcome(self, row: Dict):
        self._schedule_csv_row("fair_value_giro_probe_outcomes.csv", row)

    def _fair_value_latest_price_after(self, boundary_ts: float) -> Tuple[Optional[float], Optional[int], Optional[float]]:
        return self._chainlink_latest_sample_after(boundary_ts)

    def _fair_value_signal_interval_elapsed(self, state: Dict, now_ts: float) -> bool:
        last = float(state.get("last_signal_sample_at", 0.0) or 0.0)
        sample_seconds = max(float(getattr(self, "fair_value_sample_seconds", 1.0)), 0.1)
        return now_ts - last >= sample_seconds

    def _fair_value_stake(self, price: float) -> float:
        configured = max(float(getattr(self, "fair_value_stake_usd", 1.0)), 0.01)
        minimum = float(getattr(self, "polymarket_min_market_buy_usd", 1.0) or 1.0)
        p = max(float(price), 0.01)
        # Limit orders also need at least 5 shares in practice; for maker
        # candidates the decision helper checks that separately.
        return round(max(configured, minimum if configured * (1.0 / p) > 0 else configured), 2)

    def _fair_value_shadow_dynamic_stake(
        self,
        tactic: str,
        base_stake_usd: float,
        edge_probability: float,
        ev_per_usd: float,
        entry_model_win_probability: float,
        price_margin: float,
        continuation_probability: float,
    ) -> Dict:
        tactic_key = str(tactic or "core_edge")
        base = round(max(float(base_stake_usd or 0.0), 0.0), 4)
        enabled = bool(getattr(self, "fair_value_shadow_dynamic_stake_enabled", True))
        eligible = tactic_key in ("momentum", "late_continuation")
        if not enabled or not eligible or base <= 0:
            return {
                "shadow_dynamic_stake_enabled": False,
                "shadow_stake_usd": base,
                "shadow_stake_multiplier": 1.0,
                "shadow_stake_reason": "disabled" if not enabled else "not_eligible",
            }

        p = max(float(entry_model_win_probability or 0.0), 0.0)
        ev = max(float(ev_per_usd or 0.0), 0.0)
        edge = max(float(edge_probability or 0.0), 0.0)
        margin = max(float(price_margin or 0.0), 0.0)
        continuation = max(float(continuation_probability or 0.0), 0.0)
        multiplier = 1.0
        reasons = ["base"]

        if tactic_key == "momentum":
            if p >= 0.70:
                multiplier += 0.25
                reasons.append("model_p>=0.70")
            if p >= 0.80:
                multiplier += 0.25
                reasons.append("model_p>=0.80")
        elif tactic_key == "late_continuation":
            if p >= 0.91 or continuation >= 0.91:
                multiplier += 0.25
                reasons.append("late_p>=0.91")
            if p >= 0.95 or continuation >= 0.95:
                multiplier += 0.25
                reasons.append("late_p>=0.95")

        if ev >= 0.12:
            multiplier += 0.25
            reasons.append("ev>=0.12")
        if ev >= 0.22:
            multiplier += 0.25
            reasons.append("ev>=0.22")
        if edge >= 0.08:
            multiplier += 0.25
            reasons.append("edge>=0.08")
        if margin >= 0.06:
            multiplier += 0.25
            reasons.append("margin>=0.06")

        min_multiplier = min(
            max(float(getattr(self, "fair_value_shadow_dynamic_stake_min_multiplier", 1.0)), 0.01),
            10.0,
        )
        max_multiplier = min(
            max(float(getattr(self, "fair_value_shadow_dynamic_stake_max_multiplier", 2.0)), min_multiplier),
            10.0,
        )
        clear_for_175 = (
            ev >= 0.12
            and edge >= 0.07
            and (p >= 0.70 or continuation >= 0.90)
        )
        clear_for_2x = (
            ev >= 0.18
            and edge >= 0.09
            and (p >= 0.78 or continuation >= 0.93)
        ) or (
            ev >= 0.24
            and edge >= 0.12
        )
        if multiplier > 1.5 and not clear_for_175:
            multiplier = 1.5
            reasons.append("cap_1.50_quality")
        if multiplier > 1.75 and not clear_for_2x:
            multiplier = 1.75
            reasons.append("cap_1.75_not_clear_2x")
        multiplier = min(max(multiplier, min_multiplier), max_multiplier)
        return {
            "shadow_dynamic_stake_enabled": True,
            "shadow_stake_usd": round(base * multiplier, 4),
            "shadow_stake_multiplier": round(multiplier, 4),
            "shadow_stake_reason": "|".join(reasons),
        }

    def _load_fair_value_entry_model(self):
        path = str(
            getattr(self, "fair_value_entry_model_path", "")
            or os.path.join(self.data_dir, "fair_value_entry_model.json")
        )
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            mtime = None
        if (
            self._fair_value_entry_model is not None
            and self._fair_value_entry_model_loaded_path == path
            and self._fair_value_entry_model_loaded_mtime == mtime
        ):
            return self._fair_value_entry_model
        try:
            model = load_entry_model(path)
            if model is None:
                fallback_path = os.path.join(
                    os.path.dirname(__file__),
                    "models",
                    "fair_value_entry_model.json",
                )
                if os.path.abspath(fallback_path) != os.path.abspath(path):
                    path = fallback_path
                    try:
                        mtime = os.path.getmtime(path)
                    except OSError:
                        mtime = None
                    model = load_entry_model(path)
        except Exception as exc:
            now_ts = time.time()
            if now_ts - float(getattr(self, "_last_fair_value_entry_model_warn_ts", 0.0) or 0.0) >= 60.0:
                self._last_fair_value_entry_model_warn_ts = now_ts
                self.log(f"[FairValueModel] Could not load entry model: {type(exc).__name__}: {str(exc)[:160]}")
            self._fair_value_entry_model = None
            self._fair_value_entry_model_loaded_path = path
            self._fair_value_entry_model_loaded_mtime = mtime
            return None
        self._fair_value_entry_model = model
        self._fair_value_entry_model_loaded_path = path
        self._fair_value_entry_model_loaded_mtime = mtime
        if model:
            self.log(
                "[FairValueModel] Entry model loaded | "
                f"rows={model.get('train_rows', '?')} | "
                f"auc={float(model.get('train_auc', 0.0) or 0.0):.3f} | "
                f"brier={float(model.get('train_brier', 0.0) or 0.0):.3f}"
            )
        return model

    def _load_cheap_reversal_model(self):
        path = str(
            getattr(self, "fair_value_giro_probe_model_path", "")
            or os.path.join(self.data_dir, "cheap_reversal", "cheap_reversal_model.json")
        )
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            mtime = None
        if (
            getattr(self, "_cheap_reversal_model", None) is not None
            and getattr(self, "_cheap_reversal_model_loaded_path", None) == path
            and getattr(self, "_cheap_reversal_model_loaded_mtime", None) == mtime
        ):
            return self._cheap_reversal_model
        try:
            model = load_cheap_reversal_model(path)
        except Exception as exc:
            now_ts = time.time()
            if now_ts - float(getattr(self, "_last_cheap_reversal_model_warn_ts", 0.0) or 0.0) >= 60.0:
                self._last_cheap_reversal_model_warn_ts = now_ts
                self.log(f"[CheapReversalModel] Could not load model: {type(exc).__name__}: {str(exc)[:160]}")
            self._cheap_reversal_model = None
            self._cheap_reversal_model_loaded_path = path
            self._cheap_reversal_model_loaded_mtime = mtime
            return None
        self._cheap_reversal_model = model
        self._cheap_reversal_model_loaded_path = path
        self._cheap_reversal_model_loaded_mtime = mtime
        if model:
            metrics = model.get("training_metrics") or {}
            self.log(
                "[CheapReversalModel] Model loaded | "
                f"version={model.get('model_version', '?')} | "
                f"rows={metrics.get('rows', '?')} | "
                f"auc={float(metrics.get('auc', 0.0) or 0.0):.3f} | "
                f"brier={float(metrics.get('brier', 0.0) or 0.0):.3f}"
            )
        return model

    def _cheap_reversal_candidate_features(
        self,
        fair_data: Dict,
        side: str,
        metrics: Dict,
        price: float,
        elapsed: float,
    ) -> Dict:
        try:
            delta_bps = float(fair_data.get("delta_bps") or 0.0)
        except (TypeError, ValueError):
            delta_bps = 0.0
        elapsed = float(elapsed)
        elapsed_minutes = elapsed / 60.0 if elapsed > 0 else 0.0
        raw_yes = float(fair_data.get("raw_fair_yes") or fair_data.get("fair_yes") or 0.5)
        fair_yes = float(fair_data.get("fair_yes") or 0.5)
        market_yes_raw = fair_data.get("market_fair_yes")
        try:
            market_yes = float(market_yes_raw)
        except (TypeError, ValueError):
            market_yes = 0.5
        if side == "Yes":
            fair_probability = fair_yes
            raw_probability = raw_yes
            market_probability = market_yes
        else:
            fair_probability = 1.0 - fair_yes
            raw_probability = 1.0 - raw_yes
            market_probability = 1.0 - market_yes
        return {
            "elapsed_s": elapsed,
            "remaining_s": max(300.0 - elapsed, 0.0),
            "elapsed_fraction": min(max(elapsed / 300.0, 0.0), 1.0),
            "price": float(price),
            "maker_price": float(metrics.get("best_bid") or 0.0),
            "side_spread": float(metrics.get("spread") or 0.0),
            "side_mid": float(metrics.get("mid") or 0.0),
            "side_bid_depth_40_60": float(metrics.get("bid_depth_40_60") or 0.0),
            "side_ask_depth_40_60": float(metrics.get("ask_depth_40_60") or 0.0),
            "is_yes": 1.0 if side == "Yes" else 0.0,
            "delta_bps": delta_bps,
            "abs_delta_bps": abs(delta_bps),
            "velocity_bps_per_min": delta_bps / elapsed_minutes if elapsed_minutes > 0 else 0.0,
            "fair_probability": fair_probability,
            "raw_fair_probability": raw_probability,
            "market_probability": market_probability,
            "confidence": float(fair_data.get("confidence") or 0.0),
            "continuation_probability": float(fair_data.get("continuation_probability") or 0.5),
            "contrarian_probability": float(fair_data.get("contrarian_probability") or 0.5),
            "kalman_delta_bps": float(fair_data.get("kalman_delta_bps") or 0.0),
            "kalman_velocity_bps_per_min": float(fair_data.get("kalman_velocity_bps_per_min") or 0.0),
            "kalman_projected_delta_bps": float(fair_data.get("kalman_projected_delta_bps") or 0.0),
            "kalman_residual_bps": float(fair_data.get("kalman_residual_bps") or 0.0),
            "kalman_abs_residual_bps": float(fair_data.get("kalman_abs_residual_bps") or 0.0),
            "kalman_uncertainty_bps": float(fair_data.get("kalman_uncertainty_bps") or 0.0),
            "kalman_trend_agreement": float(fair_data.get("kalman_trend_agreement") or 0.0),
        }

    def _fair_value_entry_model_features(
        self,
        decision,
        tactic: str,
        elapsed: float,
        state: Dict,
    ) -> Dict:
        price = max(float(getattr(decision, "price", 0.0) or 0.0), 0.01)
        stake = self._fair_value_stake(price)
        if tactic == "momentum":
            stake = round(
                stake * float(getattr(self, "fair_value_momentum_stake_multiplier", 1.0)),
                2,
            )
        elif tactic == "late_continuation":
            stake = round(
                stake * float(getattr(self, "fair_value_late_continuation_stake_multiplier", 1.0)),
                2,
            )
        kalman_features = ((state.get("kalman") or {}).get("features") or {})
        fee = 0.0 if str(getattr(decision, "route", "")).startswith("maker") else taker_fee_for_stake(
            stake,
            price,
            float(getattr(self, "martingale_taker_fee_rate", 0.07)),
        )
        return {
            "entry_number": len(self._fair_value_entries(state)) + 1,
            "price": price,
            "elapsed": float(elapsed),
            "edge": float(getattr(decision, "edge_probability", 0.0) or 0.0),
            "fair": float(getattr(decision, "fair_probability", 0.0) or 0.0),
            "ev": float(getattr(decision, "ev_per_usd", 0.0) or 0.0),
            "fee": fee,
            "direction_yes": 1.0 if getattr(decision, "side", "") == "Yes" else 0.0,
            "route_maker": 1.0 if str(getattr(decision, "route", "")).startswith("maker") else 0.0,
            "route_taker": 1.0 if getattr(decision, "route", "") == "taker" else 0.0,
            "tactic_momentum": 1.0 if tactic == "momentum" else 0.0,
            "tactic_late_continuation": 1.0 if tactic == "late_continuation" else 0.0,
            "is_late_60": 1.0 if float(elapsed) >= 60.0 else 0.0,
            "is_late_180": 1.0 if float(elapsed) >= 180.0 else 0.0,
            "kalman_delta_bps": float(kalman_features.get("kalman_delta_bps", 0.0) or 0.0),
            "kalman_velocity_bps_per_min": float(kalman_features.get("kalman_velocity_bps_per_min", 0.0) or 0.0),
            "kalman_projected_delta_bps": float(kalman_features.get("kalman_projected_delta_bps", 0.0) or 0.0),
            "kalman_residual_bps": float(kalman_features.get("kalman_residual_bps", 0.0) or 0.0),
            "kalman_abs_residual_bps": float(kalman_features.get("kalman_abs_residual_bps", 0.0) or 0.0),
            "kalman_uncertainty_bps": float(kalman_features.get("kalman_uncertainty_bps", 0.0) or 0.0),
            "kalman_trend_agreement": float(kalman_features.get("kalman_trend_agreement", 0.0) or 0.0),
        }

    def _fair_value_apply_entry_model_guard(
        self,
        decision,
        tactic: str,
        elapsed: float,
        state: Dict,
    ):
        if not decision or not decision.should_enter:
            return decision
        if not bool(getattr(self, "fair_value_entry_model_enabled", True)):
            return decision
        model = self._load_fair_value_entry_model()
        if not model:
            return decision
        features = self._fair_value_entry_model_features(decision, tactic, elapsed, state)
        probability = score_entry_candidate(model, features)
        if tactic == "momentum":
            minimum = float(getattr(self, "fair_value_momentum_entry_model_min_win_prob", 0.67))
            maximum = 1.0
        elif tactic == "late_continuation":
            minimum = float(getattr(self, "fair_value_late_continuation_entry_model_min_win_prob", 0.0))
            maximum = 1.0
        else:
            minimum = float(getattr(self, "fair_value_core_entry_model_min_win_prob", 0.47))
            maximum = float(getattr(self, "fair_value_core_entry_model_max_win_prob", 0.50))
        minimum = min(max(minimum, 0.0), 1.0)
        maximum = min(max(maximum, minimum), 1.0)
        if probability < minimum:
            return replace(
                decision,
                should_enter=False,
                entry_model_win_probability=round(float(probability), 6),
                entry_model_min_probability=round(float(minimum), 6),
                entry_model_enabled=True,
                reason=f"entry_model_guard_{probability:.3f}_lt_{minimum:.3f}",
            )
        if probability > maximum:
            return replace(
                decision,
                should_enter=False,
                entry_model_win_probability=round(float(probability), 6),
                entry_model_min_probability=round(float(minimum), 6),
                entry_model_enabled=True,
                reason=f"entry_model_guard_{probability:.3f}_gt_{maximum:.3f}",
            )
        return replace(
            decision,
            entry_model_win_probability=round(float(probability), 6),
            entry_model_min_probability=round(float(minimum), 6),
            entry_model_enabled=True,
        )

    def _fair_value_apply_core_direction_filter(self, decision, fair_data: Dict):
        if not decision or not decision.should_enter:
            return decision
        if not bool(getattr(self, "fair_value_core_contrarian_only", False)):
            return decision
        delta = fair_data.get("delta_usd")
        try:
            delta_value = float(delta)
        except (TypeError, ValueError):
            delta_value = 0.0
        if abs(delta_value) < 1e-9:
            return replace(decision, should_enter=False, reason="core_contrarian_no_delta")
        is_contrarian = (
            (delta_value > 0 and decision.side == "No")
            or (delta_value < 0 and decision.side == "Yes")
        )
        if not is_contrarian:
            return replace(decision, should_enter=False, reason="core_contrarian_only")
        return decision

    def _fair_value_orderbook_row(
        self,
        slug: str,
        elapsed: float,
        token_ids: List[str],
        yes_book: Dict,
        no_book: Dict,
        fair_data: Dict,
        decision,
        opening_price: Optional[float],
        latest_price: Optional[float],
        latest_offset: Optional[float],
        state: Dict,
        tactic: str = "",
    ) -> Dict:
        row = self._build_orderbook_sample_row(
            slug=slug,
            elapsed=elapsed,
            token_ids=token_ids,
            yes_book=yes_book,
            no_book=no_book,
            target_direction=decision.side if decision and decision.should_enter else "",
            maker_price=decision.maker_price if decision and decision.route == "maker" else None,
        )
        row.update({
            "model_version": "btc_empirical_continuation_market_blend_v4",
            "price_source": fair_data.get("price_source", ""),
            "opening_price": round(float(opening_price), 8) if opening_price else "",
            "latest_price": round(float(latest_price), 8) if latest_price else "",
            "latest_offset_s": round(float(latest_offset), 3) if latest_offset is not None else "",
            "model_family": fair_data.get("model_family", ""),
            "continuation_probability": fair_data.get("continuation_probability", ""),
            "contrarian_probability": fair_data.get("contrarian_probability", ""),
            "raw_fair_yes": fair_data.get("raw_fair_yes", ""),
            "raw_fair_no": fair_data.get("raw_fair_no", ""),
            "market_fair_yes": fair_data.get("market_fair_yes", ""),
            "market_blend_weight": fair_data.get("market_blend_weight", ""),
            "fair_yes": fair_data.get("fair_yes"),
            "fair_no": fair_data.get("fair_no"),
            "delta_usd": fair_data.get("delta_usd") if fair_data.get("delta_usd") is not None else "",
            "delta_bps": fair_data.get("delta_bps") if fair_data.get("delta_bps") is not None else "",
            "confidence": fair_data.get("confidence"),
            "kalman_delta_bps": fair_data.get("kalman_delta_bps", ""),
            "kalman_velocity_bps_per_min": fair_data.get("kalman_velocity_bps_per_min", ""),
            "kalman_projected_delta_bps": fair_data.get("kalman_projected_delta_bps", ""),
            "kalman_residual_bps": fair_data.get("kalman_residual_bps", ""),
            "kalman_abs_residual_bps": fair_data.get("kalman_abs_residual_bps", ""),
            "kalman_uncertainty_bps": fair_data.get("kalman_uncertainty_bps", ""),
            "kalman_trend_agreement": fair_data.get("kalman_trend_agreement", ""),
            "decision_side": decision.side if decision else "",
            "decision_route": decision.route if decision else "",
            "decision_price": decision.price if decision else "",
            "decision_edge_probability": decision.edge_probability if decision else "",
            "decision_ev_per_usd": decision.ev_per_usd if decision else "",
            "decision_break_even_price": decision.break_even_price if decision else "",
            "decision_max_acceptable_price": decision.max_acceptable_price if decision else "",
            "decision_price_margin": decision.price_margin if decision else "",
            "decision_continuation_probability": decision.continuation_probability if decision else "",
            "decision_abs_delta_bps": decision.abs_delta_bps if decision else "",
            "decision_entry_model_win_probability": (
                decision.entry_model_win_probability
                if decision and getattr(decision, "entry_model_enabled", False)
                else ""
            ),
            "decision_entry_model_min_probability": (
                decision.entry_model_min_probability
                if decision and getattr(decision, "entry_model_enabled", False)
                else ""
            ),
            "decision_entry_model_enabled": (
                bool(getattr(decision, "entry_model_enabled", False)) if decision else False
            ),
            "decision_reason": decision.reason if decision else "",
            "decision_tactic": tactic,
            "active_entry_done": bool(self._fair_value_entries(state)),
            "open_entry_count": len(self._fair_value_entries(state)),
            "maker_candidate_active": bool(state.get("maker_candidate")),
        })
        return row

    async def _fair_value_market_context(self, slug: str, market_data: Dict) -> Optional[Dict]:
        token_ids = self._extract_clob_token_ids(market_data)
        if len(token_ids) != 2:
            return None
        yes_book, no_book = await asyncio.gather(
            self.fetch_order_book(token_ids[0]),
            self.fetch_order_book(token_ids[1]),
            return_exceptions=True,
        )
        if isinstance(yes_book, Exception):
            yes_book = {"bids": [], "asks": []}
        if isinstance(no_book, Exception):
            no_book = {"bids": [], "asks": []}
        yes_metrics = self._orderbook_metrics(yes_book)
        no_metrics = self._orderbook_metrics(no_book)
        return {
            "slug": slug,
            "token_ids": token_ids,
            "yes_book": yes_book,
            "no_book": no_book,
            "yes_metrics": yes_metrics,
            "no_metrics": no_metrics,
        }

    def _fair_value_decision(
        self,
        fair_yes: float,
        yes_metrics: Dict,
        no_metrics: Dict,
        state: Dict,
        fair_data: Optional[Dict] = None,
    ):
        prefer_maker = bool(getattr(self, "fair_value_prefer_maker", True))
        min_taker_edge = max(
            float(getattr(self, "fair_value_min_taker_edge", 0.10)),
            float(getattr(self, "fair_value_taker_guard_min_edge", 0.06)),
        )
        allowed_sides = None
        if bool(getattr(self, "fair_value_core_contrarian_only", False)):
            delta = (fair_data or {}).get("delta_usd")
            try:
                delta_value = float(delta)
            except (TypeError, ValueError):
                delta_value = 0.0
            if delta_value > 0:
                allowed_sides = {"No"}
            elif delta_value < 0:
                allowed_sides = {"Yes"}
            else:
                return FairValueDecision(should_enter=False, reason="core_contrarian_no_delta")
        allow_maker = not (
            not self.dry_run
            and bool(getattr(self, "fair_value_live_trading_enabled", False))
            and state.get("maker_attempted_slug") == state.get("active_slug")
        )
        decision = best_decision(
            fair_yes=fair_yes,
            yes_metrics=yes_metrics,
            no_metrics=no_metrics,
            min_taker_edge=min_taker_edge,
            min_maker_edge=float(getattr(self, "fair_value_min_maker_edge", 0.10)),
            min_ev_per_usd=float(getattr(self, "fair_value_min_ev_per_usd", 0.30)),
            min_price=float(getattr(
                self,
                "fair_value_core_min_price",
                getattr(self, "fair_value_min_price", 0.35),
            )),
            max_price=float(getattr(
                self,
                "fair_value_core_max_price",
                getattr(self, "fair_value_max_price", 0.50),
            )),
            fee_rate=float(getattr(self, "martingale_taker_fee_rate", 0.07)),
            prefer_maker=prefer_maker,
            min_limit_shares=float(getattr(self, "polymarket_min_limit_order_shares", 5.0) or 5.0),
            stake_usd=float(getattr(self, "fair_value_stake_usd", 1.0)),
            allow_maker=allow_maker,
            allowed_sides=allowed_sides,
        )
        return self._fair_value_apply_taker_guard(decision)

    def _fair_value_apply_taker_guard(self, decision):
        if not decision or not decision.should_enter or decision.route != "taker":
            return decision
        min_edge = float(getattr(self, "fair_value_taker_guard_min_edge", 0.06))
        min_ev = float(getattr(self, "fair_value_taker_guard_min_ev_per_usd", 0.12))
        if float(decision.edge_probability or 0.0) < min_edge:
            return FairValueDecision(should_enter=False, reason="taker_guard_edge")
        if float(decision.ev_per_usd or 0.0) < min_ev:
            return FairValueDecision(should_enter=False, reason="taker_guard_ev")
        return decision

    def _fair_value_momentum_decision(
        self,
        fair_yes: float,
        fair_data: Dict,
        yes_metrics: Dict,
        no_metrics: Dict,
    ):
        confidence = float(fair_data.get("confidence") or 0.0)
        min_confidence = float(getattr(self, "fair_value_momentum_min_confidence", 0.55))
        if confidence < min_confidence:
            return FairValueDecision(should_enter=False)
        return best_decision(
            fair_yes=fair_yes,
            yes_metrics=yes_metrics,
            no_metrics=no_metrics,
            min_taker_edge=max(
                float(getattr(self, "fair_value_momentum_min_edge", 0.02)),
                float(getattr(self, "fair_value_taker_guard_min_edge", 0.06)),
            ),
            min_maker_edge=1.0,
            min_ev_per_usd=max(
                float(getattr(self, "fair_value_momentum_min_ev_per_usd", 0.05)),
                float(getattr(self, "fair_value_taker_guard_min_ev_per_usd", 0.12)),
            ),
            min_price=float(getattr(self, "fair_value_min_price", 0.35)),
            max_price=float(getattr(self, "fair_value_momentum_max_price", 0.90)),
            fee_rate=float(getattr(self, "martingale_taker_fee_rate", 0.07)),
            prefer_maker=False,
            min_limit_shares=float(getattr(self, "polymarket_min_limit_order_shares", 5.0) or 5.0),
            stake_usd=float(getattr(self, "fair_value_stake_usd", 1.0))
            * float(getattr(self, "fair_value_momentum_stake_multiplier", 1.0)),
            allow_taker=True,
            allow_maker=False,
        )

    def _fair_value_max_taker_price_for_ev(
        self,
        fair_probability: float,
        min_ev_per_usd: float,
        fee_rate: float,
    ) -> float:
        target_ev = max(float(min_ev_per_usd), 0.0)
        fair = min(max(float(fair_probability), 0.0), 1.0)
        if fair <= 0.01:
            return 0.0
        if taker_ev_per_usd(fair, 0.01, fee_rate) < target_ev:
            return 0.0
        if taker_ev_per_usd(fair, 0.99, fee_rate) >= target_ev:
            return 0.99
        low, high = 0.01, 0.99
        for _ in range(40):
            mid = (low + high) / 2.0
            if taker_ev_per_usd(fair, mid, fee_rate) >= target_ev:
                low = mid
            else:
                high = mid
        return round(low, 6)

    def _fair_value_late_continuation_decision(
        self,
        fair_data: Dict,
        yes_metrics: Dict,
        no_metrics: Dict,
        state: Dict,
    ):
        delta_bps = fair_data.get("delta_bps")
        try:
            delta_bps_value = float(delta_bps)
        except (TypeError, ValueError):
            return FairValueDecision(should_enter=False, reason="late_continuation_no_delta")

        abs_delta_bps = abs(delta_bps_value)
        min_abs_delta = float(getattr(self, "fair_value_late_continuation_min_abs_delta_bps", 5.0))
        if abs_delta_bps < min_abs_delta:
            return FairValueDecision(should_enter=False, reason="late_continuation_delta_too_small")

        continuation_probability = float(fair_data.get("continuation_probability") or 0.5)
        min_probability = float(getattr(self, "fair_value_late_continuation_min_probability", 0.87))
        if continuation_probability < min_probability:
            return FairValueDecision(should_enter=False, reason="late_continuation_probability_too_low")
        max_probability = float(getattr(self, "fair_value_late_continuation_max_probability", 0.95))
        if continuation_probability > max_probability:
            return FairValueDecision(should_enter=False, reason="late_continuation_probability_too_high")

        side = "Yes" if delta_bps_value > 0 else "No"
        fair_yes = continuation_probability if side == "Yes" else 1.0 - continuation_probability
        min_ev = float(getattr(self, "fair_value_late_continuation_min_ev_per_usd", 0.015))
        fee_rate = float(getattr(self, "martingale_taker_fee_rate", 0.07))
        break_even_price = self._fair_value_max_taker_price_for_ev(
            continuation_probability,
            0.0,
            fee_rate,
        )
        ev_max_price = self._fair_value_max_taker_price_for_ev(
            continuation_probability,
            min_ev,
            fee_rate,
        )
        price_buffer = float(getattr(self, "fair_value_late_continuation_price_buffer", 0.01))
        max_acceptable_price = max(ev_max_price - max(price_buffer, 0.0), 0.0)
        max_acceptable_price = min(
            max_acceptable_price,
            float(getattr(self, "fair_value_late_continuation_max_price", 0.95)),
        )
        if max_acceptable_price <= 0:
            return FairValueDecision(should_enter=False, reason="late_continuation_no_acceptable_price")

        allow_maker = bool(getattr(self, "fair_value_prefer_maker", True))
        if (
            not self.dry_run
            and bool(getattr(self, "fair_value_live_trading_enabled", False))
            and state.get("maker_attempted_slug") == state.get("active_slug")
        ):
            allow_maker = False

        decision = best_decision(
            fair_yes=fair_yes,
            yes_metrics=yes_metrics,
            no_metrics=no_metrics,
            min_taker_edge=0.0,
            min_maker_edge=0.0,
            min_ev_per_usd=min_ev,
            min_price=float(getattr(self, "fair_value_min_price", 0.35)),
            max_price=max_acceptable_price,
            fee_rate=fee_rate,
            prefer_maker=False,
            min_limit_shares=float(getattr(self, "polymarket_min_limit_order_shares", 5.0) or 5.0),
            stake_usd=float(getattr(self, "fair_value_stake_usd", 1.0))
            * float(getattr(self, "fair_value_late_continuation_stake_multiplier", 1.0)),
            allow_taker=True,
            allow_maker=allow_maker,
            allowed_sides={side},
        )
        if not decision or not decision.should_enter:
            return replace(
                FairValueDecision(should_enter=False, reason="late_continuation_no_price_edge"),
                break_even_price=round(float(break_even_price), 6),
                max_acceptable_price=round(float(max_acceptable_price), 6),
                continuation_probability=round(float(continuation_probability), 6),
                abs_delta_bps=round(float(abs_delta_bps), 6),
            )

        price_margin = float(max_acceptable_price) - float(decision.price or 0.0)
        return replace(
            decision,
            reason="late_continuation",
            break_even_price=round(float(break_even_price), 6),
            max_acceptable_price=round(float(max_acceptable_price), 6),
            price_margin=round(float(price_margin), 6),
            continuation_probability=round(float(continuation_probability), 6),
            abs_delta_bps=round(float(abs_delta_bps), 6),
        )

    def _fair_value_giro_probe_decision(
        self,
        fair_data: Dict,
        yes_metrics: Dict,
        no_metrics: Dict,
        elapsed: float,
    ):
        if not bool(getattr(self, "fair_value_giro_probe_enabled", True)):
            return FairValueDecision(should_enter=False, reason="giro_probe_disabled")
        if not self.dry_run:
            return FairValueDecision(should_enter=False, reason="giro_probe_dry_run_only")

        start = float(getattr(self, "fair_value_giro_probe_start_seconds", 0.0))
        end = float(getattr(self, "fair_value_giro_probe_end_seconds", 300.0))
        if not (start <= float(elapsed) <= end):
            return FairValueDecision(should_enter=False, reason="giro_probe_outside_time")

        try:
            delta_bps = float(fair_data.get("delta_bps"))
        except (TypeError, ValueError):
            return FairValueDecision(should_enter=False, reason="giro_probe_no_delta")
        abs_delta_bps = abs(delta_bps)
        min_abs_delta = float(getattr(self, "fair_value_giro_probe_min_abs_delta_bps", 0.5))
        if abs_delta_bps < min_abs_delta:
            return FairValueDecision(should_enter=False, reason="giro_probe_delta_too_small")

        side = "No" if delta_bps > 0 else "Yes"
        metrics = yes_metrics if side == "Yes" else no_metrics
        price = float(metrics.get("best_ask") or 0.0)
        min_price = float(getattr(self, "fair_value_giro_probe_min_price", 0.15))
        max_price = float(getattr(self, "fair_value_giro_probe_max_price", 0.42))
        if not (min_price <= price <= max_price):
            return FairValueDecision(should_enter=False, reason="giro_probe_price_outside_range")

        fair_raw = fair_data.get("fair_yes") if side == "Yes" else fair_data.get("fair_no")
        raw_rule_probability = float(fair_raw or 0.0)
        fair_probability = raw_rule_probability
        model_enabled = False
        model_version = ""
        if bool(getattr(self, "fair_value_giro_probe_model_enabled", False)):
            model = self._load_cheap_reversal_model()
            if model:
                features = self._cheap_reversal_candidate_features(
                    fair_data=fair_data,
                    side=side,
                    metrics=metrics,
                    price=price,
                    elapsed=elapsed,
                )
                fair_probability = score_cheap_reversal_candidate(model, features)
                model_enabled = True
                model_version = str(model.get("model_version") or "")
        fee_rate = float(getattr(self, "martingale_taker_fee_rate", 0.07))
        breakeven = taker_breakeven_probability(price, fee_rate)
        edge = fair_probability - breakeven
        ev = taker_ev_per_usd(fair_probability, price, fee_rate)
        min_probability = float(getattr(self, "fair_value_giro_probe_min_probability", 0.45))
        if fair_probability < min_probability:
            return FairValueDecision(
                should_enter=False,
                reason="giro_probe_probability_too_low",
                fair_probability=round(fair_probability, 6),
                cheap_reversal_model_probability=round(fair_probability, 6) if model_enabled else 0.0,
                cheap_reversal_model_enabled=model_enabled,
                cheap_reversal_model_version=model_version,
                cheap_reversal_raw_probability=round(raw_rule_probability, 6),
            )
        min_ev = float(getattr(self, "fair_value_giro_probe_min_ev_per_usd", 0.05))
        if ev < min_ev:
            return FairValueDecision(
                should_enter=False,
                reason="giro_probe_ev_too_low",
                fair_probability=round(fair_probability, 6),
                edge_probability=round(edge, 6),
                ev_per_usd=round(ev, 6),
                cheap_reversal_model_probability=round(fair_probability, 6) if model_enabled else 0.0,
                cheap_reversal_model_enabled=model_enabled,
                cheap_reversal_model_version=model_version,
                cheap_reversal_raw_probability=round(raw_rule_probability, 6),
            )
        confidence = float(fair_data.get("confidence") or 0.0)
        min_confidence = float(getattr(self, "fair_value_giro_probe_min_confidence", 0.10))
        if confidence < min_confidence:
            return FairValueDecision(
                should_enter=False,
                reason="giro_probe_confidence_too_low",
                fair_probability=round(fair_probability, 6),
                edge_probability=round(edge, 6),
                ev_per_usd=round(ev, 6),
                cheap_reversal_model_probability=round(fair_probability, 6) if model_enabled else 0.0,
                cheap_reversal_model_enabled=model_enabled,
                cheap_reversal_model_version=model_version,
                cheap_reversal_raw_probability=round(raw_rule_probability, 6),
            )
        return FairValueDecision(
            should_enter=True,
            side=side,
            route="paper_taker",
            price=round(price, 4),
            fair_probability=round(fair_probability, 6),
            edge_probability=round(edge, 6),
            ev_per_usd=round(ev, 6),
            fee_fraction=round(max(fee_rate, 0.0) * (1.0 - price), 6),
            cheap_reversal_model_probability=round(fair_probability, 6) if model_enabled else 0.0,
            cheap_reversal_model_enabled=model_enabled,
            cheap_reversal_model_version=model_version,
            cheap_reversal_raw_probability=round(raw_rule_probability, 6),
            continuation_probability=round(float(fair_data.get("continuation_probability") or 0.5), 6),
            abs_delta_bps=round(float(abs_delta_bps), 6),
            reason="cheap_reversal_model" if model_enabled else "giro_probe_cheap_contrarian",
        )

    def _fair_value_giro_probe_allows(
        self,
        probe_state: Dict,
        slug: str,
        decision,
    ) -> Tuple[bool, str]:
        if not decision or not decision.should_enter:
            return False, getattr(decision, "reason", "no_decision")
        windows = probe_state.setdefault("windows", {})
        entries = windows.setdefault(slug, [])
        max_entries = int(getattr(self, "fair_value_giro_probe_max_entries_per_window", 4) or 0)
        if max_entries > 0 and len(entries) >= max_entries:
            return False, "giro_probe_window_limit"
        price_step = float(getattr(self, "fair_value_giro_probe_min_price_step", 0.03))
        for entry in entries:
            if entry.get("direction") != decision.side:
                continue
            entry_price = float(entry.get("entry_price", 0.0) or 0.0)
            if abs(entry_price - float(decision.price or 0.0)) < price_step:
                return False, "giro_probe_duplicate_price_bucket"
        return True, "ok"

    def _record_fair_value_giro_probe_signal(
        self,
        probe_state: Dict,
        slug: str,
        decision,
        fair_data: Dict,
        token_ids: List[str],
        elapsed: float,
    ) -> bool:
        allowed, reason = self._fair_value_giro_probe_allows(probe_state, slug, decision)
        if not allowed:
            return False

        import datetime

        token_index = 0 if decision.side == "Yes" else 1
        token_id = token_ids[token_index] if token_index < len(token_ids) else ""
        stake = round(
            self._fair_value_stake(decision.price)
            * float(getattr(self, "fair_value_giro_probe_stake_multiplier", 1.0)),
            2,
        )
        price = max(float(decision.price), 0.01)
        shares = round(stake / price, 4)
        fee = taker_fee_for_stake(
            stake,
            price,
            float(getattr(self, "martingale_taker_fee_rate", 0.07)),
        )
        entry_id = int(probe_state.get("next_entry_id", 1) or 1)
        probe_state["next_entry_id"] = entry_id + 1
        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
        kalman_features = fair_data or {}
        row = {
            "event_ts_utc": now_iso,
            "slug": slug,
            "entry_id": entry_id,
            "tactic": "giro_probe",
            "dry_run": self.dry_run,
            "live_trading_enabled": bool(getattr(self, "fair_value_live_trading_enabled", False)),
            "direction": decision.side,
            "target_token_id": token_id,
            "route": decision.route,
            "elapsed_s": round(float(elapsed), 3),
            "price": round(price, 6),
            "stake_usd": stake,
            "shares": shares,
            "fee_usd": fee,
            "fair_probability": round(float(decision.fair_probability), 6),
            "cheap_model_probability": round(float(getattr(decision, "cheap_reversal_model_probability", 0.0) or 0.0), 6),
            "cheap_raw_probability": round(float(getattr(decision, "cheap_reversal_raw_probability", 0.0) or 0.0), 6),
            "cheap_model_enabled": bool(getattr(decision, "cheap_reversal_model_enabled", False)),
            "cheap_model_version": getattr(decision, "cheap_reversal_model_version", ""),
            "edge_probability": round(float(decision.edge_probability), 6),
            "ev_per_usd": round(float(decision.ev_per_usd), 6),
            "trigger_reason": decision.reason,
            "delta_bps": fair_data.get("delta_bps", ""),
            "raw_fair_yes": fair_data.get("raw_fair_yes", ""),
            "fair_yes": fair_data.get("fair_yes", ""),
            "fair_no": fair_data.get("fair_no", ""),
            "market_fair_yes": fair_data.get("market_fair_yes", ""),
            "continuation_probability": fair_data.get("continuation_probability", ""),
            "contrarian_probability": fair_data.get("contrarian_probability", ""),
            "confidence": fair_data.get("confidence", ""),
            "price_source": fair_data.get("price_source", ""),
            "kalman_delta_bps": kalman_features.get("kalman_delta_bps", ""),
            "kalman_velocity_bps_per_min": kalman_features.get("kalman_velocity_bps_per_min", ""),
            "kalman_projected_delta_bps": kalman_features.get("kalman_projected_delta_bps", ""),
            "kalman_residual_bps": kalman_features.get("kalman_residual_bps", ""),
            "kalman_abs_residual_bps": kalman_features.get("kalman_abs_residual_bps", ""),
            "kalman_uncertainty_bps": kalman_features.get("kalman_uncertainty_bps", ""),
            "kalman_trend_agreement": kalman_features.get("kalman_trend_agreement", ""),
        }
        entry = {
            "entry_id": entry_id,
            "direction": decision.side,
            "target_token_id": token_id,
            "entry_price": round(price, 6),
            "entry_shares": shares,
            "stake_usd": stake,
            "entry_fee_usd": fee,
            "elapsed_s": round(float(elapsed), 3),
            "opened_at": now_iso,
            "fair_probability": row["fair_probability"],
            "cheap_model_probability": row["cheap_model_probability"],
            "cheap_raw_probability": row["cheap_raw_probability"],
            "cheap_model_enabled": row["cheap_model_enabled"],
            "cheap_model_version": row["cheap_model_version"],
            "edge_probability": row["edge_probability"],
            "ev_per_usd": row["ev_per_usd"],
            "trigger_reason": decision.reason,
            "delta_bps": row["delta_bps"],
            "price_source": row["price_source"],
        }
        probe_state.setdefault("windows", {}).setdefault(slug, []).append(entry)
        self._record_fair_value_giro_probe_entry(row)
        self.log(
            f"[GiroProbe] PAPER ENTRY | slug={slug} | #{len(probe_state['windows'][slug])} | "
            f"direction={decision.side} | px={price:.4f} | p={float(decision.fair_probability):.3f} | "
            f"edge={float(decision.edge_probability) * 100:.2f}pp | ev=${float(decision.ev_per_usd):.4f}/$ | "
            f"model={'cheap' if getattr(decision, 'cheap_reversal_model_enabled', False) else 'fallback'} | "
            f"elapsed={float(elapsed):.2f}s"
        )
        return True

    async def _settle_fair_value_giro_probe_windows(
        self,
        probe_state: Dict,
        now_ts: float,
    ) -> None:
        import datetime

        windows = probe_state.setdefault("windows", {})
        attempts = probe_state.setdefault("settlement_attempts", {})
        for slug, entries in list(windows.items()):
            if not entries:
                windows.pop(slug, None)
                attempts.pop(slug, None)
                continue
            start_time = self._start_time_from_btc_slug(slug)
            if not start_time:
                windows.pop(slug, None)
                attempts.pop(slug, None)
                continue
            end_ts = start_time.timestamp() + 300.0
            if now_ts < end_ts:
                continue
            last_attempt = float(attempts.get(slug, 0.0) or 0.0)
            if now_ts - last_attempt < 5.0:
                continue
            attempts[slug] = now_ts

            yes_won = None
            opening_price = None
            closing_price = None
            detail = {}
            official_entry = next(
                (
                    entry for entry in entries
                    if entry.get("target_token_id") and entry.get("direction") in ("Yes", "No")
                ),
                None,
            )
            if official_entry:
                token_won, _settlement_price = await self._infer_settlement_result(
                    slug,
                    str(official_entry.get("target_token_id")),
                )
                if token_won is not None:
                    direction = official_entry.get("direction")
                    yes_won = bool(token_won) if direction == "Yes" else not bool(token_won)
                    detail = {"source": "official_outcome", "price_delta": ""}

            if yes_won is None:
                yes_won, opening_price, closing_price = await self._infer_chainlink_window_result(
                    slug,
                    "Yes",
                    wait_seconds=1.0,
                )
                detail = getattr(self, "_last_chainlink_settlement_detail", {}) or {}
                if yes_won is None:
                    continue

            if opening_price is not None and closing_price is not None:
                detail = {
                    **detail,
                    "price_delta": float(closing_price) - float(opening_price),
                }

            total_pnl = 0.0
            wins = 0
            losses = 0
            for entry in entries:
                direction = entry.get("direction")
                if direction not in ("Yes", "No"):
                    continue
                is_win = bool(yes_won) if direction == "Yes" else not bool(yes_won)
                shares = float(entry.get("entry_shares", 0.0) or 0.0)
                stake = float(entry.get("stake_usd", 0.0) or 0.0)
                fee = float(entry.get("entry_fee_usd", 0.0) or 0.0)
                payout = round(shares * (1.0 if is_win else 0.0), 4)
                pnl = round(payout - stake - fee, 4)
                total_pnl += pnl
                wins += 1 if is_win else 0
                losses += 0 if is_win else 1
                self._record_fair_value_giro_probe_outcome({
                    "event_ts_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    "slug": slug,
                    "entry_id": entry.get("entry_id", ""),
                    "tactic": "giro_probe",
                    "dry_run": self.dry_run,
                    "direction": direction,
                    "route": "paper_taker",
                    "result": "WIN" if is_win else "LOSS",
                    "stake_usd": round(stake, 4),
                    "entry_price": round(float(entry.get("entry_price", 0.0) or 0.0), 6),
                    "shares": round(shares, 4),
                    "fee_usd": round(fee, 4),
                    "payout_usd": payout,
                    "pnl_usd": pnl,
                    "fair_probability": entry.get("fair_probability", ""),
                    "cheap_model_probability": entry.get("cheap_model_probability", ""),
                    "cheap_raw_probability": entry.get("cheap_raw_probability", ""),
                    "cheap_model_enabled": entry.get("cheap_model_enabled", ""),
                    "cheap_model_version": entry.get("cheap_model_version", ""),
                    "edge_probability": entry.get("edge_probability", ""),
                    "ev_per_usd": entry.get("ev_per_usd", ""),
                    "trigger_reason": entry.get("trigger_reason", ""),
                    "delta_bps": entry.get("delta_bps", ""),
                    "opening_price": round(float(opening_price), 8) if opening_price else "",
                    "closing_price": round(float(closing_price), 8) if closing_price else "",
                    "price_delta": detail.get("price_delta", ""),
                    "settlement_source": detail.get("source", ""),
                })
            self.log(
                f"[GiroProbe] Window settled for {slug}. entries={len(entries)} | "
                f"W/L={wins}/{losses} | PnL=${total_pnl:.2f} | source={detail.get('source', '')}"
            )
            windows.pop(slug, None)
            attempts.pop(slug, None)

    def _fair_value_ladder_allows(self, state: Dict, decision, tactic: str) -> bool:
        if not decision or not decision.should_enter:
            return False
        entries = self._fair_value_entries(state)
        if not entries:
            return True
        best_ev = max(float(entry.get("ev_per_usd") or 0.0) for entry in entries)
        if tactic == "momentum":
            improvement = float(getattr(
                self,
                "fair_value_momentum_min_ev_improvement_per_entry",
                0.04,
            ))
        elif tactic == "late_continuation":
            improvement = float(getattr(
                self,
                "fair_value_late_continuation_min_ev_improvement_per_entry",
                0.03,
            ))
        else:
            improvement = float(getattr(
                self,
                "fair_value_min_ev_improvement_per_entry",
                0.03,
            ))
        return float(decision.ev_per_usd or 0.0) >= best_ev + improvement

    def _fair_value_select_decision(
        self,
        elapsed: float,
        core_decision,
        momentum_decision,
        late_continuation_decision=None,
        state: Optional[Dict] = None,
    ) -> Tuple[object, str]:
        choices = []
        state = state or {}
        core_count = self._fair_value_tactic_count(state, "core_edge")
        momentum_count = self._fair_value_tactic_count(state, "momentum")
        late_count = self._fair_value_tactic_count(state, "late_continuation")
        core_limit = int(getattr(self, "fair_value_core_max_entries_per_window", 2) or 0)
        momentum_limit = int(getattr(self, "fair_value_momentum_max_entries_per_window", 2) or 0)
        late_limit = int(getattr(self, "fair_value_late_continuation_max_entries_per_window", 2) or 0)
        core_start = float(getattr(self, "fair_value_entry_start_seconds", 0.0))
        core_end = float(getattr(self, "fair_value_entry_end_seconds", 240.0))
        if (
            bool(getattr(self, "fair_value_core_enabled", True))
            and
            core_start <= elapsed <= core_end
            and core_decision
            and core_decision.should_enter
            and (core_limit <= 0 or core_count < core_limit)
            and self._fair_value_ladder_allows(state, core_decision, "core_edge")
        ):
            choices.append(("core_edge", core_decision))

        if bool(getattr(self, "fair_value_momentum_enabled", True)):
            momentum_start = float(getattr(self, "fair_value_momentum_start_seconds", 240.0))
            momentum_end = float(getattr(self, "fair_value_momentum_end_seconds", 300.0))
            if (
                momentum_start <= elapsed <= momentum_end
                and momentum_decision
                and momentum_decision.should_enter
                and (momentum_limit <= 0 or momentum_count < momentum_limit)
                and self._fair_value_ladder_allows(state, momentum_decision, "momentum")
            ):
                choices.append(("momentum", momentum_decision))

        if bool(getattr(self, "fair_value_late_continuation_enabled", True)):
            late_start = float(getattr(self, "fair_value_late_continuation_start_seconds", 250.0))
            late_end = float(getattr(self, "fair_value_late_continuation_end_seconds", 300.0))
            if (
                late_start <= elapsed <= late_end
                and late_continuation_decision
                and late_continuation_decision.should_enter
                and (late_limit <= 0 or late_count < late_limit)
                and self._fair_value_ladder_allows(state, late_continuation_decision, "late_continuation")
            ):
                choices.append(("late_continuation", late_continuation_decision))

        if not choices:
            return FairValueDecision(should_enter=False), ""
        tactic, decision = max(choices, key=lambda item: float(item[1].ev_per_usd or 0.0))
        return decision, tactic

    def _fair_value_tactic_count(self, state: Dict, tactic: str) -> int:
        return sum(
            1
            for entry in self._fair_value_entries(state)
            if (entry.get("tactic") or "core_edge") == tactic
        )

    def _fair_value_is_transient_order_error(self, error_text: str) -> bool:
        text = str(error_text or "").lower()
        return any(
            marker in text
            for marker in (
                "request exception",
                "connecterror",
                "connection",
                "timed out",
                "timeout",
                "winerror 10054",
                "clob connectivity",
            )
        )

    def _fair_value_order_error_cooldown_seconds(self, error_text: str) -> float:
        text = str(error_text or "").lower()
        if self._fair_value_is_transient_order_error(error_text):
            return 1.0
        if "service not ready" in text or "order timed out" in text:
            return 1.0
        if any(marker in text for marker in ("invalid signature", "insufficient", "invalid amount")):
            return 30.0
        return 1.0

    def _fair_value_register_order_error(self, state: Dict, slug: str, error_text: str) -> None:
        errors = int(state.get("consecutive_order_errors", 0) or 0) + 1
        state["consecutive_order_errors"] = errors
        base = self._fair_value_order_error_cooldown_seconds(error_text)
        if base <= 2.0:
            cooldown = base
        else:
            cooldown = min(base * max(errors, 1), 180.0)
        until = time.time() + cooldown
        state["order_error_cooldown_until"] = until
        if not self._fair_value_is_transient_order_error(error_text):
            state["maker_attempted_slug"] = slug
        self.log(
            f"[FairValue] Order retry delay {cooldown:.0f}s after {errors} consecutive rejection(s). "
            f"Reason: {str(error_text)[:180]}"
        )

    def _fair_value_clear_order_errors(self, state: Dict) -> None:
        state["consecutive_order_errors"] = 0
        state["order_error_cooldown_until"] = 0.0

    def _fair_value_latest_entry_end(self) -> float:
        ends = [float(getattr(self, "fair_value_entry_end_seconds", 240.0))]
        if bool(getattr(self, "fair_value_momentum_enabled", True)):
            ends.append(float(getattr(self, "fair_value_momentum_end_seconds", 300.0)))
        if bool(getattr(self, "fair_value_late_continuation_enabled", True)):
            ends.append(float(getattr(self, "fair_value_late_continuation_end_seconds", 300.0)))
        return max(ends)

    def _fair_value_apply_entry(
        self,
        state: Dict,
        slug: str,
        direction: str,
        token_id: str,
        route: str,
        price: float,
        stake_usd: float,
        fair_probability: float,
        edge_probability: float,
        ev_per_usd: float,
        elapsed: float,
        order_text: str = "",
        filled_usd: Optional[float] = None,
        filled_shares: Optional[float] = None,
        tactic: str = "core_edge",
        entry_model_win_probability: float = 0.0,
        entry_model_min_probability: float = 0.0,
        break_even_price: float = 0.0,
        max_acceptable_price: float = 0.0,
        price_margin: float = 0.0,
        continuation_probability: float = 0.0,
        abs_delta_bps: float = 0.0,
    ):
        import datetime

        stake = round(float(filled_usd if filled_usd is not None else stake_usd), 4)
        p = max(float(price), 0.01)
        shares = round(
            float(filled_shares) if filled_shares is not None else stake / p,
            4,
        )
        fee = 0.0 if str(route).startswith("maker") else taker_fee_for_stake(
            stake,
            p,
            float(getattr(self, "martingale_taker_fee_rate", 0.07)),
        )
        shadow = self._fair_value_shadow_dynamic_stake(
            tactic=tactic or "core_edge",
            base_stake_usd=stake,
            edge_probability=edge_probability,
            ev_per_usd=ev_per_usd,
            entry_model_win_probability=entry_model_win_probability,
            price_margin=price_margin,
            continuation_probability=continuation_probability,
        )
        kalman_features = ((state.get("kalman") or {}).get("features") or {})
        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
        entries = self._fair_value_entries(state)
        entry_id = int(state.get("next_entry_id", 1) or 1)
        entry_number = len(entries) + 1
        entry = {
            "entry_id": entry_id,
            "entry_number_in_window": entry_number,
            "tactic": tactic or "core_edge",
            "slug": slug,
            "direction": direction,
            "target_token_id": token_id,
            "entry_price": round(p, 6),
            "entry_shares": shares,
            "stake_usd": stake,
            "entry_fee_usd": fee,
            "entry_route": route,
            "shadow_dynamic_stake_enabled": shadow["shadow_dynamic_stake_enabled"],
            "shadow_stake_usd": shadow["shadow_stake_usd"],
            "shadow_stake_multiplier": shadow["shadow_stake_multiplier"],
            "shadow_stake_reason": shadow["shadow_stake_reason"],
            "fair_probability": round(float(fair_probability), 6),
            "edge_probability": round(float(edge_probability), 6),
            "ev_per_usd": round(float(ev_per_usd), 6),
            "entry_model_win_probability": round(float(entry_model_win_probability or 0.0), 6),
            "entry_model_min_probability": round(float(entry_model_min_probability or 0.0), 6),
            "break_even_price": round(float(break_even_price or 0.0), 6),
            "max_acceptable_price": round(float(max_acceptable_price or 0.0), 6),
            "price_margin": round(float(price_margin or 0.0), 6),
            "continuation_probability": round(float(continuation_probability or 0.0), 6),
            "abs_delta_bps": round(float(abs_delta_bps or 0.0), 6),
            "kalman_delta_bps": round(float(kalman_features.get("kalman_delta_bps", 0.0) or 0.0), 6),
            "kalman_velocity_bps_per_min": round(float(kalman_features.get("kalman_velocity_bps_per_min", 0.0) or 0.0), 6),
            "kalman_projected_delta_bps": round(float(kalman_features.get("kalman_projected_delta_bps", 0.0) or 0.0), 6),
            "kalman_residual_bps": round(float(kalman_features.get("kalman_residual_bps", 0.0) or 0.0), 6),
            "kalman_abs_residual_bps": round(float(kalman_features.get("kalman_abs_residual_bps", 0.0) or 0.0), 6),
            "kalman_uncertainty_bps": round(float(kalman_features.get("kalman_uncertainty_bps", 0.0) or 0.0), 6),
            "kalman_trend_agreement": round(float(kalman_features.get("kalman_trend_agreement", 0.0) or 0.0), 6),
            "opened_at": now_iso,
            "elapsed_s": round(float(elapsed), 3),
        }
        entries.append(entry)
        state["entries"] = entries
        state["next_entry_id"] = entry_id + 1
        state.update({
            "active_slug": slug,
            "entry_done": True,
            "direction": direction,
            "target_token_id": token_id,
            "entry_price": round(p, 6),
            "entry_shares": shares,
            "stake_usd": stake,
            "entry_fee_usd": fee,
            "entry_route": route,
            "shadow_dynamic_stake_enabled": shadow["shadow_dynamic_stake_enabled"],
            "shadow_stake_usd": shadow["shadow_stake_usd"],
            "shadow_stake_multiplier": shadow["shadow_stake_multiplier"],
            "shadow_stake_reason": shadow["shadow_stake_reason"],
            "fair_probability": round(float(fair_probability), 6),
            "edge_probability": round(float(edge_probability), 6),
            "ev_per_usd": round(float(ev_per_usd), 6),
            "entry_model_win_probability": round(float(entry_model_win_probability or 0.0), 6),
            "entry_model_min_probability": round(float(entry_model_min_probability or 0.0), 6),
            "break_even_price": round(float(break_even_price or 0.0), 6),
            "max_acceptable_price": round(float(max_acceptable_price or 0.0), 6),
            "price_margin": round(float(price_margin or 0.0), 6),
            "continuation_probability": round(float(continuation_probability or 0.0), 6),
            "abs_delta_bps": round(float(abs_delta_bps or 0.0), 6),
            "kalman_delta_bps": round(float(kalman_features.get("kalman_delta_bps", 0.0) or 0.0), 6),
            "kalman_velocity_bps_per_min": round(float(kalman_features.get("kalman_velocity_bps_per_min", 0.0) or 0.0), 6),
            "kalman_projected_delta_bps": round(float(kalman_features.get("kalman_projected_delta_bps", 0.0) or 0.0), 6),
            "kalman_residual_bps": round(float(kalman_features.get("kalman_residual_bps", 0.0) or 0.0), 6),
            "kalman_abs_residual_bps": round(float(kalman_features.get("kalman_abs_residual_bps", 0.0) or 0.0), 6),
            "kalman_uncertainty_bps": round(float(kalman_features.get("kalman_uncertainty_bps", 0.0) or 0.0), 6),
            "kalman_trend_agreement": round(float(kalman_features.get("kalman_trend_agreement", 0.0) or 0.0), 6),
            "opened_at": now_iso,
            "tactic": tactic or "core_edge",
            "elapsed_s": round(float(elapsed), 3),
            "maker_candidate": None,
            "consecutive_order_errors": 0,
            "order_error_cooldown_until": 0.0,
        })
        self._record_fair_value_entry({
            "event_ts_utc": now_iso,
            "slug": slug,
            "entry_id": entry_id,
            "entry_number_in_window": entry_number,
            "tactic": tactic or "core_edge",
            "dry_run": self.dry_run,
            "live_trading_enabled": bool(getattr(self, "fair_value_live_trading_enabled", False)),
            "direction": direction,
            "route": route,
            "elapsed_s": round(float(elapsed), 3),
            "price": round(p, 6),
            "stake_usd": stake,
            "shares": shares,
            "fee_usd": fee,
            "shadow_dynamic_stake_enabled": shadow["shadow_dynamic_stake_enabled"],
            "shadow_stake_usd": shadow["shadow_stake_usd"],
            "shadow_stake_multiplier": shadow["shadow_stake_multiplier"],
            "shadow_stake_reason": shadow["shadow_stake_reason"],
            "fair_probability": round(float(fair_probability), 6),
            "edge_probability": round(float(edge_probability), 6),
            "ev_per_usd": round(float(ev_per_usd), 6),
            "entry_model_win_probability": round(float(entry_model_win_probability or 0.0), 6),
            "entry_model_min_probability": round(float(entry_model_min_probability or 0.0), 6),
            "break_even_price": round(float(break_even_price or 0.0), 6),
            "max_acceptable_price": round(float(max_acceptable_price or 0.0), 6),
            "price_margin": round(float(price_margin or 0.0), 6),
            "continuation_probability": round(float(continuation_probability or 0.0), 6),
            "abs_delta_bps": round(float(abs_delta_bps or 0.0), 6),
            "kalman_delta_bps": round(float(kalman_features.get("kalman_delta_bps", 0.0) or 0.0), 6),
            "kalman_velocity_bps_per_min": round(float(kalman_features.get("kalman_velocity_bps_per_min", 0.0) or 0.0), 6),
            "kalman_projected_delta_bps": round(float(kalman_features.get("kalman_projected_delta_bps", 0.0) or 0.0), 6),
            "kalman_residual_bps": round(float(kalman_features.get("kalman_residual_bps", 0.0) or 0.0), 6),
            "kalman_abs_residual_bps": round(float(kalman_features.get("kalman_abs_residual_bps", 0.0) or 0.0), 6),
            "kalman_uncertainty_bps": round(float(kalman_features.get("kalman_uncertainty_bps", 0.0) or 0.0), 6),
            "kalman_trend_agreement": round(float(kalman_features.get("kalman_trend_agreement", 0.0) or 0.0), 6),
            "order_text": order_text[:240],
        })
        max_price_text = (
            f"max_px={float(max_acceptable_price):.4f} | "
            if float(max_acceptable_price or 0.0) > 0.0
            else ""
        )
        self.log(
            f"[FairValue] ENTRY | slug={slug} | #{entry_number} | tactic={tactic or 'core_edge'} | "
            f"direction={direction} | route={route} | "
            f"usd=${stake:.2f} | px={p:.4f} | fair={float(fair_probability):.3f} | "
            f"edge={float(edge_probability) * 100:.2f}pp | ev=${float(ev_per_usd):.4f}/$ | "
            f"{max_price_text}"
            f"model_p={float(entry_model_win_probability or 0.0):.3f} | "
            f"shadow=${float(shadow['shadow_stake_usd']):.2f}x{float(shadow['shadow_stake_multiplier']):.2f} | "
            f"fee~=${fee:.2f} | elapsed={float(elapsed):.2f}s"
        )

    async def _fair_value_try_live_entry(
        self,
        session: ClientSession,
        slug: str,
        token_id: str,
        direction: str,
        route: str,
        price: float,
        stake_usd: float,
        fair_probability: float = 0.0,
        edge_probability: float = 0.0,
        ev_per_usd: float = 0.0,
        tactic: str = "core_edge",
        elapsed: float = 0.0,
    ) -> Tuple[bool, str, Optional[float], Optional[float], Optional[float], str]:
        if self.dry_run or not bool(getattr(self, "fair_value_live_trading_enabled", False)):
            return True, "paper", None, None, None, route
        if session is None:
            return False, "no MCP session", None, None, None, route
        if route == "maker":
            maker_shares = self._shares_for_limit_minimum(
                float(stake_usd) / max(float(price), 0.01),
                float(price),
            )
            min_limit_shares = float(getattr(self, "polymarket_min_limit_order_shares", 5.0) or 5.0)
            if maker_shares < min_limit_shares:
                return (
                    False,
                    f"maker size {maker_shares:.2f} below min {min_limit_shares:.2f}",
                    None,
                    None,
                    None,
                    route,
                )

            wait_seconds = max(float(getattr(self, "fair_value_maker_wait_seconds", 3.0)), 0.0)
            sample_seconds = min(
                max(float(getattr(self, "fair_value_sample_seconds", 1.0)), 0.1),
                0.5,
            )
            self._record_fair_value_maker_event({
                "event_ts_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "slug": slug,
                "dry_run": self.dry_run,
                "event": "created_live",
                "tactic": tactic or "core_edge",
                "direction": direction,
                "maker_price": round(float(price), 6),
                "best_ask": "",
                "elapsed_s": round(float(elapsed), 3),
                "stake_usd": round(float(stake_usd), 4),
                "fair_probability": round(float(fair_probability), 6),
                "edge_probability": round(float(edge_probability), 6),
                "ev_per_usd": round(float(ev_per_usd), 6),
            })
            self.log(
                f"[FairValue] Live maker order | slug={slug} | direction={direction} | "
                f"maker_px={float(price):.4f} | usd=${float(stake_usd):.2f} | "
                f"shares={maker_shares:.2f} | wait={wait_seconds:.1f}s"
            )
            order_res = await session.call_tool("place_order", arguments={
                "market_slug": slug,
                "side": "BUY",
                "size": maker_shares,
                "price": round(float(price), 4),
                "token_id": token_id,
                "order_type": "GTC",
                "post_only": True,
                "defer_exec": False,
            })
            order_text = order_res.content[0].text if order_res and order_res.content else ""
            order_ok, order_error, order_data = self._parse_order_response(order_text, allow_live=True)
            if not order_ok:
                return False, order_error[:240], None, None, None, route

            order_id = self._extract_order_id(order_data)
            if not order_id:
                return False, "maker order response had no order_id", None, None, None, route

            filled_usd, filled_shares, avg_entry_price, status = self._maker_fill_from_order_data(
                order_data,
                requested_shares=maker_shares,
                maker_price=float(price),
            )
            deadline = time.time() + wait_seconds
            while filled_shares < maker_shares * 0.98 and status not in ("filled", "matched"):
                now = time.time()
                if now >= deadline:
                    break
                await asyncio.sleep(min(sample_seconds, max(deadline - now, 0.0)))
                check_res = await session.call_tool("get_order", arguments={"order_id": order_id})
                check_text = check_res.content[0].text if check_res and check_res.content else ""
                check_ok, check_error, check_data = self._parse_order_response(check_text, allow_live=True)
                if not check_ok:
                    self.log(f"[FairValue] Live maker status check failed: {check_error[:180]}")
                    continue
                filled_usd, filled_shares, avg_entry_price, status = self._maker_fill_from_order_data(
                    check_data,
                    requested_shares=maker_shares,
                    maker_price=float(price),
                )

            if filled_shares < maker_shares * 0.98:
                cancel_res = await session.call_tool("cancel_order", arguments={"order_id": order_id})
                cancel_text = cancel_res.content[0].text if cancel_res and cancel_res.content else ""
                cancel_ok, cancel_error, cancel_data = self._parse_order_response(cancel_text, allow_live=True)
                not_canceled = (
                    cancel_data.get("not_canceled")
                    or cancel_data.get("notCanceled")
                    if isinstance(cancel_data, dict)
                    else None
                )
                if not_canceled:
                    cancel_ok = False
                    cancel_error = f"not_canceled={not_canceled}"
                if cancel_ok:
                    self.log(f"[FairValue] Cancelled unfilled maker remainder for order {order_id[:10]}...")
                else:
                    self.log(f"[FairValue] Maker cancel warning: {cancel_error[:180]}")

            if filled_shares <= 0:
                self._record_fair_value_maker_event({
                    "event_ts_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    "slug": slug,
                    "dry_run": self.dry_run,
                    "event": "expired_live",
                    "tactic": tactic or "core_edge",
                    "direction": direction,
                    "maker_price": round(float(price), 6),
                    "best_ask": "",
                    "elapsed_s": round(float(elapsed), 3),
                    "stake_usd": round(float(stake_usd), 4),
                    "fair_probability": round(float(fair_probability), 6),
                    "edge_probability": round(float(edge_probability), 6),
                    "ev_per_usd": round(float(ev_per_usd), 6),
                })
                return False, "maker order not filled", None, None, None, route

            filled_usd = round(
                filled_usd if filled_usd > 0 else filled_shares * float(price),
                4,
            )
            complete = filled_shares >= maker_shares * 0.98
            route_label = "maker_live" if complete else "maker_partial"
            self._record_fair_value_maker_event({
                "event_ts_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "slug": slug,
                "dry_run": self.dry_run,
                "event": "filled_live" if complete else "partial_live",
                "tactic": tactic or "core_edge",
                "direction": direction,
                "maker_price": round(float(price), 6),
                "best_ask": "",
                "elapsed_s": round(float(elapsed), 3),
                "stake_usd": round(float(filled_usd), 4),
                "fair_probability": round(float(fair_probability), 6),
                "edge_probability": round(float(edge_probability), 6),
                "ev_per_usd": round(float(ev_per_usd), 6),
            })
            self.log(
                f"[FairValue] Maker fill | route={route_label} | usd=${filled_usd:.2f} | "
                f"px={avg_entry_price:.4f} | shares={filled_shares:.4f} | status={status or 'unknown'}"
            )
            return True, order_text[:240], filled_usd, filled_shares, avg_entry_price, route_label

        order_type = str(getattr(self, "fair_value_order_type", "FOK") or "FOK").upper()
        if order_type not in ("FAK", "FOK"):
            order_type = "FOK"
        order_res = await session.call_tool("place_market_order", arguments={
            "market_slug": slug,
            "side": "BUY",
            "amount": round(max(float(stake_usd), self.polymarket_min_market_buy_usd), 2),
            "token_id": token_id,
            "order_type": order_type,
            "defer_exec": False,
        })
        order_text = order_res.content[0].text if order_res and order_res.content else ""
        order_ok, order_error, order_data = self._parse_order_response(order_text, allow_live=False)
        if not order_ok:
            return False, order_error[:240], None, None, None, route
        filled_usd, filled_shares, avg_entry_price = self._order_fill_from_response(
            order_data,
            fallback_usd=stake_usd,
            fallback_price=price,
        )
        return True, order_text[:240], filled_usd, filled_shares, avg_entry_price, route

    async def _fair_value_handle_maker_candidate(
        self,
        session: ClientSession,
        state: Dict,
        slug: str,
        elapsed: float,
        context: Dict,
    ) -> bool:
        candidate = state.get("maker_candidate")
        if not isinstance(candidate, dict) or candidate.get("slug") != slug:
            return False
        if not self.dry_run and bool(getattr(self, "fair_value_live_trading_enabled", False)):
            self.log("[FairValue] Clearing stale paper maker candidate before live trading.")
            state["maker_candidate"] = None
            self.save_state()
            return False

        side = candidate.get("direction")
        metrics = context["yes_metrics"] if side == "Yes" else context["no_metrics"]
        best_ask = float(metrics.get("best_ask") or 0.0)
        maker_price = float(candidate.get("maker_price") or 0.0)
        now_ts = time.time()
        if best_ask > 0 and best_ask <= maker_price:
            token_id = context["token_ids"][0 if side == "Yes" else 1]
            route = (
                "maker_simulated"
                if self.dry_run or not bool(getattr(self, "fair_value_live_trading_enabled", False))
                else "maker_live"
            )
            self._record_fair_value_maker_event({
                "event_ts_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "slug": slug,
                "dry_run": self.dry_run,
                "event": "filled_simulated" if route == "maker_simulated" else "filled_live",
                "tactic": candidate.get("tactic", "core_edge") or "core_edge",
                "direction": side,
                "maker_price": round(maker_price, 6),
                "best_ask": round(best_ask, 6),
                "elapsed_s": round(float(elapsed), 3),
                "stake_usd": round(float(candidate.get("stake_usd") or 0.0), 4),
                "fair_probability": round(float(candidate.get("fair_probability") or 0.0), 6),
                "edge_probability": round(float(candidate.get("edge_probability") or 0.0), 6),
                "ev_per_usd": round(float(candidate.get("ev_per_usd") or 0.0), 6),
                "entry_model_win_probability": round(float(candidate.get("entry_model_win_probability") or 0.0), 6),
                "entry_model_min_probability": round(float(candidate.get("entry_model_min_probability") or 0.0), 6),
                "break_even_price": round(float(candidate.get("break_even_price") or 0.0), 6),
                "max_acceptable_price": round(float(candidate.get("max_acceptable_price") or 0.0), 6),
                "price_margin": round(float(candidate.get("price_margin") or 0.0), 6),
                "continuation_probability": round(float(candidate.get("continuation_probability") or 0.0), 6),
                "abs_delta_bps": round(float(candidate.get("abs_delta_bps") or 0.0), 6),
            })
            self._fair_value_apply_entry(
                state=state,
                slug=slug,
                direction=side,
                token_id=token_id,
                route=route,
                price=maker_price,
                stake_usd=float(candidate.get("stake_usd") or getattr(self, "fair_value_stake_usd", 1.0)),
                fair_probability=float(candidate.get("fair_probability") or 0.0),
                edge_probability=float(candidate.get("edge_probability") or 0.0),
                ev_per_usd=float(candidate.get("ev_per_usd") or 0.0),
                elapsed=elapsed,
                tactic=candidate.get("tactic", "core_edge") or "core_edge",
                entry_model_win_probability=float(candidate.get("entry_model_win_probability") or 0.0),
                entry_model_min_probability=float(candidate.get("entry_model_min_probability") or 0.0),
                break_even_price=float(candidate.get("break_even_price") or 0.0),
                max_acceptable_price=float(candidate.get("max_acceptable_price") or 0.0),
                price_margin=float(candidate.get("price_margin") or 0.0),
                continuation_probability=float(candidate.get("continuation_probability") or 0.0),
                abs_delta_bps=float(candidate.get("abs_delta_bps") or 0.0),
            )
            return True

        if now_ts >= float(candidate.get("deadline_ts") or 0.0):
            self._record_fair_value_maker_event({
                "event_ts_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "slug": slug,
                "dry_run": self.dry_run,
                "event": "expired",
                "tactic": candidate.get("tactic", "core_edge") or "core_edge",
                "direction": side,
                "maker_price": round(maker_price, 6),
                "best_ask": round(best_ask, 6),
                "elapsed_s": round(float(elapsed), 3),
                "stake_usd": round(float(candidate.get("stake_usd") or 0.0), 4),
                "fair_probability": round(float(candidate.get("fair_probability") or 0.0), 6),
                "edge_probability": round(float(candidate.get("edge_probability") or 0.0), 6),
                "ev_per_usd": round(float(candidate.get("ev_per_usd") or 0.0), 6),
                "entry_model_win_probability": round(float(candidate.get("entry_model_win_probability") or 0.0), 6),
                "entry_model_min_probability": round(float(candidate.get("entry_model_min_probability") or 0.0), 6),
                "break_even_price": round(float(candidate.get("break_even_price") or 0.0), 6),
                "max_acceptable_price": round(float(candidate.get("max_acceptable_price") or 0.0), 6),
                "price_margin": round(float(candidate.get("price_margin") or 0.0), 6),
                "continuation_probability": round(float(candidate.get("continuation_probability") or 0.0), 6),
                "abs_delta_bps": round(float(candidate.get("abs_delta_bps") or 0.0), 6),
            })
            self.log(
                f"[FairValue] Maker candidate expired | slug={slug} | direction={side} | "
                f"maker_px={maker_price:.4f} | best_ask={best_ask:.4f}. Taker can still enter if edge remains."
            )
            state["maker_candidate"] = None
            state["maker_attempted_slug"] = slug
            self.save_state()
        else:
            return True
        return False

    async def _settle_active_fair_value_trade(self, state: Dict, slug: str, now_ts: float) -> bool:
        import datetime

        entries = list(self._fair_value_entries(state))
        if not entries:
            return False

        start_time = self._start_time_from_btc_slug(slug)
        if not start_time:
            return False

        yes_won = None
        opening_price = None
        closing_price = None
        detail = {}

        official_entry = next(
            (
                entry for entry in entries
                if entry.get("target_token_id") and entry.get("direction") in ("Yes", "No")
            ),
            None,
        )
        if official_entry:
            token_won, _settlement_price = await self._infer_settlement_result(
                slug,
                str(official_entry.get("target_token_id")),
            )
            if token_won is not None:
                entry_direction = official_entry.get("direction")
                yes_won = bool(token_won) if entry_direction == "Yes" else not bool(token_won)
                detail = {
                    "source": "official_outcome",
                    "price_delta": "",
                }

        if yes_won is None:
            yes_won, opening_price, closing_price = await self._infer_chainlink_window_result(
                slug,
                "Yes",
                wait_seconds=2.5,
            )
            detail = getattr(self, "_last_chainlink_settlement_detail", {}) or {}
            if yes_won is None:
                last_log = float(state.get("last_settlement_wait_log_at", 0.0) or 0.0)
                if time.time() - last_log >= 30.0:
                    self.log(
                        f"[FairValue] Waiting for official/Chainlink settlement for {slug}. "
                        "Holding entries unresolved."
                    )
                    state["last_settlement_wait_log_at"] = time.time()
                    self.save_state()
                return False

        if opening_price is not None and closing_price is not None:
            delta = float(closing_price) - float(opening_price)
            detail = {
                **detail,
                "price_delta": delta,
            }

        total_pnl = 0.0
        wins = 0
        losses = 0
        decision_latency = round(max(time.time() - now_ts, 0.0), 4)
        for entry in entries:
            direction = entry.get("direction")
            if direction not in ("Yes", "No"):
                continue
            is_win = bool(yes_won) if direction == "Yes" else not bool(yes_won)
            shares = float(entry.get("entry_shares", 0.0) or 0.0)
            stake = float(entry.get("stake_usd", 0.0) or 0.0)
            fee = float(entry.get("entry_fee_usd", 0.0) or 0.0)
            payout = round(shares * (1.0 if is_win else 0.0), 4)
            pnl = round(payout - stake - fee, 4)
            entry_price = max(float(entry.get("entry_price", 0.0) or 0.0), 0.01)
            shadow_stake = float(entry.get("shadow_stake_usd", stake) or stake)
            shadow_shares = round(shadow_stake / entry_price, 4)
            shadow_fee = 0.0 if str(entry.get("entry_route", "")).startswith("maker") else taker_fee_for_stake(
                shadow_stake,
                entry_price,
                float(getattr(self, "martingale_taker_fee_rate", 0.07)),
            )
            shadow_payout = round(shadow_shares * (1.0 if is_win else 0.0), 4)
            shadow_pnl = round(shadow_payout - shadow_stake - shadow_fee, 4)
            total_pnl += pnl
            if is_win:
                wins += 1
            else:
                losses += 1
            self._record_fair_value_outcome({
                "event_ts_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "slug": slug,
                "entry_id": entry.get("entry_id", ""),
                "entry_number_in_window": entry.get("entry_number_in_window", ""),
                "tactic": entry.get("tactic", "core_edge"),
                "dry_run": self.dry_run,
                "direction": direction,
                "route": entry.get("entry_route", ""),
                "result": "WIN" if is_win else "LOSS",
                "stake_usd": round(stake, 4),
                "entry_price": round(entry_price, 6),
                "shares": round(shares, 4),
                "fee_usd": round(fee, 4),
                "payout_usd": payout,
                "pnl_usd": pnl,
                "shadow_dynamic_stake_enabled": entry.get("shadow_dynamic_stake_enabled", False),
                "shadow_stake_usd": round(shadow_stake, 4),
                "shadow_stake_multiplier": entry.get("shadow_stake_multiplier", 1.0),
                "shadow_stake_reason": entry.get("shadow_stake_reason", ""),
                "shadow_shares": shadow_shares,
                "shadow_fee_usd": round(shadow_fee, 4),
                "shadow_payout_usd": shadow_payout,
                "shadow_pnl_usd": shadow_pnl,
                "fair_probability": entry.get("fair_probability", ""),
                "edge_probability": entry.get("edge_probability", ""),
                "ev_per_usd": entry.get("ev_per_usd", ""),
                "entry_model_win_probability": entry.get("entry_model_win_probability", ""),
                "entry_model_min_probability": entry.get("entry_model_min_probability", ""),
                "break_even_price": entry.get("break_even_price", ""),
                "max_acceptable_price": entry.get("max_acceptable_price", ""),
                "price_margin": entry.get("price_margin", ""),
                "continuation_probability": entry.get("continuation_probability", ""),
                "abs_delta_bps": entry.get("abs_delta_bps", ""),
                "opening_price": round(float(opening_price), 8) if opening_price else "",
                "closing_price": round(float(closing_price), 8) if closing_price else "",
                "price_delta": detail.get("price_delta", ""),
                "settlement_source": detail.get("source", ""),
                "decision_latency": decision_latency,
            })
        self.log(
            f"[FairValue] Window settled for {slug}. entries={len(entries)} | "
            f"W/L={wins}/{losses} | PnL=${total_pnl:.2f} | source={detail.get('source', '')}"
        )
        fresh = self._new_fair_value_state()
        state.clear()
        state.update(fresh)
        self.save_state()
        return True

    def _release_unresolved_fair_value_state(self, state: Dict, slug: str, reason: str) -> None:
        import datetime

        entries = list(self._fair_value_entries(state))
        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
        for entry in entries:
            self._record_fair_value_outcome({
                "event_ts_utc": now_iso,
                "slug": slug,
                "entry_id": entry.get("entry_id", ""),
                "entry_number_in_window": entry.get("entry_number_in_window", ""),
                "tactic": entry.get("tactic", "core_edge"),
                "dry_run": self.dry_run,
                "direction": entry.get("direction", ""),
                "route": entry.get("entry_route", ""),
                "result": "UNRESOLVED",
                "stake_usd": round(float(entry.get("stake_usd", 0.0) or 0.0), 4),
                "entry_price": round(float(entry.get("entry_price", 0.0) or 0.0), 6),
                "shares": round(float(entry.get("entry_shares", 0.0) or 0.0), 4),
                "fee_usd": round(float(entry.get("entry_fee_usd", 0.0) or 0.0), 4),
                "payout_usd": "",
                "pnl_usd": "",
                "shadow_dynamic_stake_enabled": entry.get("shadow_dynamic_stake_enabled", False),
                "shadow_stake_usd": entry.get("shadow_stake_usd", ""),
                "shadow_stake_multiplier": entry.get("shadow_stake_multiplier", ""),
                "shadow_stake_reason": entry.get("shadow_stake_reason", ""),
                "shadow_shares": "",
                "shadow_fee_usd": "",
                "shadow_payout_usd": "",
                "shadow_pnl_usd": "",
                "fair_probability": entry.get("fair_probability", ""),
                "edge_probability": entry.get("edge_probability", ""),
                "ev_per_usd": entry.get("ev_per_usd", ""),
                "entry_model_win_probability": entry.get("entry_model_win_probability", ""),
                "entry_model_min_probability": entry.get("entry_model_min_probability", ""),
                "break_even_price": entry.get("break_even_price", ""),
                "max_acceptable_price": entry.get("max_acceptable_price", ""),
                "price_margin": entry.get("price_margin", ""),
                "continuation_probability": entry.get("continuation_probability", ""),
                "abs_delta_bps": entry.get("abs_delta_bps", ""),
                "opening_price": "",
                "closing_price": "",
                "price_delta": "",
                "settlement_source": reason,
                "decision_latency": "",
            })

        self.log(
            f"[FairValue] Released unresolved window {slug}. entries={len(entries)} | "
            f"reason={reason}. Continuing with fresh windows."
        )
        fresh = self._new_fair_value_state()
        state.clear()
        state.update(fresh)
        self.save_state()

    async def _reconcile_stale_fair_value_state(
        self,
        state: Dict,
        current_slug: str,
        now_ts: float,
    ) -> bool:
        active_slug = state.get("active_slug")
        if not active_slug or active_slug == current_slug or not self._fair_value_entries(state):
            return True

        previous_start = self._start_time_from_btc_slug(active_slug)
        if previous_start is None:
            self._release_unresolved_fair_value_state(
                state,
                str(active_slug),
                "invalid_slug",
            )
            self._last_unresolved_fair_value_slug = None
            return True

        previous_end_ts = previous_start.timestamp() + 300
        if now_ts < previous_end_ts:
            return False

        stale_age = max(float(now_ts) - float(previous_end_ts), 0.0)
        retry_seconds = 5.0
        last_attempt = float(state.get("last_settlement_attempt_at", 0.0) or 0.0)
        if now_ts - last_attempt >= retry_seconds:
            state["last_settlement_attempt_at"] = now_ts
            self.save_state()
            settled = await self._settle_active_fair_value_trade(state, str(active_slug), now_ts)
            if settled:
                self._last_unresolved_fair_value_slug = None
                return True

        release_after = float(getattr(self, "fair_value_unresolved_release_seconds", 900.0))
        if stale_age >= release_after:
            self._release_unresolved_fair_value_state(
                state,
                str(active_slug),
                f"unresolved_after_{int(release_after)}s",
            )
            self._last_unresolved_fair_value_slug = None
            return True

        last_log = float(getattr(self, "_last_unresolved_fair_value_log_ts", 0.0) or 0.0)
        if (
            getattr(self, "_last_unresolved_fair_value_slug", None) != active_slug
            or now_ts - last_log >= 60.0
        ):
            self.log(
                f"[FairValue] Previous window {active_slug} is waiting for settlement "
                f"(age={stale_age:.0f}s). Holding new entries briefly."
            )
            self._last_unresolved_fair_value_slug = active_slug
            self._last_unresolved_fair_value_log_ts = now_ts
        return False

    async def run_fair_value_strategy(self, session: ClientSession):
        import datetime

        self._ensure_chainlink_price_feed()
        now_ts = time.time()
        window_start = int(now_ts // 300) * 300
        slug = f"btc-updown-5m-{window_start}"
        if "fair_value_state" not in self.app_state:
            self.app_state["fair_value_state"] = self._new_fair_value_state()
        else:
            self.app_state["fair_value_state"] = self._normalize_fair_value_state(
                self.app_state["fair_value_state"]
            )
        state = self.app_state["fair_value_state"]
        if "fair_value_giro_probe_state" not in self.app_state:
            self.app_state["fair_value_giro_probe_state"] = self._new_fair_value_giro_probe_state()
        else:
            self.app_state["fair_value_giro_probe_state"] = self._normalize_fair_value_giro_probe_state(
                self.app_state["fair_value_giro_probe_state"]
            )
        giro_probe_state = self.app_state["fair_value_giro_probe_state"]
        await self._settle_fair_value_giro_probe_windows(giro_probe_state, now_ts)

        if not await self._reconcile_stale_fair_value_state(state, slug, now_ts):
            return
        now_ts = time.time()
        window_start = int(now_ts // 300) * 300
        slug = f"btc-updown-5m-{window_start}"

        market_data = await self.get_market_by_slug(slug, use_cache=True, cache_ttl=300)
        if market_data:
            market_data["slug"] = slug
        if not market_data:
            if getattr(self, "_last_logged_no_market_fair_value", None) != window_start:
                self.log("[FairValue] No active 5m BTC market found right now. Waiting...")
                self._last_logged_no_market_fair_value = window_start
            return

        if state.get("active_slug") != slug:
            state.update(self._new_fair_value_state())
            state["active_slug"] = slug
            self.save_state()

        start_time = market_data["start_time"]
        elapsed = now_ts - start_time.timestamp()
        context = await self._fair_value_market_context(slug, market_data)
        if not context:
            return

        opening_price, latest_price, latest_offset, price_source = await self._fair_value_price_context(
            start_time.timestamp(),
            now_ts,
        )
        state["kalman"] = kalman_step(
            state.get("kalman"),
            latest_price or 0.0,
            now_ts,
            opening_price or 0.0,
            float(window_start),
        )
        fair_data = estimate_fair_yes(
            opening_price=opening_price,
            latest_price=latest_price,
            elapsed_seconds=elapsed,
            sensitivity_bps=float(getattr(self, "fair_value_model_sensitivity_bps", 28.0)),
        )
        fair_data = blend_fair_yes_with_market(
            fair_data,
            context["yes_metrics"],
            context["no_metrics"],
        )
        fair_data.update((state.get("kalman") or {}).get("features") or {})
        fair_data["price_source"] = price_source
        core_decision = self._fair_value_decision(
            fair_yes=float(fair_data.get("fair_yes") or 0.5),
            yes_metrics=context["yes_metrics"],
            no_metrics=context["no_metrics"],
            state=state,
            fair_data=fair_data,
        )
        core_decision = self._fair_value_apply_entry_model_guard(
            core_decision,
            "core_edge",
            elapsed,
            state,
        )
        momentum_decision = self._fair_value_momentum_decision(
            fair_yes=float(fair_data.get("fair_yes") or 0.5),
            fair_data=fair_data,
            yes_metrics=context["yes_metrics"],
            no_metrics=context["no_metrics"],
        )
        momentum_decision = self._fair_value_apply_entry_model_guard(
            momentum_decision,
            "momentum",
            elapsed,
            state,
        )
        late_continuation_decision = self._fair_value_late_continuation_decision(
            fair_data=fair_data,
            yes_metrics=context["yes_metrics"],
            no_metrics=context["no_metrics"],
            state=state,
        )
        late_continuation_decision = self._fair_value_apply_entry_model_guard(
            late_continuation_decision,
            "late_continuation",
            elapsed,
            state,
        )
        giro_probe_decision = self._fair_value_giro_probe_decision(
            fair_data=fair_data,
            yes_metrics=context["yes_metrics"],
            no_metrics=context["no_metrics"],
            elapsed=elapsed,
        )
        decision, tactic = self._fair_value_select_decision(
            elapsed=elapsed,
            core_decision=core_decision,
            momentum_decision=momentum_decision,
            late_continuation_decision=late_continuation_decision,
            state=state,
        )

        if self._fair_value_signal_interval_elapsed(state, now_ts):
            self._record_fair_value_signal(
                self._fair_value_orderbook_row(
                    slug=slug,
                    elapsed=elapsed,
                    token_ids=context["token_ids"],
                    yes_book=context["yes_book"],
                    no_book=context["no_book"],
                    fair_data=fair_data,
                    decision=decision,
                    opening_price=opening_price,
                    latest_price=latest_price,
                    latest_offset=latest_offset,
                    state=state,
                    tactic=tactic,
                )
            )
            state["last_signal_sample_at"] = now_ts

        if giro_probe_decision and giro_probe_decision.should_enter:
            self._record_fair_value_giro_probe_signal(
                probe_state=giro_probe_state,
                slug=slug,
                decision=giro_probe_decision,
                fair_data=fair_data,
                token_ids=context["token_ids"],
                elapsed=elapsed,
            )

        if await self._fair_value_handle_maker_candidate(session, state, slug, elapsed, context):
            self.save_state()
            return

        cooldown_until = float(state.get("order_error_cooldown_until", 0.0) or 0.0)
        if (
            not self.dry_run
            and bool(getattr(self, "fair_value_live_trading_enabled", False))
            and cooldown_until > now_ts
        ):
            last_log = float(state.get("last_order_cooldown_log_at", 0.0) or 0.0)
            if now_ts - last_log >= 15.0:
                self.log(
                    f"[FairValue] Live order cooldown active for {cooldown_until - now_ts:.0f}s. "
                    "Skipping entries while CLOB/order execution recovers."
                )
                state["last_order_cooldown_log_at"] = now_ts
                self.save_state()
            return

        if elapsed > self._fair_value_latest_entry_end():
            if not self._fair_value_entries(state) and state.get("skip_logged_slug") != slug:
                self.log(
                    f"[FairValue] No qualifying entry for {slug} before {self._fair_value_latest_entry_end():.0f}s. "
                    "Window recorded and skipped."
                )
                state["skip_logged_slug"] = slug
                self.save_state()
            return

        if not decision.should_enter:
            return

        token_index = 0 if decision.side == "Yes" else 1
        token_id = context["token_ids"][token_index]
        stake = self._fair_value_stake(decision.price)
        if tactic == "momentum":
            stake = round(
                stake * float(getattr(self, "fair_value_momentum_stake_multiplier", 1.0)),
                2,
            )
        elif tactic == "late_continuation":
            stake = round(
                stake * float(getattr(self, "fair_value_late_continuation_stake_multiplier", 1.0)),
                2,
            )
        if decision.route == "maker":
            if self.dry_run or not bool(getattr(self, "fair_value_live_trading_enabled", False)):
                wait_seconds = max(float(getattr(self, "fair_value_maker_wait_seconds", 3.0)), 0.0)
                deadline = min(now_ts + wait_seconds, start_time.timestamp() + self._fair_value_latest_entry_end())
                if deadline > now_ts:
                    state["maker_candidate"] = {
                        "slug": slug,
                        "direction": decision.side,
                        "token_id": token_id,
                        "maker_price": decision.maker_price or decision.price,
                        "created_ts": now_ts,
                        "deadline_ts": deadline,
                        "stake_usd": stake,
                        "fair_probability": decision.fair_probability,
                        "edge_probability": decision.edge_probability,
                        "ev_per_usd": decision.ev_per_usd,
                        "entry_model_win_probability": decision.entry_model_win_probability,
                        "entry_model_min_probability": decision.entry_model_min_probability,
                        "break_even_price": decision.break_even_price,
                        "max_acceptable_price": decision.max_acceptable_price,
                        "price_margin": decision.price_margin,
                        "continuation_probability": decision.continuation_probability,
                        "abs_delta_bps": decision.abs_delta_bps,
                        "tactic": tactic or "core_edge",
                    }
                    state["maker_attempted_slug"] = slug
                    self._record_fair_value_maker_event({
                        "event_ts_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                        "slug": slug,
                        "dry_run": self.dry_run,
                        "event": "created",
                        "tactic": tactic or "core_edge",
                        "direction": decision.side,
                        "maker_price": round(float(decision.maker_price or decision.price), 6),
                        "best_ask": round(float(decision.price), 6),
                        "elapsed_s": round(float(elapsed), 3),
                        "stake_usd": round(float(stake), 4),
                        "fair_probability": round(float(decision.fair_probability), 6),
                        "edge_probability": round(float(decision.edge_probability), 6),
                        "ev_per_usd": round(float(decision.ev_per_usd), 6),
                        "entry_model_win_probability": round(float(decision.entry_model_win_probability), 6),
                        "entry_model_min_probability": round(float(decision.entry_model_min_probability), 6),
                        "break_even_price": round(float(decision.break_even_price), 6),
                        "max_acceptable_price": round(float(decision.max_acceptable_price), 6),
                        "price_margin": round(float(decision.price_margin), 6),
                        "continuation_probability": round(float(decision.continuation_probability), 6),
                        "abs_delta_bps": round(float(decision.abs_delta_bps), 6),
                    })
                    self.log(
                        f"[FairValue] Maker candidate | slug={slug} | direction={decision.side} | "
                        f"maker_px={decision.price:.4f} | fair={decision.fair_probability:.3f} | "
                        f"edge={decision.edge_probability * 100:.2f}pp | wait={deadline - now_ts:.1f}s"
                    )
                    self.save_state()
                    return
                return

        ok, order_text, filled_usd, filled_shares, avg_price, route = await self._fair_value_try_live_entry(
            session=session,
            slug=slug,
            token_id=token_id,
            direction=decision.side,
            route=decision.route,
            price=decision.price,
            stake_usd=stake,
            fair_probability=decision.fair_probability,
            edge_probability=decision.edge_probability,
            ev_per_usd=decision.ev_per_usd,
            tactic=tactic or "core_edge",
            elapsed=elapsed,
        )
        if not ok:
            self.log(f"[FairValue] Entry rejected: {order_text}")
            if not self.dry_run and bool(getattr(self, "fair_value_live_trading_enabled", False)):
                self._fair_value_register_order_error(state, slug, order_text)
                self.save_state()
            return
        if not self.dry_run and not bool(getattr(self, "fair_value_live_trading_enabled", False)):
            route = "paper_live_disabled"
        self._fair_value_apply_entry(
            state=state,
            slug=slug,
            direction=decision.side,
            token_id=token_id,
            route=route,
            price=float(avg_price if avg_price is not None else decision.price),
            stake_usd=stake,
            fair_probability=decision.fair_probability,
            edge_probability=decision.edge_probability,
            ev_per_usd=decision.ev_per_usd,
            elapsed=elapsed,
            order_text=order_text,
            filled_usd=filled_usd,
            filled_shares=filled_shares,
            tactic=tactic or "core_edge",
            entry_model_win_probability=decision.entry_model_win_probability,
            entry_model_min_probability=decision.entry_model_min_probability,
            break_even_price=decision.break_even_price,
            max_acceptable_price=decision.max_acceptable_price,
            price_margin=decision.price_margin,
            continuation_probability=decision.continuation_probability,
            abs_delta_bps=decision.abs_delta_bps,
        )
        self.save_state()

    def _martingale_in_recovery(self, state: Dict) -> bool:
        return int(state.get("streak", 0) or 0) > 0 or float(state.get("loss_bank", 0.0) or 0.0) > 0

    def _entry_price_bounds(self, state: Dict, elapsed: float) -> Tuple[float, float]:
        if self._martingale_in_recovery(state):
            if elapsed >= self.martingale_late_recovery_start_seconds:
                return (
                    self.martingale_late_recovery_min_entry_price,
                    self.martingale_late_recovery_max_entry_price,
                )
            return self.martingale_recovery_min_entry_price, self.martingale_recovery_max_entry_price
        if elapsed >= self.martingale_late_entry_start_seconds:
            return self.martingale_late_min_entry_price, self.martingale_late_max_entry_price
        if elapsed >= self.martingale_mid_entry_start_seconds:
            return self.martingale_mid_min_entry_price, self.martingale_mid_max_entry_price
        return self.martingale_min_entry_price, self.martingale_max_entry_price

    def _entry_price_allowed(self, price: float, min_price: float = None, max_price: float = None) -> bool:
        lower = self.martingale_min_entry_price if min_price is None else float(min_price)
        upper = self.martingale_max_entry_price if max_price is None else float(max_price)
        return lower <= float(price) <= upper

    def _start_time_from_btc_slug(self, slug: str):
        import datetime

        try:
            window_start = int(str(slug).rsplit("-", 1)[1])
            return datetime.datetime.fromtimestamp(window_start, datetime.timezone.utc)
        except Exception:
            return None

    def _reset_martingale_cycle_after_gap(self, state: Dict, slug: str):
        previous_loss_bank = float(state.get("loss_bank", 0.0) or 0.0)
        previous_streak = int(state.get("streak", 0) or 0)
        state["streak"] = 0
        state["direction"] = None
        state["loss_bank"] = 0.0
        state["current_amount"] = self.martingale_initial_amount
        state["entry_fee_usd"] = 0.0
        state["entry_route"] = ""
        state["maker_price"] = None
        state["maker_waited_seconds"] = 0.0
        state["maker_touch_signal"] = False
        state["cycle_start_balance"] = None
        state["entry_retry_after"] = 0.0
        state["entry_attempts"] = 0
        state["skip_logged"] = True
        self.log(
            f"[Martingale] Coverage gap on {slug}. Resetting probability cycle "
            f"(streak was {previous_streak}, loss_bank was ${previous_loss_bank:.2f})."
        )

    def _abandon_martingale_recovery(self, state: Dict, reason: str):
        previous_loss_bank = float(state.get("loss_bank", 0.0) or 0.0)
        previous_streak = int(state.get("streak", 0) or 0)
        state["streak"] = 0
        state["direction"] = None
        state["loss_bank"] = 0.0
        state["current_amount"] = self.martingale_initial_amount
        state["entry_fee_usd"] = 0.0
        state["entry_route"] = ""
        state["maker_price"] = None
        state["maker_waited_seconds"] = 0.0
        state["maker_touch_signal"] = False
        state["cycle_start_balance"] = None
        state["entry_retry_after"] = 0.0
        state["entry_attempts"] = 0
        state["skip_logged"] = True
        self.log(
            f"[Martingale] Recovery abandoned: {reason} "
            f"(streak was {previous_streak}, loss_bank was ${previous_loss_bank:.2f})."
        )

    def _apply_martingale_result(self, state: Dict, slug: str, is_win: bool, shares: float, entry_price: float):
        entry_notional = float(state.get("current_amount", 0.0) or (float(shares) * float(entry_price)))
        entry_fee = self._entry_fee_from_state(state, shares, entry_price)
        previous_loss_bank = float(state.get("loss_bank", 0.0) or 0.0)
        if is_win:
            net_profit = self._martingale_win_net_profit(shares, entry_notional, entry_fee)
            recovery_target = previous_loss_bank + max(float(self.martingale_initial_amount), 0.01)
            if net_profit + 0.01 >= recovery_target:
                state["streak"] = 0
                state["direction"] = None
                state["loss_bank"] = 0.0
                state["current_amount"] = self.martingale_initial_amount
                state["entry_fee_usd"] = 0.0
                state["entry_route"] = ""
                state["maker_price"] = None
                state["maker_waited_seconds"] = 0.0
                state["maker_touch_signal"] = False
                state["cycle_start_balance"] = None
                self.log(
                    f"[Martingale] WIN settled for {slug}. "
                    f"Net=${net_profit:.2f} recovered target=${recovery_target:.2f}. "
                    f"Resetting cycle; next target profit: ${self.martingale_initial_amount:.2f}."
                )
            else:
                residual_loss = max(previous_loss_bank - net_profit, 0.0)
                state["streak"] = 0
                state["direction"] = None
                state["loss_bank"] = round(residual_loss, 4)
                state["current_amount"] = 0.0
                state["entry_fee_usd"] = 0.0
                state["entry_route"] = ""
                self.log(
                    f"[Martingale] WIN settled for {slug}, but fill did not recover cycle target. "
                    f"Net=${net_profit:.2f}, target=${recovery_target:.2f}, "
                    f"carrying residual loss_bank=${state['loss_bank']:.2f}."
                )
        else:
            state["streak"] = int(state.get("streak", 0) or 0) + 1
            loss_bank = previous_loss_bank
            loss_bank += entry_notional + entry_fee
            state["loss_bank"] = round(loss_bank, 4)
            state["current_amount"] = 0.0
            state["entry_fee_usd"] = 0.0
            state["entry_route"] = ""
            self.log(
                f"[Martingale] LOSS settled for {slug}. "
                f"Loss bank: ${state['loss_bank']:.2f} (includes est. fee ${entry_fee:.2f}). Next stake will target "
                f"${self.martingale_initial_amount:.2f} profit in same direction ({state.get('direction')})."
            )

    def _binary_result_from_price(self, price: float):
        try:
            p = float(price)
        except Exception:
            return None
        if p >= 0.99:
            return True
        if p <= 0.01:
            return False
        return None

    async def _settle_active_martingale_trade(self, state: Dict, slug: str, start_time, now_ts: float) -> bool:
        target_token_id = state.get("target_token_id")
        if not target_token_id:
            return False

        is_win, opening_price, closing_price = await self._infer_chainlink_window_result(
            slug=slug,
            direction=state.get("direction"),
        )
        chainlink_detail = getattr(self, "_last_chainlink_settlement_detail", {}) or {}
        settlement_source = chainlink_detail.get("source", "chainlink_boundary")
        settled_price = None
        if is_win is None:
            is_win, settled_price = await self._infer_settlement_result(slug, target_token_id)
            settlement_source = "official_outcome"
        if is_win is None:
            return False

        exit_price = 1.0 if is_win else 0.0
        if settled_price is not None:
            exit_price = float(settled_price)
        self.log(
            f"[Martingale] EXIT | slug={slug} | direction={state.get('direction')} | "
            f"exit_price={exit_price} | settlement={settlement_source}"
        )
        if settlement_source.startswith("chainlink_"):
            self.log(
                f"[Martingale] Chainlink boundary | open={opening_price:.8f} | "
                f"close={closing_price:.8f} | delta={chainlink_detail.get('price_delta', 0.0):.8f} | "
                f"open_offset={chainlink_detail.get('opening_offset_seconds', 0.0):+.3f}s | "
                f"close_offset={chainlink_detail.get('closing_offset_seconds', 0.0):+.3f}s | "
                f"margin=${chainlink_detail.get('min_margin', 0.0):.2f} | "
                f"decision_latency={max(time.time() - (start_time.timestamp() + 300), 0.0):.2f}s"
            )

        settlement_key = f"{slug}|{state.get('opened_at').isoformat() if hasattr(state.get('opened_at'), 'isoformat') else (state.get('opened_at') or now_ts)}"
        record_state = state.copy()
        self._schedule_martingale_trade_record(
            state=record_state,
            slug=slug,
            start_time=start_time,
            exit_price=exit_price,
            is_win=is_win,
            close_status="settled",
            close_error=f"Settlement source: {settlement_source}.",
            settlement_key=settlement_key,
        )

        shares = float(state.get("entry_shares", 0.0))
        entry_price = float(state.get("entry_price", 0.0))
        self._apply_martingale_result(state, slug, bool(is_win), shares, entry_price)
        state["entry_done"] = False
        state["active_slug"] = None
        state["target_token_id"] = None
        state["entry_price"] = 0.0
        state["entry_shares"] = 0.0
        state["entry_fee_usd"] = 0.0
        state["opened_at"] = None
        state["entry_retry_after"] = 0.0
        state["entry_attempts"] = 0
        state["skip_logged"] = False
        self.save_state()
        return True

    def _extract_clob_token_ids(self, market_data: Dict) -> List[str]:
        markets = market_data.get("markets", []) if isinstance(market_data, dict) else []
        if not markets:
            return []
        token_ids = markets[0].get("clobTokenIds", [])
        if isinstance(token_ids, str):
            try:
                token_ids = json.loads(token_ids)
            except json.JSONDecodeError:
                token_ids = []
        if not isinstance(token_ids, list) or len(token_ids) != 2:
            return []
        return [str(token_id) for token_id in token_ids]

    async def _prepare_martingale_entry_plan(self, state: Dict, slug: str, now_ts: float):
        existing = self._martingale_entry_plans.get(slug)
        if existing and existing.get("token_ids"):
            return existing

        last_attempt = float(self._martingale_prefetch_attempts.get(slug, 0.0) or 0.0)
        if now_ts - last_attempt < 5.0:
            return existing
        self._martingale_prefetch_attempts[slug] = now_ts
        market_data = await self.get_market_by_slug(slug, use_cache=True, cache_ttl=900)
        token_ids = self._extract_clob_token_ids(market_data)
        if not token_ids:
            return None

        current_shares = float(state.get("entry_shares", 0.0))
        current_price = float(state.get("entry_price", 0.0))
        current_cost = float(state.get("current_amount", 0.0) or (current_shares * current_price))
        current_fee = self._entry_fee_from_state(state, current_shares, current_price)
        loss_bank_before = max(float(state.get("loss_bank", 0.0)), 0.0)
        plan = {
            "slug": slug,
            "market_data": market_data,
            "token_ids": token_ids,
            "prepared_at": now_ts,
            "win": {
                "direction": None,
                "streak": 0,
                "loss_bank": 0.0,
                "recovery_target": float(self.martingale_initial_amount),
            },
            "loss": {
                "direction": state.get("direction"),
                "streak": int(state.get("streak", 0) or 0) + 1,
                "loss_bank": round(loss_bank_before + current_cost + current_fee, 4),
                "recovery_target": round(
                    loss_bank_before + current_cost + current_fee + float(self.martingale_initial_amount),
                    4,
                ),
            },
        }
        self._martingale_entry_plans[slug] = plan
        stale_slugs = sorted(
            self._martingale_entry_plans,
            key=lambda item: self._martingale_entry_plans[item].get("prepared_at", 0.0),
        )[:-4]
        for stale_slug in stale_slugs:
            self._martingale_entry_plans.pop(stale_slug, None)
        self.log(
            f"[Martingale] Next window prepared | slug={slug} | "
            f"win_target=${plan['win']['recovery_target']:.2f} | "
            f"loss_target=${plan['loss']['recovery_target']:.2f}"
        )
        return plan

    def _is_permanent_order_error(self, error_text: str) -> bool:
        lower_error = (error_text or "").lower()
        permanent_markers = [
            "invalid amounts",
            "invalid amount",
            "invalid_order_min_size",
            "lower than the minimum",
            "max accuracy",
            "minimum",
        ]
        return any(marker in lower_error for marker in permanent_markers)

    def _martingale_retry_delay(self, error_text: str, attempts: int) -> float:
        import random
        import re

        lower_error = (error_text or "").lower()
        if self._is_permanent_order_error(lower_error):
            return 60.0

        retry_after_match = re.search(r"retry_after_seconds['\"]?\s*[:=]\s*(\d+)", lower_error)
        if retry_after_match:
            return float(min(max(int(retry_after_match.group(1)), 1), 60))

        if "post-only mode" in lower_error or "cancel-only" in lower_error:
            return 30.0

        if (
            "425" in lower_error
            or "too early" in lower_error
            or "service not ready" in lower_error
            or "market is not yet ready" in lower_error
            or "no orders found to match" in lower_error
        ):
            if "no orders found to match" in lower_error:
                return 1.0
            base = min(2 ** min(max(attempts - 1, 0), 5), 30)
            return float(base + random.uniform(0.0, 1.0))

        if "order timed out" in lower_error or "request exception" in lower_error or "500" in lower_error:
            return float(min(4 + (attempts * 2), 20))

        return 15.0

    def _append_trade_to_excel(self, row: Dict):
        import os
        import pandas as pd

        excel_path = os.path.join(os.path.dirname(__file__), "..", "historical_performance.xlsx")
        new_row = pd.DataFrame([row])
        if os.path.exists(excel_path):
            df = pd.read_excel(excel_path)
            df = pd.concat([df, new_row], ignore_index=True)
        else:
            df = new_row
        df.to_excel(excel_path, index=False)

    def _update_trade_row_in_excel(self, settlement_key: str, updates: Dict):
        import os
        import pandas as pd

        excel_path = os.path.join(os.path.dirname(__file__), "..", "historical_performance.xlsx")
        if not os.path.exists(excel_path):
            return False
        df = pd.read_excel(excel_path)
        if "Settlement Key" not in df.columns:
            return False
        mask = df["Settlement Key"].astype(str) == str(settlement_key)
        if not mask.any():
            return False
        idx = df[mask].index[-1]
        for k, v in updates.items():
            if k not in df.columns:
                df[k] = None
            # Excel columns can be inferred as int64 after early losing trades
            # with payout 0; cast before assigning decimal payouts like 3.7.
            df[k] = df[k].astype("object")
            df.at[idx, k] = v
        df.to_excel(excel_path, index=False)
        return True

    def _record_martingale_trade_row(
        self,
        state: Dict,
        slug: str,
        start_time,
        exit_price: float,
        is_win: Optional[bool],
        close_status: str = "closed_ok",
        close_error: str = "",
        settlement_key: str = "",
    ):
        import datetime
        opened_at = state.get("opened_at")
        closed_at = datetime.datetime.now(datetime.timezone.utc)
        market_timestamp = None
        if isinstance(start_time, datetime.datetime):
            market_timestamp = start_time.isoformat()

        entry_shares = float(state.get("entry_shares", 0.0))
        entry_price = float(state.get("entry_price", 0.0))
        bet_size_usd = float(state.get("current_amount", 0.0))
        entry_fee_usd = self._entry_fee_from_state(state, entry_shares, entry_price)

        entry_notional_usd = round(bet_size_usd or (entry_shares * entry_price), 4)
        exit_value_est_usd = round(entry_shares * exit_price, 4)
        payout_est_usd = round(entry_shares * (1.0 if is_win is True else 0.0), 4)
        if is_win is None:
            pnl_est_usd = round(exit_value_est_usd - entry_notional_usd - entry_fee_usd, 4)
        else:
            pnl_est_usd = round(payout_est_usd - entry_notional_usd - entry_fee_usd, 4)
        session_stats = self.app_state["martingale_session_stats"]
        if is_win is not None:
            session_stats["trades"] += 1
            if is_win:
                session_stats["wins"] += 1
            else:
                session_stats["losses"] += 1
        session_stats["cum_pnl_usd"] = round(float(session_stats["cum_pnl_usd"]) + pnl_est_usd, 4)
        trades = int(session_stats["trades"])
        wins = int(session_stats["wins"])
        losses = int(session_stats["losses"])
        win_rate = round((wins / trades) * 100.0, 2) if trades > 0 else 0.0

        self._append_trade_to_excel({
            "Market": slug,
            "Market timestamp (UTC)": market_timestamp,
            "Position opened timestamp (UTC)": opened_at.isoformat() if hasattr(opened_at, "isoformat") else opened_at,
            "Position closed timestamp (UTC)": closed_at.isoformat(),
            "Market timestamp (local)": self._format_ts_local(start_time if isinstance(start_time, datetime.datetime) else None),
            "Position opened timestamp (local)": self._format_ts_local(opened_at),
            "Position closed timestamp (local)": self._format_ts_local(closed_at),
            "Direction": state["direction"],
            "Bet Size USD (target)": round(bet_size_usd, 4),
            "Shares": round(entry_shares, 4),
            "Entry Price": round(entry_price, 6),
            "Entry Route": state.get("entry_route", ""),
            "Maker Price": state.get("maker_price"),
            "Maker Wait Seconds": state.get("maker_waited_seconds", 0.0),
            "Maker Touch Signal": state.get("maker_touch_signal", False),
            "Exit Price (snapshot)": round(exit_price, 6),
            "Entry Notional USD": entry_notional_usd,
            "Entry Fee USD (estimate)": round(entry_fee_usd, 4),
            "Exit Value USD (estimate)": exit_value_est_usd,
            "Result": "WIN" if is_win is True else ("LOSS" if is_win is False else "PENDING"),
            "Payout USD (estimate)": payout_est_usd,
            "PnL USD (estimate)": pnl_est_usd,
            "Close Status": close_status,
            "Close Error": close_error[:220] if close_error else "",
            "Settlement Key": settlement_key,
            "Token ID": state.get("target_token_id"),
            "Streak_Level": state["streak"],
            "Dry_Run": self.dry_run,
            "Session started at (UTC)": session_stats["started_at"],
            "Session trades": trades,
            "Session wins": wins,
            "Session losses": losses,
            "Session win rate %": win_rate,
            "Session cumulative PnL USD": float(session_stats["cum_pnl_usd"]),
        })

    def _format_ts_local(self, dt):
        if dt is None:
            return None
        if isinstance(dt, str):
            return dt
        try:
            local_tz = ZoneInfo(os.getenv("BOT_TIMEZONE", "Europe/Madrid"))
            return dt.astimezone(local_tz).strftime("%Y-%m-%d %H:%M:%S %Z")
        except Exception:
            return dt.isoformat()

    def load_state(self):
        import json
        if os.path.exists(self.data_file):
            try:
                with open(self.data_file, 'r') as f:
                    data = json.load(f)
                    config = data.get('config', {})
                    # Load list of wallets if present, else fallback to single
                    self.target_wallets = config.get('target_wallets', [])
                    if not self.target_wallets and config.get('target_wallet'):
                        self.target_wallets = [config.get('target_wallet')]
                        
                    self.dry_run = config.get('dry_run', self.dry_run)
                    self.poll_interval = config.get('poll_interval', self.poll_interval)
                    self.size_mode = config.get('size_mode', self.size_mode)
                    self.size_value = config.get('size_value', self.size_value)
                    
                    self.winning_strategy_enabled = config.get('winning_strategy_enabled', False)
                    self.winning_price_threshold = config.get('winning_price_threshold', 0.75)
                    self.winning_entry_time = config.get('winning_entry_time', 3.0)
                    self.winning_exit_time = config.get('winning_exit_time', 4.5)
                    self.strategy_mode = config.get('strategy_mode', self.strategy_mode)
                    self.winning_size_mode = config.get('winning_size_mode', self.winning_size_mode)
                    self.winning_size_value = config.get('winning_size_value', self.winning_size_value)
                    self.martingale_initial_amount = config.get('martingale_initial_amount', self.martingale_initial_amount)
                    self.martingale_entry_start_seconds = config.get('martingale_entry_start_seconds', self.martingale_entry_start_seconds)
                    self.martingale_entry_end_seconds = config.get('martingale_entry_end_seconds', self.martingale_entry_end_seconds)
                    self.martingale_min_entry_price = config.get('martingale_min_entry_price', self.martingale_min_entry_price)
                    self.martingale_max_entry_price = config.get('martingale_max_entry_price', self.martingale_max_entry_price)
                    self.martingale_recovery_entry_end_seconds = config.get('martingale_recovery_entry_end_seconds', self.martingale_recovery_entry_end_seconds)
                    self.martingale_recovery_min_entry_price = config.get('martingale_recovery_min_entry_price', self.martingale_recovery_min_entry_price)
                    self.martingale_recovery_max_entry_price = config.get('martingale_recovery_max_entry_price', self.martingale_recovery_max_entry_price)
                    self.martingale_taker_fee_rate = config.get('martingale_taker_fee_rate', self.martingale_taker_fee_rate)
                    self.martingale_balance_floor_fraction = config.get('martingale_balance_floor_fraction', self.martingale_balance_floor_fraction)
                    self.martingale_order_type = config.get('martingale_order_type', self.martingale_order_type)
                    self.martingale_chainlink_boundary_tolerance_seconds = config.get('martingale_chainlink_boundary_tolerance_seconds', self.martingale_chainlink_boundary_tolerance_seconds)
                    self.martingale_chainlink_min_decision_margin = config.get('martingale_chainlink_min_decision_margin', self.martingale_chainlink_min_decision_margin)
                    self.martingale_maker_wait_seconds = config.get('martingale_maker_wait_seconds', self.martingale_maker_wait_seconds)
                    self.martingale_maker_price_offset = config.get('martingale_maker_price_offset', self.martingale_maker_price_offset)
                    self.martingale_maker_sample_seconds = config.get('martingale_maker_sample_seconds', self.martingale_maker_sample_seconds)
                    self.martingale_maker_record_seconds = config.get('martingale_maker_record_seconds', self.martingale_maker_record_seconds)
                    self.martingale_maker_blind_price = config.get('martingale_maker_blind_price', self.martingale_maker_blind_price)
                    self.martingale_maker_blind_end_seconds = config.get('martingale_maker_blind_end_seconds', self.martingale_maker_blind_end_seconds)
                    self.fair_value_stake_usd = config.get('fair_value_stake_usd', self.fair_value_stake_usd)
                    self.fair_value_min_taker_edge = config.get('fair_value_min_taker_edge', self.fair_value_min_taker_edge)
                    self.fair_value_min_maker_edge = config.get('fair_value_min_maker_edge', self.fair_value_min_maker_edge)
                    self.fair_value_min_ev_per_usd = config.get('fair_value_min_ev_per_usd', self.fair_value_min_ev_per_usd)
                    self.fair_value_taker_guard_min_edge = config.get(
                        'fair_value_taker_guard_min_edge',
                        self.fair_value_taker_guard_min_edge,
                    )
                    self.fair_value_taker_guard_min_ev_per_usd = config.get(
                        'fair_value_taker_guard_min_ev_per_usd',
                        self.fair_value_taker_guard_min_ev_per_usd,
                    )
                    self.fair_value_entry_start_seconds = config.get('fair_value_entry_start_seconds', self.fair_value_entry_start_seconds)
                    self.fair_value_entry_end_seconds = config.get('fair_value_entry_end_seconds', self.fair_value_entry_end_seconds)
                    self.fair_value_min_price = config.get('fair_value_min_price', self.fair_value_min_price)
                    self.fair_value_max_price = config.get('fair_value_max_price', self.fair_value_max_price)
                    self.fair_value_core_min_price = config.get(
                        'fair_value_core_min_price',
                        getattr(self, 'fair_value_core_min_price', self.fair_value_min_price),
                    )
                    self.fair_value_core_max_price = config.get(
                        'fair_value_core_max_price',
                        getattr(self, 'fair_value_core_max_price', self.fair_value_max_price),
                    )
                    self.fair_value_maker_wait_seconds = config.get('fair_value_maker_wait_seconds', self.fair_value_maker_wait_seconds)
                    self.fair_value_sample_seconds = config.get('fair_value_sample_seconds', self.fair_value_sample_seconds)
                    self.fair_value_model_sensitivity_bps = config.get('fair_value_model_sensitivity_bps', self.fair_value_model_sensitivity_bps)
                    self.fair_value_prefer_maker = bool(config.get('fair_value_prefer_maker', self.fair_value_prefer_maker))
                    self.fair_value_live_trading_enabled = bool(config.get('fair_value_live_trading_enabled', self.fair_value_live_trading_enabled))
                    self.fair_value_order_type = config.get('fair_value_order_type', self.fair_value_order_type)
                    self.fair_value_core_enabled = bool(config.get('fair_value_core_enabled', self.fair_value_core_enabled))
                    self.fair_value_core_contrarian_only = bool(config.get(
                        'fair_value_core_contrarian_only',
                        self.fair_value_core_contrarian_only,
                    ))
                    self.fair_value_core_max_entries_per_window = config.get(
                        'fair_value_core_max_entries_per_window',
                        self.fair_value_core_max_entries_per_window,
                    )
                    self.fair_value_min_ev_improvement_per_entry = config.get(
                        'fair_value_min_ev_improvement_per_entry',
                        self.fair_value_min_ev_improvement_per_entry,
                    )
                    self.fair_value_momentum_enabled = bool(config.get('fair_value_momentum_enabled', self.fair_value_momentum_enabled))
                    self.fair_value_momentum_start_seconds = config.get('fair_value_momentum_start_seconds', self.fair_value_momentum_start_seconds)
                    self.fair_value_momentum_end_seconds = config.get('fair_value_momentum_end_seconds', self.fair_value_momentum_end_seconds)
                    self.fair_value_momentum_min_edge = config.get('fair_value_momentum_min_edge', self.fair_value_momentum_min_edge)
                    self.fair_value_momentum_min_ev_per_usd = config.get('fair_value_momentum_min_ev_per_usd', self.fair_value_momentum_min_ev_per_usd)
                    self.fair_value_momentum_min_confidence = config.get('fair_value_momentum_min_confidence', self.fair_value_momentum_min_confidence)
                    self.fair_value_momentum_max_price = config.get('fair_value_momentum_max_price', self.fair_value_momentum_max_price)
                    self.fair_value_momentum_stake_multiplier = config.get('fair_value_momentum_stake_multiplier', self.fair_value_momentum_stake_multiplier)
                    self.fair_value_momentum_max_entries_per_window = config.get(
                        'fair_value_momentum_max_entries_per_window',
                        self.fair_value_momentum_max_entries_per_window,
                    )
                    self.fair_value_momentum_min_ev_improvement_per_entry = config.get(
                        'fair_value_momentum_min_ev_improvement_per_entry',
                        self.fair_value_momentum_min_ev_improvement_per_entry,
                    )
                    self.fair_value_late_continuation_enabled = bool(config.get(
                        'fair_value_late_continuation_enabled',
                        self.fair_value_late_continuation_enabled,
                    ))
                    self.fair_value_late_continuation_start_seconds = config.get(
                        'fair_value_late_continuation_start_seconds',
                        self.fair_value_late_continuation_start_seconds,
                    )
                    self.fair_value_late_continuation_end_seconds = config.get(
                        'fair_value_late_continuation_end_seconds',
                        self.fair_value_late_continuation_end_seconds,
                    )
                    self.fair_value_late_continuation_min_abs_delta_bps = config.get(
                        'fair_value_late_continuation_min_abs_delta_bps',
                        self.fair_value_late_continuation_min_abs_delta_bps,
                    )
                    self.fair_value_late_continuation_min_probability = config.get(
                        'fair_value_late_continuation_min_probability',
                        self.fair_value_late_continuation_min_probability,
                    )
                    self.fair_value_late_continuation_max_probability = config.get(
                        'fair_value_late_continuation_max_probability',
                        self.fair_value_late_continuation_max_probability,
                    )
                    self.fair_value_late_continuation_min_ev_per_usd = config.get(
                        'fair_value_late_continuation_min_ev_per_usd',
                        self.fair_value_late_continuation_min_ev_per_usd,
                    )
                    self.fair_value_late_continuation_price_buffer = config.get(
                        'fair_value_late_continuation_price_buffer',
                        self.fair_value_late_continuation_price_buffer,
                    )
                    self.fair_value_late_continuation_max_price = config.get(
                        'fair_value_late_continuation_max_price',
                        self.fair_value_late_continuation_max_price,
                    )
                    self.fair_value_late_continuation_stake_multiplier = config.get(
                        'fair_value_late_continuation_stake_multiplier',
                        self.fair_value_late_continuation_stake_multiplier,
                    )
                    self.fair_value_late_continuation_max_entries_per_window = config.get(
                        'fair_value_late_continuation_max_entries_per_window',
                        self.fair_value_late_continuation_max_entries_per_window,
                    )
                    self.fair_value_late_continuation_min_ev_improvement_per_entry = config.get(
                        'fair_value_late_continuation_min_ev_improvement_per_entry',
                        self.fair_value_late_continuation_min_ev_improvement_per_entry,
                    )
                    self.fair_value_late_continuation_entry_model_min_win_prob = config.get(
                        'fair_value_late_continuation_entry_model_min_win_prob',
                        self.fair_value_late_continuation_entry_model_min_win_prob,
                    )
                    self.fair_value_giro_probe_enabled = bool(config.get(
                        'fair_value_giro_probe_enabled',
                        self.fair_value_giro_probe_enabled,
                    ))
                    self.fair_value_giro_probe_start_seconds = config.get(
                        'fair_value_giro_probe_start_seconds',
                        self.fair_value_giro_probe_start_seconds,
                    )
                    self.fair_value_giro_probe_end_seconds = config.get(
                        'fair_value_giro_probe_end_seconds',
                        self.fair_value_giro_probe_end_seconds,
                    )
                    self.fair_value_giro_probe_min_price = config.get(
                        'fair_value_giro_probe_min_price',
                        self.fair_value_giro_probe_min_price,
                    )
                    self.fair_value_giro_probe_max_price = config.get(
                        'fair_value_giro_probe_max_price',
                        self.fair_value_giro_probe_max_price,
                    )
                    self.fair_value_giro_probe_min_abs_delta_bps = config.get(
                        'fair_value_giro_probe_min_abs_delta_bps',
                        self.fair_value_giro_probe_min_abs_delta_bps,
                    )
                    self.fair_value_giro_probe_min_probability = config.get(
                        'fair_value_giro_probe_min_probability',
                        self.fair_value_giro_probe_min_probability,
                    )
                    self.fair_value_giro_probe_min_ev_per_usd = config.get(
                        'fair_value_giro_probe_min_ev_per_usd',
                        self.fair_value_giro_probe_min_ev_per_usd,
                    )
                    self.fair_value_giro_probe_min_confidence = config.get(
                        'fair_value_giro_probe_min_confidence',
                        self.fair_value_giro_probe_min_confidence,
                    )
                    self.fair_value_giro_probe_max_entries_per_window = config.get(
                        'fair_value_giro_probe_max_entries_per_window',
                        self.fair_value_giro_probe_max_entries_per_window,
                    )
                    self.fair_value_giro_probe_min_price_step = config.get(
                        'fair_value_giro_probe_min_price_step',
                        self.fair_value_giro_probe_min_price_step,
                    )
                    self.fair_value_giro_probe_stake_multiplier = config.get(
                        'fair_value_giro_probe_stake_multiplier',
                        self.fair_value_giro_probe_stake_multiplier,
                    )
                    self.fair_value_giro_probe_model_enabled = bool(config.get(
                        'fair_value_giro_probe_model_enabled',
                        self.fair_value_giro_probe_model_enabled,
                    ))
                    self.fair_value_giro_probe_model_path = config.get(
                        'fair_value_giro_probe_model_path',
                        self.fair_value_giro_probe_model_path,
                    )
                    self.fair_value_entry_model_enabled = bool(config.get(
                        'fair_value_entry_model_enabled',
                        self.fair_value_entry_model_enabled,
                    ))
                    self.fair_value_core_entry_model_min_win_prob = config.get(
                        'fair_value_core_entry_model_min_win_prob',
                        self.fair_value_core_entry_model_min_win_prob,
                    )
                    self.fair_value_core_entry_model_max_win_prob = config.get(
                        'fair_value_core_entry_model_max_win_prob',
                        self.fair_value_core_entry_model_max_win_prob,
                    )
                    self.fair_value_momentum_entry_model_min_win_prob = config.get(
                        'fair_value_momentum_entry_model_min_win_prob',
                        self.fair_value_momentum_entry_model_min_win_prob,
                    )
                    self.fair_value_shadow_dynamic_stake_enabled = bool(config.get(
                        'fair_value_shadow_dynamic_stake_enabled',
                        self.fair_value_shadow_dynamic_stake_enabled,
                    ))
                    self.fair_value_shadow_dynamic_stake_min_multiplier = config.get(
                        'fair_value_shadow_dynamic_stake_min_multiplier',
                        self.fair_value_shadow_dynamic_stake_min_multiplier,
                    )
                    self.fair_value_shadow_dynamic_stake_max_multiplier = config.get(
                        'fair_value_shadow_dynamic_stake_max_multiplier',
                        self.fair_value_shadow_dynamic_stake_max_multiplier,
                    )
                    self.fair_value_entry_model_path = config.get(
                        'fair_value_entry_model_path',
                        self.fair_value_entry_model_path,
                    )
                    self.martingale_taker_fee_rate = max(float(self.martingale_taker_fee_rate), 0.0)
                    self.martingale_balance_floor_fraction = min(max(float(self.martingale_balance_floor_fraction), 0.0), 1.0)
                    self.martingale_order_type = str(self.martingale_order_type or "FOK").upper()
                    if self.martingale_order_type not in ("FAK", "FOK"):
                        self.martingale_order_type = "FOK"
                    self.martingale_chainlink_boundary_tolerance_seconds = min(
                        max(float(self.martingale_chainlink_boundary_tolerance_seconds), 0.0),
                        5.0,
                    )
                    self.martingale_chainlink_min_decision_margin = max(
                        float(self.martingale_chainlink_min_decision_margin),
                        0.0,
                    )
                    self.martingale_maker_wait_seconds = min(max(float(self.martingale_maker_wait_seconds), 0.0), 10.0)
                    self.martingale_maker_price_offset = min(max(float(self.martingale_maker_price_offset), 0.0), 0.10)
                    self.martingale_maker_sample_seconds = min(max(float(self.martingale_maker_sample_seconds), 0.1), 5.0)
                    self.martingale_maker_record_seconds = min(max(float(self.martingale_maker_record_seconds), 1.0), 120.0)
                    self.martingale_maker_blind_price = min(max(float(self.martingale_maker_blind_price), 0.40), 0.60)
                    self.martingale_maker_blind_end_seconds = min(max(float(self.martingale_maker_blind_end_seconds), 0.0), 30.0)
                    self.fair_value_stake_usd = min(max(float(self.fair_value_stake_usd), 0.01), 1000.0)
                    self.fair_value_min_taker_edge = min(max(float(self.fair_value_min_taker_edge), 0.0), 0.50)
                    self.fair_value_min_maker_edge = min(max(float(self.fair_value_min_maker_edge), 0.0), 0.50)
                    self.fair_value_min_ev_per_usd = min(max(float(self.fair_value_min_ev_per_usd), 0.0), 5.0)
                    self.fair_value_taker_guard_min_edge = min(max(float(self.fair_value_taker_guard_min_edge), 0.0), 0.50)
                    self.fair_value_taker_guard_min_ev_per_usd = min(max(float(self.fair_value_taker_guard_min_ev_per_usd), 0.0), 5.0)
                    self.fair_value_entry_start_seconds = min(max(float(self.fair_value_entry_start_seconds), 0.0), 300.0)
                    self.fair_value_entry_end_seconds = min(max(float(self.fair_value_entry_end_seconds), 1.0), 300.0)
                    if self.fair_value_entry_end_seconds < self.fair_value_entry_start_seconds:
                        self.fair_value_entry_end_seconds = self.fair_value_entry_start_seconds
                    self.fair_value_min_price = min(max(float(self.fair_value_min_price), 0.01), 0.99)
                    self.fair_value_max_price = min(max(float(self.fair_value_max_price), 0.01), 0.99)
                    if self.fair_value_min_price > self.fair_value_max_price:
                        self.fair_value_min_price, self.fair_value_max_price = self.fair_value_max_price, self.fair_value_min_price
                    core_min_price = float(getattr(self, "fair_value_core_min_price", self.fair_value_min_price))
                    core_max_price = float(getattr(self, "fair_value_core_max_price", self.fair_value_max_price))
                    self.fair_value_core_min_price = min(max(core_min_price, 0.01), 0.99)
                    self.fair_value_core_max_price = min(max(core_max_price, 0.01), 0.99)
                    if self.fair_value_core_min_price > self.fair_value_core_max_price:
                        self.fair_value_core_min_price, self.fair_value_core_max_price = (
                            self.fair_value_core_max_price,
                            self.fair_value_core_min_price,
                        )
                    self.fair_value_maker_wait_seconds = min(max(float(self.fair_value_maker_wait_seconds), 0.0), 30.0)
                    self.fair_value_sample_seconds = min(max(float(self.fair_value_sample_seconds), 0.1), 10.0)
                    self.fair_value_model_sensitivity_bps = min(max(float(self.fair_value_model_sensitivity_bps), 1.0), 250.0)
                    self.fair_value_momentum_start_seconds = min(max(float(self.fair_value_momentum_start_seconds), 0.0), 300.0)
                    self.fair_value_momentum_end_seconds = min(max(float(self.fair_value_momentum_end_seconds), 1.0), 300.0)
                    if self.fair_value_momentum_end_seconds < self.fair_value_momentum_start_seconds:
                        self.fair_value_momentum_end_seconds = self.fair_value_momentum_start_seconds
                    self.fair_value_momentum_min_edge = min(max(float(self.fair_value_momentum_min_edge), 0.0), 0.50)
                    self.fair_value_momentum_min_ev_per_usd = min(max(float(self.fair_value_momentum_min_ev_per_usd), 0.0), 5.0)
                    self.fair_value_momentum_min_confidence = min(max(float(self.fair_value_momentum_min_confidence), 0.0), 1.0)
                    self.fair_value_momentum_max_price = min(max(float(self.fair_value_momentum_max_price), 0.01), 0.99)
                    self.fair_value_momentum_stake_multiplier = min(max(float(self.fair_value_momentum_stake_multiplier), 0.01), 25.0)
                    self.fair_value_order_type = str(self.fair_value_order_type or "FOK").upper()
                    if self.fair_value_order_type not in ("FAK", "FOK"):
                        self.fair_value_order_type = "FOK"
                    self.fair_value_core_max_entries_per_window = min(
                        max(int(self.fair_value_core_max_entries_per_window), 0),
                        50,
                    )
                    self.fair_value_min_ev_improvement_per_entry = min(
                        max(float(self.fair_value_min_ev_improvement_per_entry), 0.0),
                        5.0,
                    )
                    self.fair_value_momentum_max_entries_per_window = min(
                        max(int(self.fair_value_momentum_max_entries_per_window), 0),
                        50,
                    )
                    self.fair_value_momentum_min_ev_improvement_per_entry = min(
                        max(float(self.fair_value_momentum_min_ev_improvement_per_entry), 0.0),
                        5.0,
                    )
                    self.fair_value_late_continuation_start_seconds = min(
                        max(float(self.fair_value_late_continuation_start_seconds), 0.0),
                        300.0,
                    )
                    self.fair_value_late_continuation_end_seconds = min(
                        max(float(self.fair_value_late_continuation_end_seconds), 1.0),
                        300.0,
                    )
                    if self.fair_value_late_continuation_end_seconds < self.fair_value_late_continuation_start_seconds:
                        self.fair_value_late_continuation_end_seconds = self.fair_value_late_continuation_start_seconds
                    self.fair_value_late_continuation_min_abs_delta_bps = min(
                        max(float(self.fair_value_late_continuation_min_abs_delta_bps), 0.0),
                        250.0,
                    )
                    self.fair_value_late_continuation_min_probability = min(
                        max(float(self.fair_value_late_continuation_min_probability), 0.5),
                        0.999,
                    )
                    self.fair_value_late_continuation_max_probability = min(
                        max(float(self.fair_value_late_continuation_max_probability), 0.5),
                        0.999,
                    )
                    if self.fair_value_late_continuation_min_probability > self.fair_value_late_continuation_max_probability:
                        self.fair_value_late_continuation_min_probability, self.fair_value_late_continuation_max_probability = (
                            self.fair_value_late_continuation_max_probability,
                            self.fair_value_late_continuation_min_probability,
                        )
                    self.fair_value_late_continuation_min_ev_per_usd = min(
                        max(float(self.fair_value_late_continuation_min_ev_per_usd), 0.0),
                        5.0,
                    )
                    self.fair_value_late_continuation_price_buffer = min(
                        max(float(self.fair_value_late_continuation_price_buffer), 0.0),
                        0.25,
                    )
                    self.fair_value_late_continuation_max_price = min(
                        max(float(self.fair_value_late_continuation_max_price), 0.01),
                        0.99,
                    )
                    self.fair_value_late_continuation_stake_multiplier = min(
                        max(float(self.fair_value_late_continuation_stake_multiplier), 0.01),
                        25.0,
                    )
                    self.fair_value_late_continuation_max_entries_per_window = min(
                        max(int(self.fair_value_late_continuation_max_entries_per_window), 0),
                        50,
                    )
                    self.fair_value_late_continuation_min_ev_improvement_per_entry = min(
                        max(float(self.fair_value_late_continuation_min_ev_improvement_per_entry), 0.0),
                        5.0,
                    )
                    self.fair_value_late_continuation_entry_model_min_win_prob = min(
                        max(float(self.fair_value_late_continuation_entry_model_min_win_prob), 0.0),
                        1.0,
                    )
                    self.fair_value_giro_probe_start_seconds = min(max(float(self.fair_value_giro_probe_start_seconds), 0.0), 300.0)
                    self.fair_value_giro_probe_end_seconds = min(max(float(self.fair_value_giro_probe_end_seconds), 1.0), 300.0)
                    if self.fair_value_giro_probe_end_seconds < self.fair_value_giro_probe_start_seconds:
                        self.fair_value_giro_probe_end_seconds = self.fair_value_giro_probe_start_seconds
                    self.fair_value_giro_probe_min_price = min(max(float(self.fair_value_giro_probe_min_price), 0.01), 0.99)
                    self.fair_value_giro_probe_max_price = min(max(float(self.fair_value_giro_probe_max_price), 0.01), 0.99)
                    if self.fair_value_giro_probe_min_price > self.fair_value_giro_probe_max_price:
                        self.fair_value_giro_probe_min_price, self.fair_value_giro_probe_max_price = (
                            self.fair_value_giro_probe_max_price,
                            self.fair_value_giro_probe_min_price,
                        )
                    self.fair_value_giro_probe_min_abs_delta_bps = min(max(float(self.fair_value_giro_probe_min_abs_delta_bps), 0.0), 250.0)
                    self.fair_value_giro_probe_min_probability = min(max(float(self.fair_value_giro_probe_min_probability), 0.0), 1.0)
                    self.fair_value_giro_probe_min_ev_per_usd = min(max(float(self.fair_value_giro_probe_min_ev_per_usd), -1.0), 10.0)
                    self.fair_value_giro_probe_min_confidence = min(max(float(self.fair_value_giro_probe_min_confidence), 0.0), 1.0)
                    self.fair_value_giro_probe_max_entries_per_window = min(max(int(self.fair_value_giro_probe_max_entries_per_window), 0), 100)
                    self.fair_value_giro_probe_min_price_step = min(max(float(self.fair_value_giro_probe_min_price_step), 0.0), 0.50)
                    self.fair_value_giro_probe_stake_multiplier = min(max(float(self.fair_value_giro_probe_stake_multiplier), 0.01), 25.0)
                    self.fair_value_giro_probe_model_path = str(self.fair_value_giro_probe_model_path or os.path.join(
                        self.data_dir,
                        "cheap_reversal",
                        "cheap_reversal_model.json",
                    ))
                    self.fair_value_core_entry_model_min_win_prob = min(
                        max(float(self.fair_value_core_entry_model_min_win_prob), 0.0),
                        1.0,
                    )
                    self.fair_value_core_entry_model_max_win_prob = min(
                        max(float(self.fair_value_core_entry_model_max_win_prob), 0.0),
                        1.0,
                    )
                    if self.fair_value_core_entry_model_min_win_prob > self.fair_value_core_entry_model_max_win_prob:
                        self.fair_value_core_entry_model_min_win_prob, self.fair_value_core_entry_model_max_win_prob = (
                            self.fair_value_core_entry_model_max_win_prob,
                            self.fair_value_core_entry_model_min_win_prob,
                        )
                    self.fair_value_momentum_entry_model_min_win_prob = min(
                        max(float(self.fair_value_momentum_entry_model_min_win_prob), 0.0),
                        1.0,
                    )
                    self.fair_value_shadow_dynamic_stake_min_multiplier = min(
                        max(float(self.fair_value_shadow_dynamic_stake_min_multiplier), 0.01),
                        10.0,
                    )
                    self.fair_value_shadow_dynamic_stake_max_multiplier = min(
                        max(
                            float(self.fair_value_shadow_dynamic_stake_max_multiplier),
                            self.fair_value_shadow_dynamic_stake_min_multiplier,
                        ),
                        10.0,
                    )
                    self.martingale_entry_end_seconds = 265
                    if self.martingale_min_entry_price < 0.40:
                        self.martingale_min_entry_price = 0.40
                    if self.martingale_max_entry_price > 0.60:
                        self.martingale_max_entry_price = 0.60
                    self.martingale_recovery_entry_end_seconds = min(
                        max(int(self.martingale_recovery_entry_end_seconds), 15),
                        285,
                    )
                    self.martingale_recovery_min_entry_price = max(
                        float(self.martingale_recovery_min_entry_price),
                        float(self.martingale_min_entry_price),
                    )
                    self.martingale_recovery_max_entry_price = min(
                        float(self.martingale_recovery_max_entry_price),
                        float(self.martingale_max_entry_price),
                    )
                    if self.martingale_recovery_min_entry_price > self.martingale_recovery_max_entry_price:
                        self.martingale_recovery_min_entry_price = self.martingale_min_entry_price
                        self.martingale_recovery_max_entry_price = self.martingale_max_entry_price
                    self.app_state["multipliers"] = data.get('multipliers', {})
                    self.app_state["pending_settlements"] = data.get('pending_settlements', [])
                    martingale_state = data.get('martingale_state')
                    if martingale_state:
                        opened_at = martingale_state.get("opened_at")
                        if isinstance(opened_at, str):
                            from datetime import datetime
                            try:
                                martingale_state["opened_at"] = datetime.fromisoformat(opened_at)
                            except ValueError:
                                martingale_state["opened_at"] = None
                        martingale_state.setdefault("loss_bank", 0.0)
                        martingale_state.setdefault("entry_retry_after", 0.0)
                        martingale_state.setdefault("entry_attempts", 0)
                        martingale_state.setdefault("skip_logged", False)
                        self.app_state["martingale_state"] = self._normalize_martingale_state(martingale_state)
                    fair_value_state = data.get('fair_value_state')
                    if fair_value_state:
                        self.app_state["fair_value_state"] = self._normalize_fair_value_state(fair_value_state)
                    fair_value_giro_probe_state = data.get('fair_value_giro_probe_state')
                    if fair_value_giro_probe_state:
                        self.app_state["fair_value_giro_probe_state"] = self._normalize_fair_value_giro_probe_state(
                            fair_value_giro_probe_state
                        )
                    self.history = data.get('history', [])
                    self.log(f"State loaded from {self.data_file}")
            except Exception as e:
                self.log(f"Error loading state: {e}")

    def save_state(self):
        import json
        martingale_state = self.app_state.get("martingale_state")
        if martingale_state:
            martingale_state = martingale_state.copy()
            opened_at = martingale_state.get("opened_at")
            if hasattr(opened_at, "isoformat"):
                martingale_state["opened_at"] = opened_at.isoformat()
        fair_value_state = self.app_state.get("fair_value_state")
        if fair_value_state:
            fair_value_state = fair_value_state.copy()
            opened_at = fair_value_state.get("opened_at")
            if hasattr(opened_at, "isoformat"):
                fair_value_state["opened_at"] = opened_at.isoformat()
        fair_value_giro_probe_state = self.app_state.get("fair_value_giro_probe_state")
        if fair_value_giro_probe_state:
            fair_value_giro_probe_state = fair_value_giro_probe_state.copy()
        data = {
            "config": {
                "target_wallets": self.target_wallets,
                "dry_run": self.dry_run,
                "poll_interval": self.poll_interval,
                "size_mode": self.size_mode,
                "size_value": self.size_value,
                "winning_strategy_enabled": self.winning_strategy_enabled,
                "winning_price_threshold": self.winning_price_threshold,
                "winning_entry_time": self.winning_entry_time,
                "winning_exit_time": self.winning_exit_time,
                "strategy_mode": self.strategy_mode,
                "winning_size_mode": self.winning_size_mode,
                "winning_size_value": self.winning_size_value,
                "martingale_initial_amount": self.martingale_initial_amount,
                "martingale_entry_start_seconds": self.martingale_entry_start_seconds,
                "martingale_entry_end_seconds": self.martingale_entry_end_seconds,
                "martingale_min_entry_price": self.martingale_min_entry_price,
                "martingale_max_entry_price": self.martingale_max_entry_price,
                "martingale_recovery_entry_end_seconds": self.martingale_recovery_entry_end_seconds,
                "martingale_recovery_min_entry_price": self.martingale_recovery_min_entry_price,
                "martingale_recovery_max_entry_price": self.martingale_recovery_max_entry_price,
                "martingale_taker_fee_rate": self.martingale_taker_fee_rate,
                "martingale_balance_floor_fraction": self.martingale_balance_floor_fraction,
                "martingale_order_type": self.martingale_order_type,
                "martingale_chainlink_boundary_tolerance_seconds": self.martingale_chainlink_boundary_tolerance_seconds,
                "martingale_chainlink_min_decision_margin": self.martingale_chainlink_min_decision_margin,
                "martingale_maker_wait_seconds": self.martingale_maker_wait_seconds,
                "martingale_maker_price_offset": self.martingale_maker_price_offset,
                "martingale_maker_sample_seconds": self.martingale_maker_sample_seconds,
                "martingale_maker_record_seconds": self.martingale_maker_record_seconds,
                "martingale_maker_blind_price": self.martingale_maker_blind_price,
                "martingale_maker_blind_end_seconds": self.martingale_maker_blind_end_seconds,
                "fair_value_stake_usd": self.fair_value_stake_usd,
                "fair_value_min_taker_edge": self.fair_value_min_taker_edge,
                "fair_value_min_maker_edge": self.fair_value_min_maker_edge,
                "fair_value_min_ev_per_usd": self.fair_value_min_ev_per_usd,
                "fair_value_taker_guard_min_edge": self.fair_value_taker_guard_min_edge,
                "fair_value_taker_guard_min_ev_per_usd": self.fair_value_taker_guard_min_ev_per_usd,
                "fair_value_entry_start_seconds": self.fair_value_entry_start_seconds,
                "fair_value_entry_end_seconds": self.fair_value_entry_end_seconds,
                "fair_value_min_price": self.fair_value_min_price,
                "fair_value_max_price": self.fair_value_max_price,
                "fair_value_core_min_price": self.fair_value_core_min_price,
                "fair_value_core_max_price": self.fair_value_core_max_price,
                "fair_value_maker_wait_seconds": self.fair_value_maker_wait_seconds,
                "fair_value_sample_seconds": self.fair_value_sample_seconds,
                "fair_value_model_sensitivity_bps": self.fair_value_model_sensitivity_bps,
                "fair_value_prefer_maker": self.fair_value_prefer_maker,
                "fair_value_live_trading_enabled": self.fair_value_live_trading_enabled,
                "fair_value_order_type": self.fair_value_order_type,
                "fair_value_core_enabled": self.fair_value_core_enabled,
                "fair_value_core_contrarian_only": self.fair_value_core_contrarian_only,
                "fair_value_core_max_entries_per_window": self.fair_value_core_max_entries_per_window,
                "fair_value_min_ev_improvement_per_entry": self.fair_value_min_ev_improvement_per_entry,
                "fair_value_momentum_enabled": self.fair_value_momentum_enabled,
                "fair_value_momentum_start_seconds": self.fair_value_momentum_start_seconds,
                "fair_value_momentum_end_seconds": self.fair_value_momentum_end_seconds,
                "fair_value_momentum_min_edge": self.fair_value_momentum_min_edge,
                "fair_value_momentum_min_ev_per_usd": self.fair_value_momentum_min_ev_per_usd,
                "fair_value_momentum_min_confidence": self.fair_value_momentum_min_confidence,
                "fair_value_momentum_max_price": self.fair_value_momentum_max_price,
                "fair_value_momentum_stake_multiplier": self.fair_value_momentum_stake_multiplier,
                "fair_value_momentum_max_entries_per_window": self.fair_value_momentum_max_entries_per_window,
                "fair_value_momentum_min_ev_improvement_per_entry": self.fair_value_momentum_min_ev_improvement_per_entry,
                "fair_value_late_continuation_enabled": self.fair_value_late_continuation_enabled,
                "fair_value_late_continuation_start_seconds": self.fair_value_late_continuation_start_seconds,
                "fair_value_late_continuation_end_seconds": self.fair_value_late_continuation_end_seconds,
                "fair_value_late_continuation_min_abs_delta_bps": self.fair_value_late_continuation_min_abs_delta_bps,
                "fair_value_late_continuation_min_probability": self.fair_value_late_continuation_min_probability,
                "fair_value_late_continuation_max_probability": self.fair_value_late_continuation_max_probability,
                "fair_value_late_continuation_min_ev_per_usd": self.fair_value_late_continuation_min_ev_per_usd,
                "fair_value_late_continuation_price_buffer": self.fair_value_late_continuation_price_buffer,
                "fair_value_late_continuation_max_price": self.fair_value_late_continuation_max_price,
                "fair_value_late_continuation_stake_multiplier": self.fair_value_late_continuation_stake_multiplier,
                "fair_value_late_continuation_max_entries_per_window": self.fair_value_late_continuation_max_entries_per_window,
                "fair_value_late_continuation_min_ev_improvement_per_entry": self.fair_value_late_continuation_min_ev_improvement_per_entry,
                "fair_value_late_continuation_entry_model_min_win_prob": self.fair_value_late_continuation_entry_model_min_win_prob,
                "fair_value_giro_probe_enabled": self.fair_value_giro_probe_enabled,
                "fair_value_giro_probe_start_seconds": self.fair_value_giro_probe_start_seconds,
                "fair_value_giro_probe_end_seconds": self.fair_value_giro_probe_end_seconds,
                "fair_value_giro_probe_min_price": self.fair_value_giro_probe_min_price,
                "fair_value_giro_probe_max_price": self.fair_value_giro_probe_max_price,
                "fair_value_giro_probe_min_abs_delta_bps": self.fair_value_giro_probe_min_abs_delta_bps,
                "fair_value_giro_probe_min_probability": self.fair_value_giro_probe_min_probability,
                "fair_value_giro_probe_min_ev_per_usd": self.fair_value_giro_probe_min_ev_per_usd,
                "fair_value_giro_probe_min_confidence": self.fair_value_giro_probe_min_confidence,
                "fair_value_giro_probe_max_entries_per_window": self.fair_value_giro_probe_max_entries_per_window,
                "fair_value_giro_probe_min_price_step": self.fair_value_giro_probe_min_price_step,
                "fair_value_giro_probe_stake_multiplier": self.fair_value_giro_probe_stake_multiplier,
                "fair_value_giro_probe_model_enabled": self.fair_value_giro_probe_model_enabled,
                "fair_value_giro_probe_model_path": self.fair_value_giro_probe_model_path,
                "fair_value_entry_model_enabled": self.fair_value_entry_model_enabled,
                "fair_value_core_entry_model_min_win_prob": self.fair_value_core_entry_model_min_win_prob,
                "fair_value_core_entry_model_max_win_prob": self.fair_value_core_entry_model_max_win_prob,
                "fair_value_momentum_entry_model_min_win_prob": self.fair_value_momentum_entry_model_min_win_prob,
                "fair_value_shadow_dynamic_stake_enabled": self.fair_value_shadow_dynamic_stake_enabled,
                "fair_value_shadow_dynamic_stake_min_multiplier": self.fair_value_shadow_dynamic_stake_min_multiplier,
                "fair_value_shadow_dynamic_stake_max_multiplier": self.fair_value_shadow_dynamic_stake_max_multiplier,
                "fair_value_entry_model_path": self.fair_value_entry_model_path
            },
            "multipliers": self.app_state["multipliers"],
            "history": self.history,
            "pending_settlements": self.app_state.get("pending_settlements", []),
            "martingale_state": martingale_state,
            "fair_value_state": fair_value_state,
            "fair_value_giro_probe_state": fair_value_giro_probe_state
        }
        try:
            with open(self.data_file, 'w') as f:
                json.dump(data, f, indent=4)
        except Exception as e:
            self.log(f"Error saving state: {e}")

    def log(self, message: str):
        print(message)
        self.log_total_count = int(getattr(self, "log_total_count", 0) or 0) + 1
        self.logs.append(message)
        limit = int(getattr(self, "log_buffer_limit", 5000) or 5000)
        if len(self.logs) > limit:
            del self.logs[:len(self.logs) - limit]
        try:
            import datetime
            os.makedirs(self.data_dir, exist_ok=True)
            with open(self.runtime_log_file, "a", encoding="utf-8") as f:
                ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
                f.write(f"{ts} {message}\n")
        except Exception:
            pass

    async def start(self, duration_minutes: int = None):
        if self.running:
            return
        self.running = True
        self.initial_sync_complete = False # Reset on start to get fresh snapshot
        
        # Set stop time if duration provided
        if duration_minutes and duration_minutes > 0:
            import time
            self.stop_time = time.time() + (duration_minutes * 60)
            self.log(f"Timer set for {duration_minutes} minutes. Stopping at {time.ctime(self.stop_time)}")
        else:
            self.stop_time = None
            
        self.log(f"Starting Copy Trader Service...")
        self.log(f"Targets: {len(self.target_wallets)} wallets | Dry Run: {self.dry_run}")
        if self.target_wallets:
             self.log(f" -> {', '.join(self.target_wallets)}")
        self.log(f"Strategy: {self.size_mode.upper()} | Value: {self.size_value}")
        if self._is_martingale_maker_mode():
            self.log(
                "[MartingaleMaker] Recorder enabled | "
                f"samples={os.path.join(self.data_dir, 'martingale_maker_orderbook_samples.csv')} | "
                f"entries={os.path.join(self.data_dir, 'martingale_maker_entry_events.csv')}"
            )
            self.log(
                "[MartingaleMaker] Config | "
                f"wait={self.martingale_maker_wait_seconds:.1f}s | "
                f"offset={self.martingale_maker_price_offset:.2f} | "
                f"sample={self.martingale_maker_sample_seconds:.1f}s | "
                f"record={self.martingale_maker_record_seconds:.1f}s"
            )
        if self._is_fair_value_mode():
            self.log(
                "[FairValue] Recorder enabled | "
                f"signals={os.path.join(self.data_dir, 'fair_value_signals.csv')} | "
                f"entries={os.path.join(self.data_dir, 'fair_value_entries.csv')} | "
                f"maker={os.path.join(self.data_dir, 'fair_value_maker_events.csv')} | "
                f"outcomes={os.path.join(self.data_dir, 'fair_value_outcomes.csv')} | "
                f"giro_probe_entries={os.path.join(self.data_dir, 'fair_value_giro_probe_entries.csv')} | "
                f"giro_probe_outcomes={os.path.join(self.data_dir, 'fair_value_giro_probe_outcomes.csv')}"
            )
            self.log(
                "[FairValue] Config | "
                f"stake=${self.fair_value_stake_usd:.2f} | "
                f"taker_edge={self.fair_value_min_taker_edge * 100:.2f}pp | "
                f"maker_edge={self.fair_value_min_maker_edge * 100:.2f}pp | "
                f"min_ev=${self.fair_value_min_ev_per_usd:.2f}/$ | "
                f"taker_guard={self.fair_value_taker_guard_min_edge * 100:.2f}pp/"
                f"${self.fair_value_taker_guard_min_ev_per_usd:.2f}/$ | "
                f"entry={self.fair_value_entry_start_seconds:.0f}-{self.fair_value_entry_end_seconds:.0f}s | "
                f"core={'on' if self.fair_value_core_enabled else 'off'} "
                f"{'giro' if self.fair_value_core_contrarian_only else 'any'} "
                f"px={self.fair_value_core_min_price:.2f}-{self.fair_value_core_max_price:.2f} "
                f"max={self.fair_value_core_max_entries_per_window} | "
                f"momentum={'on' if self.fair_value_momentum_enabled else 'off'} "
                f"{self.fair_value_momentum_start_seconds:.0f}-{self.fair_value_momentum_end_seconds:.0f}s | "
                f"momentum_ev=${self.fair_value_momentum_min_ev_per_usd:.2f}/$ | "
                f"momentum_max={self.fair_value_momentum_max_entries_per_window} | "
                f"late_cont={'on' if self.fair_value_late_continuation_enabled else 'off'} "
                f"{self.fair_value_late_continuation_start_seconds:.0f}-{self.fair_value_late_continuation_end_seconds:.0f}s "
                f"p={self.fair_value_late_continuation_min_probability:.2f}-{self.fair_value_late_continuation_max_probability:.2f} "
                f"delta>={self.fair_value_late_continuation_min_abs_delta_bps:.1f}bps | "
                f"giro_probe={'on' if self.fair_value_giro_probe_enabled else 'off'} "
                f"{self.fair_value_giro_probe_start_seconds:.0f}-{self.fair_value_giro_probe_end_seconds:.0f}s "
                f"px={self.fair_value_giro_probe_min_price:.2f}-{self.fair_value_giro_probe_max_price:.2f} "
                f"p>={self.fair_value_giro_probe_min_probability:.2f} "
                f"ev>={self.fair_value_giro_probe_min_ev_per_usd:.2f}/$ "
                f"conf>={self.fair_value_giro_probe_min_confidence:.2f} "
                f"max={self.fair_value_giro_probe_max_entries_per_window} | "
                f"shadow_stake={'on' if self.fair_value_shadow_dynamic_stake_enabled else 'off'} "
                f"{self.fair_value_shadow_dynamic_stake_min_multiplier:.2f}-"
                f"{self.fair_value_shadow_dynamic_stake_max_multiplier:.2f}x | "
                f"sample={self.fair_value_sample_seconds:.1f}s | "
                f"live_orders={bool(self.fair_value_live_trading_enabled)}"
            )
            if bool(getattr(self, "fair_value_entry_model_enabled", True)):
                self.log(
                    "[FairValueModel] Guard enabled | "
                    f"core_p={self.fair_value_core_entry_model_min_win_prob:.2f}-{self.fair_value_core_entry_model_max_win_prob:.2f} | "
                    f"momentum_min_p={self.fair_value_momentum_entry_model_min_win_prob:.2f} | "
                    f"late_min_p={self.fair_value_late_continuation_entry_model_min_win_prob:.2f}"
                )
            if bool(getattr(self, "fair_value_giro_probe_model_enabled", True)):
                self.log(
                    "[CheapReversalModel] Giro probe model enabled | "
                    f"path={getattr(self, 'fair_value_giro_probe_model_path', '')}"
                )
            if bool(getattr(self, "fair_value_supervisor_enabled", True)):
                self.log(
                    "[Supervisor] Recommendation-only mode enabled | "
                    f"interval={float(getattr(self, 'fair_value_supervisor_interval_seconds', 7200.0)) / 3600.0:.1f}h | "
                    "no auto changes, dry-run only."
                )
            if bool(getattr(self, "fair_value_entry_model_auto_retrain_enabled", True)):
                self.log(
                    "[FairValueModel] Auto retrain enabled | "
                    f"interval={float(getattr(self, 'fair_value_entry_model_auto_retrain_interval_seconds', 7200.0)) / 3600.0:.1f}h | "
                    "promotes only after holdout validation and only in dry run."
                )
        self._task = asyncio.create_task(self._monitor_loop())

    async def stop(self):
        if not self.running:
            return
        self.running = False
        self.log("Stopping Copy Trader Service...")
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None

    async def _monitor_loop(self):
        server_params = StdioServerParameters(
            command=PYTHON_EXE,
            args=[SERVER_SCRIPT],
            env=os.environ.copy()
        )

        try:
            async with stdio_client(server_params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    
                    # Verify tools
                    tools = await session.list_tools()
                    self.log(f"Connected to MCP Server. Tools: {[t.name for t in tools.tools]}")
                    if self._is_martingale_mode():
                        self._ensure_chainlink_price_feed()
                    
                    while self.running:
                        # Check timer
                        if self.stop_time:
                            import time
                            if time.time() > self.stop_time:
                                self.log("Timer expired. Stopping Copy Trader Service...")
                                self.running = False
                                break
                        
                        import time
                        now_ts = time.time()
                        has_pending_settlements = bool(self.app_state.get("pending_settlements"))
                        should_reconcile = (
                            self._last_reconcile_ts is None
                            or (now_ts - self._last_reconcile_ts) >= 60
                            or (self._is_martingale_mode() and has_pending_settlements)
                        )
                        if should_reconcile:
                            await self.reconcile_pending_settlements(session)
                            self._last_reconcile_ts = now_ts
                        
                        await self.tick(session)
                        if self._is_fair_value_mode():
                            await self._fair_value_supervisor_if_due(now_ts)
                            await self._fair_value_entry_model_retrain_if_due(now_ts)
                        if self._last_heartbeat_ts is None or (now_ts - self._last_heartbeat_ts) >= 60:
                            self.log(
                                f"[Heartbeat] running=True | strategy={self.strategy_mode} | "
                                f"poll={self.poll_interval}s | "
                                f"log_events={getattr(self, 'log_total_count', len(self.logs))} | "
                                f"log_buffer={len(self.logs)}/{getattr(self, 'log_buffer_limit', 5000)}"
                            )
                            self._last_heartbeat_ts = now_ts
                        tick_elapsed = time.time() - now_ts
                        sleep_for = max(0.05, float(self.poll_interval) - tick_elapsed)
                        if self._is_martingale_mode() or self._is_fair_value_mode():
                            now_after_tick = time.time()
                            seconds_to_boundary = 300.0 - (now_after_tick % 300.0)
                            if self._is_fair_value_mode():
                                sleep_for = min(
                                    sleep_for,
                                    max(float(getattr(self, "fair_value_sample_seconds", 1.0)), 0.1),
                                )
                            if seconds_to_boundary <= 2.0 or seconds_to_boundary >= 299.0:
                                sleep_for = min(sleep_for, 0.10)
                            elif seconds_to_boundary <= 10.0:
                                sleep_for = min(sleep_for, 0.25)
                        await asyncio.sleep(sleep_for)
        except Exception as e:
            self.log(f"Critical Error in Monitor Loop: {e}")
            self.running = False
        finally:
            await self._stop_chainlink_price_feed()
            await self._drain_background_tasks()
            await self._close_http_session()

    async def tick(self, session: ClientSession):
        try:
            # Branch based on Strategy Mode
            if self._is_martingale_mode():
                await self.run_martingale_strategy(session)
                return
            elif self._is_fair_value_mode():
                await self.run_fair_value_strategy(session)
                return
            elif self.strategy_mode == 'winning':
                await self.run_winning_strategy(session)
                # Auto-close is handled here for winning strategy specifically?
                # Actually, check_auto_close might need to differ or just run generally
                if self.winning_strategy_enabled: # or always if in winning mode
                     await self.check_auto_close(session)
                return

            # --- COPY TRADING MODE (Default) ---
            
            # Iterate through ALL target wallets
            for target_wallet in self.target_wallets:
                # 1. Fetch Target Positions
                positions_result = await session.call_tool("get_wallet_positions", arguments={"address": target_wallet})
                
                # Parse Response
                current_positions = {}
                import json
                import ast
                
                content = positions_result.content[0].text
                data = None
                
                try:
                    # Try JSON first (standard)
                    data = json.loads(content) if isinstance(content, str) else content
                except:
                    try:
                        # Fallback to literal_eval (if Python string rep)
                        data = ast.literal_eval(content) if isinstance(content, str) else content
                    except:
                        data = content

                if isinstance(data, list):
                    for pos in data:
                        # Data API format usually: { "asset": "0x...", "size": "100", ... }
                        asset_id = pos.get("asset")
                        size = float(pos.get("size", 0))
                        price = float(pos.get("curPrice", 0.5)) # Fallback to 0.5 if unavailable
                        if asset_id and size > 0:
                            current_positions[asset_id] = size
                            # Update global price cache
                            self.app_state["prices"][asset_id] = price
                    
                    # 2. Logic Engine (Per Wallet)
                    # Initialize dict for this wallet if not exists
                    if target_wallet not in self.app_state["positions"]:
                         self.app_state["positions"][target_wallet] = {}

                    if not self.initial_sync_complete:
                        # We just store state on first run, effectively "syncing"
                        self.app_state["positions"][target_wallet] = current_positions
                        self.log(f"[Sync] {target_wallet[:6]}... : Tracking {len(current_positions)} positions.")
                    else:
                        await self.diff_and_execute(session, target_wallet, current_positions)
                        
                else:
                    self.log(f"[Tick] Invalid data format received for {target_wallet[:6]}...")
            
            # After processing all wallets
            self.initial_sync_complete = True
            
            # 3. Auto-Close Logic (Check existing positions)
            # Only if explicitly enabled as a mix-in or legacy toggle
            if self.winning_strategy_enabled:
                 await self.check_auto_close(session)

        except Exception as e:
            # Import traceback to print full stack trace to logs for debugging
            import traceback
            self.log(f"Error in tick: {e}")
            traceback.print_exc()

    async def diff_and_execute(self, session, target_wallet: str, current_positions: Dict[str, float]):
        previous_positions = self.app_state["positions"].get(target_wallet, {})
        
        # Check for changes
        all_assets = set(current_positions.keys()) | set(previous_positions.keys())
        
        for asset_id in all_assets:
            new_size = current_positions.get(asset_id, 0.0)
            old_size = previous_positions.get(asset_id, 0.0)
            
            if new_size != old_size:
                delta = new_size - old_size
                action = "BUY" if delta > 0 else "SELL"
                
                # Log the detection
                self.log(f"[Signal] {target_wallet[:6]}... {action}: Asset {asset_id[:6]}... | Change: {delta:+.2f}")
                
                # Winning Strategy Filter
                execute_trade = True
                if self.winning_strategy_enabled and action == "BUY":
                    execute_trade = await self.check_winning_condition(asset_id, self.app_state["prices"].get(asset_id, 0))
                
                if execute_trade:
                    # Execute Trade
                    await self.execute_trade(session, asset_id, delta, action)

        # Update State for this wallet
        self.app_state["positions"][target_wallet] = current_positions

    async def execute_trade(self, session, asset_id: str, target_delta: float, action: str):
        # 1. Calculate My Size
        trade_size = 0.0
        
        # Get multiplier for this asset (used in Percentage mode, or legacy Fixed tracking)
        multiplier = self.app_state["multipliers"].get(asset_id)
        
        if self.size_mode == 'percentage':
            # Percentage mode uses the fixed config value as a universal multiplier
            multiplier = self.size_value
            trade_size = abs(target_delta) * multiplier
            self.log(f"   -> Strategy: Percentage ({self.size_value}x). Order Size: {trade_size:.2f}")
            
        elif self.size_mode == 'fixed':
            # NEW LOGIC: Strict USD Amount for Buys, Sell Everything for Sells
            
            if action == "BUY":
                # Always buy exactly 'size_value' USD worth
                price = self.app_state["prices"].get(asset_id, 0.5) 
                if price <= 0: price = 0.5 # Safety
                
                # Size (Shares) = USD / Price
                trade_size = self.size_value / price
                self.log(f"   -> Strategy: Fixed USD (${self.size_value}). Price: ${price:.2f} -> Size: {trade_size:.2f} shares")
                
            elif action == "SELL":
                # Sell Everything: We need to know how much we hold
                if not self.my_address:
                    self.log("   -> Strategy: Fixed (Sell). Cannot sell all because 'my_address' is unknown.")
                    return

                try:
                    # Fetch MY positions to find current holding
                    my_pos_result = await session.call_tool("get_wallet_positions", arguments={"address": self.my_address})
                    import json
                    import ast
                    content = my_pos_result.content[0].text
                    # Parse...
                    my_data = None
                    try: my_data = json.loads(content) if isinstance(content, str) else content
                    except: 
                        try: my_data = ast.literal_eval(content) if isinstance(content, str) else content
                        except: my_data = content
                    
                    my_holding = 0.0
                    if isinstance(my_data, list):
                        for pos in my_data:
                            if pos.get("asset") == asset_id:
                                my_holding = float(pos.get("size", 0))
                                break
                    
                    if my_holding > 0:
                        trade_size = my_holding
                        # Floor to 2 decimals to avoid rounding up beyond actual balance
                        # e.g. 11.956 -> 11.95 (Safe), not 11.96 (Error)
                        trade_size = math.floor(trade_size * 100) / 100
                        self.log(f"   -> Strategy: Fixed (Sell). Selling entire position: {trade_size:.2f} shares")
                    else:
                        self.log(f"   -> Strategy: Fixed (Sell). We hold 0 shares. Nothing to sell.")
                        trade_size = 0.0
                except Exception as e:
                     self.log(f"   -> Strategy: Fixed (Sell). Error fetching my positions: {e}")
                     trade_size = 0.0

        # Round trade size to 2 decimals for Polymarket
        # For BUYs, standard round is fine. For SELLs, we already floored above.
        if action == "BUY":
            trade_size = round(trade_size, 2)
        elif action == "SELL" and self.size_mode != 'fixed':
             # Legacy percentage sell logic still needs rounding
             trade_size = round(trade_size, 2)

        if trade_size <= 0:
            self.log("   -> Calculated size is 0. Skipping.")
            return

        # 2. Place Order
        if self.dry_run:
            self.log(f"   [DRY RUN] Would {action} {trade_size:.2f} of {asset_id}")
        else:
            self.log(f"   [EXECUTE] Placing {action} Order: {trade_size:.2f} shares...")
            # We need 'side' and 'price'.
            # For Copy Trading, usually we want to cross the spread (Market Order) or Limit at safe price.
            # CLOB only supports Limit, and strict validation (e.g. max 0.99).
            # BUY -> Price 0.99 (Safe "Market Buy" to fill against current Asks)
            # SELL -> Price 0.01 (Safe "Market Sell" to fill against current Bids)
            limit_price = 0.99 if action == "BUY" else 0.01
            
            result = await session.call_tool("place_order", arguments={
                "market_slug": "na", # not needed for token_id based order
                "side": action,
                "size": trade_size,
                "price": limit_price,
                "token_id": asset_id
            })
            self.log(f"   -> Order Result: {str(result.content)[:100]}")

    async def check_winning_condition(self, asset_id: str, current_price: float) -> bool:
        market_data = await self.get_market_data(asset_id)
        if not market_data:
            self.log(f"[Filter] No market data for {asset_id}. Skipping.")
            return False
            
        if not market_data['is_5m']:
            # If not 5m, decide if we skip or allow. Plan says "only 5m".
            # For safety, skipping non-5m if strategy enabled.
            self.log(f"[Filter] Asset {asset_id} is not a 5-minute market. Skipping.")
            return False
            
        import datetime
        now = datetime.datetime.now(datetime.timezone.utc)
        start_time = market_data['start_time']
        
        elapsed = (now - start_time).total_seconds() / 60.0 # minutes
        
        # Condition 1: Time Window (Entry Time <= Elapsed < Exit Time)
        # We only want to enter the CURRENT open market
        if elapsed < self.winning_entry_time:
             self.log(f"[Filter] Too early. Elapsed: {elapsed:.2f}m < {self.winning_entry_time}m. Skipping.")
             return False
        
        if elapsed >= self.winning_exit_time:
             self.log(f"[Filter] Too late (or old market). Elapsed: {elapsed:.2f}m >= {self.winning_exit_time}m. Skipping.")
             return False
             
        # Condition 2: Price > 0.75 (default)
        if current_price <= self.winning_price_threshold:
             self.log(f"[Filter] Price too low. {current_price} <= {self.winning_price_threshold}. Skipping.")
             return False
             
        self.log(f"[Filter] MATCH! Elapsed: {elapsed:.2f}m, Price: {current_price}. Executing.")
        return True

    async def check_auto_close(self, session):
        # Fetch our own positions to close them
        proxy_address = os.getenv("POLYMARKET_PROXY_ADDRESS")
        if not proxy_address:
            self.log("[Auto-Close] Proxy address not found in env. Cannot auto-close.")
            return

        try:
            positions_result = await session.call_tool("get_wallet_positions", arguments={"address": proxy_address})
            import json
            import ast
            
            content = positions_result.content[0].text
            data = None
            try:
                data = json.loads(content) if isinstance(content, str) else content
            except:
                try:
                    data = ast.literal_eval(content) if isinstance(content, str) else content
                except:
                    data = content
            
            import datetime
            now = datetime.datetime.now(datetime.timezone.utc)
            
            if isinstance(data, list):
                for pos in data:
                    asset_id = pos.get("asset")
                    size = float(pos.get("size", 0))
                    if asset_id and size > 0:
                        # Check if this asset needs closing
                        market_data = await self.get_market_data(asset_id)
                        if market_data and market_data['is_5m']:
                             elapsed = (now - market_data['start_time']).total_seconds() / 60.0
                             
                             if elapsed >= self.winning_exit_time:
                                 self.log(f"[Auto-Close] Time limit reached ({elapsed:.2f}m >= {self.winning_exit_time}m). Closing {asset_id[:6]}... Size: {size}")
                                 # Execute Sell
                                 # We pass -size for delta, and "SELL" action
                                 await self.execute_trade(session, asset_id, -size, "SELL")
        except Exception as e:
            self.log(f"[Auto-Close] Error checking positions: {e}")
                     
    async def find_active_5m_btc_market(self):
        """Dynamically finds the currently active 5-minute BTC market."""
        import aiohttp
        import datetime
        timeout = aiohttp.ClientTimeout(total=6)
        async with aiohttp.ClientSession(timeout=timeout, headers=self.http_headers) as http_session:
            url = "https://gamma-api.polymarket.com/events?limit=500&active=true&closed=false"
            for attempt in range(1, 4):
                try:
                    async with http_session.get(url, ssl=False) as response:
                        if response.status == 200:
                            data = await response.json()
                            for event in data:
                                slug = event.get('slug', '').lower()
                                title = event.get('title', '').lower()
                                
                                is_btc = 'btc' in slug or 'bitcoin' in slug or 'btc' in title or 'bitcoin' in title
                                is_5m = '5m' in slug or '5 min' in title or '5-minute' in title
                                
                                if is_btc and is_5m:
                                    start_str = event.get('startTime') or event.get('startDate')
                                    if start_str:
                                        self._active_events_failures = 0
                                        start_time = datetime.datetime.fromisoformat(start_str.replace("Z", "+00:00"))
                                        return {'start_time': start_time, 'markets': event.get('markets', []), 'slug': event.get('slug')}
                except Exception as e:
                    if self.debug_logs:
                        self.log(f"[Warn] Active events fetch failed (attempt {attempt}/3): {e}")
            self._active_events_failures += 1
            import time
            now_ts = time.time()
            if self._last_active_events_warn_ts is None or (now_ts - self._last_active_events_warn_ts) >= 60:
                self.log("[Warn] Active events fetch failed 3/3. Falling back to deterministic slug.")
                self._last_active_events_warn_ts = now_ts
        return None

    async def run_winning_strategy(self, session: ClientSession):
        """
        Independent strategy:
        1. Find active 5m BTC market.
        2. Check Entry Window & Price Condition.
        3. Execute Buy if conditions met.
        """
        import time
        import datetime
        import aiohttp
        
        # 1. Identify Current Market Slug
        now_ts = time.time()
        # Round down to nearest 300s (5 min)
        window_start = int(now_ts // 300) * 300
        guessed_slug = f"btc-updown-5m-{window_start}"
        
        # Check if already traded this window in logic memory or app state
        if "winning_trades" not in self.app_state:
            self.app_state["winning_trades"] = {}

        # 2. Fetch Market Details dynamically
        market_data = await self.find_active_5m_btc_market()
        if not market_data:
            market_data = await self.get_market_by_slug(guessed_slug)
            if market_data:
                market_data['slug'] = guessed_slug

        if not market_data:
            current_window = int(now_ts // 300) * 300
            if getattr(self, "_last_logged_no_market_win", None) != current_window:
                self.log(f"[Info] No active 5m BTC market found on Polymarket right now. Waiting...")
                self._last_logged_no_market_win = current_window
            return
            
        slug = market_data['slug']
        
        # 3. Check Time Window
        now = datetime.datetime.now(datetime.timezone.utc)
        start_time = market_data['start_time']
        elapsed = (now - start_time).total_seconds() / 60.0
        
        if elapsed < self.winning_entry_time:
             # Too early
             return
        if elapsed >= self.winning_exit_time:
             # Too late
             return
             
        # Check if already traded (Double Check)
        if self.app_state["winning_trades"].get(slug):
            return

        # 4. Check Price Condition
        markets = market_data.get('markets', [])
        
        for m in markets:
            clob_token_ids = m.get('clobTokenIds', [])
            
            if isinstance(clob_token_ids, str):
                import json
                try:
                    clob_token_ids = json.loads(clob_token_ids)
                except:
                    clob_token_ids = []
            
            if len(clob_token_ids) != 2:
                continue
                
            price_up = await self.fetch_price(clob_token_ids[0])
            price_down = await self.fetch_price(clob_token_ids[1])
            
            target_token_id = None
            price_found = 0.0
            side_label = ""
            
            if price_up > self.winning_price_threshold:
                target_token_id = clob_token_ids[0]
                price_found = price_up
                side_label = "UP (Yes)"
            elif price_down > self.winning_price_threshold:
                target_token_id = clob_token_ids[1]
                price_found = price_down
                side_label = "DOWN (No)"
                
            if target_token_id:
                self.log(f"[Winning Strat] MATCH! {slug} | {side_label} Price {price_found} > {self.winning_price_threshold}")
                
                # 5. Sizing
                size_to_buy = self.winning_size_value
                if self.winning_size_mode == 'percent':
                    try:
                        bal_res = await session.call_tool("get_balance")
                        bal_str = bal_res.content[0].text
                        import json
                        # Try to parse float directly or dict
                        try:
                            balance = float(bal_str)
                        except:
                            try:
                                bal_data = json.loads(bal_str.replace("'", '"'))
                                balance = float(bal_data.get('balance', 0))
                            except:
                                self.log(f"[Error] Could not parse balance: {bal_str}")
                                return
                        
                        size_to_buy = balance * (self.winning_size_value / 100.0)
                    except Exception as e:
                        self.log(f"[Error] Failed sizing: {e}. Defaulting to min.")
                        size_to_buy = 1.0

                shares_to_buy = self._usd_to_shares(size_to_buy, price_found)
                self.log(
                    f"[Winning Strat] BUY {target_token_id[:10]}... "
                    f"usd=${size_to_buy:.2f} | px={price_found:.4f} | shares={shares_to_buy:.2f}"
                )
                
                if not self.dry_run:
                    try:
                        order_res = await session.call_tool("place_order", arguments={
                            "market_slug": slug, 
                            "side": "BUY",
                            "size": shares_to_buy,
                            "price": 0.99, 
                            "token_id": target_token_id
                        })
                        self.log(f"[Winning Strat] Implemented: {order_res.content[0].text[:50]}...")
                        self.app_state["winning_trades"][slug] = True
                    except Exception as e:
                         self.log(f"[Error] Execution failed: {e}")
                else:
                    self.log("[Winning Strat] Dry Run - Trade Simulated")
                    self.app_state["winning_trades"][slug] = True

    async def run_martingale_strategy(self, session: ClientSession):
        import datetime
        import random
        
        self._ensure_chainlink_price_feed()
        now_ts = time.time()
        # Round down to nearest 300s (5 min)
        window_start = int(now_ts // 300) * 300
        guessed_slug = f"btc-updown-5m-{window_start}"
        
        # BTC 5m slugs are deterministic. Avoid the slow Gamma active-events scan
        # so entries can happen in the first seconds of the window.
        market_data = await self.get_market_by_slug(guessed_slug, use_cache=True, cache_ttl=300)
        if market_data:
            market_data['slug'] = guessed_slug

        if not market_data:
            current_window = int(now_ts // 300) * 300
            if getattr(self, "_last_logged_no_market_mart", None) != current_window:
                self.log(f"[Info] No active 5m BTC market found on Polymarket right now. Waiting...")
                self._last_logged_no_market_mart = current_window
            return
            
        slug = market_data['slug']
        
        if "martingale_state" not in self.app_state:
            self.app_state["martingale_state"] = self._new_martingale_state()
        else:
            self.app_state["martingale_state"] = self._normalize_martingale_state(
                self.app_state["martingale_state"]
            )
        if "martingale_session_stats" not in self.app_state:
            self.app_state["martingale_session_stats"] = {
                "trades": 0,
                "wins": 0,
                "losses": 0,
                "cum_pnl_usd": 0.0,
                "started_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            }
            
        state = self.app_state["martingale_state"]
        pending_settlements = self.app_state.get("pending_settlements", [])
        non_martingale_pending = [
            item for item in pending_settlements
            if not item.get("is_martingale")
        ]
        if non_martingale_pending:
            if self.debug_logs:
                self.log("[Martingale] Waiting for previous trade settlement before new entry.")
            return
        
        # Settle the previous window as soon as the next one starts, then continue
        # into the current entry path in the same tick.
        if state["active_slug"] and state["active_slug"] != slug and state["entry_done"]:
            previous_slug = state["active_slug"]
            previous_start = self._start_time_from_btc_slug(previous_slug)
            if previous_start and now_ts >= previous_start.timestamp() + 300:
                settled = await self._settle_active_martingale_trade(
                    state=state,
                    slug=previous_slug,
                    start_time=previous_start,
                    now_ts=now_ts,
                )
                if not settled:
                    if getattr(self, "_last_unresolved_martingale_slug", None) != previous_slug:
                        self.log(
                            f"[Martingale] Previous window {previous_slug} has no usable Chainlink "
                            "boundary result or official outcome yet. Holding entry without guessing."
                        )
                        self._last_unresolved_martingale_slug = previous_slug
                    return
                self._last_unresolved_martingale_slug = None
                now_ts = time.time()
            else:
                return

        if state["active_slug"] != slug:
            if not state.get("entry_done") and int(state.get("streak", 0)) == 0:
                state["direction"] = None
            state["active_slug"] = slug
            state["entry_done"] = False
            state["target_token_id"] = None
            state["entry_retry_after"] = 0.0
            state["entry_attempts"] = 0
            state["skip_logged"] = False
            
        # Prevent re-running in a closed window
        if state["active_slug"] == f"closed_{slug}":
            return
            
        start_time = market_data['start_time']
        elapsed = now_ts - start_time.timestamp()
        if elapsed >= 285:
            await self._prepare_martingale_entry_plan(
                state=state,
                slug=f"btc-updown-5m-{window_start + 300}",
                now_ts=now_ts,
            )
        
        if self.debug_logs:
            self.log(f"[Debug] Market found: {slug} | elapsed={elapsed:.1f}s | entry_done={state['entry_done']}")

        recovery_mode = self._martingale_in_recovery(state)
        entry_end_seconds = (
            self.martingale_recovery_entry_end_seconds
            if recovery_mode
            else self.martingale_entry_end_seconds
        )
        min_entry_price, max_entry_price = self._entry_price_bounds(state, elapsed)

        # ENTRY LOGIC -> new cycles are early-only; recovery cycles keep trying
        # later so accumulated losses are not silently discarded.
        if self.martingale_entry_start_seconds <= elapsed <= entry_end_seconds and not state["entry_done"]:
            retry_after = float(state.get("entry_retry_after", 0.0) or 0.0)
            if now_ts < retry_after:
                return

            prepared_plan = self._martingale_entry_plans.get(slug)
            clob_token_ids = (
                prepared_plan.get("token_ids", [])
                if prepared_plan
                else self._extract_clob_token_ids(market_data)
            )
            if len(clob_token_ids) != 2:
                return
            self._ensure_maker_orderbook_recorder(slug, start_time.timestamp(), clob_token_ids)
            
            prices = {}
            if state["direction"] in ("Yes", "No"):
                token_index = 0 if state["direction"] == "Yes" else 1
                target_token_id = clob_token_ids[token_index]
                price_value = await self.fetch_buy_price(target_token_id)
                prices[state["direction"]] = float(price_value)
            else:
                price_results = await asyncio.gather(
                    self.fetch_buy_price(clob_token_ids[0]),
                    self.fetch_buy_price(clob_token_ids[1]),
                    return_exceptions=True,
                )
                for label, value in zip(("Yes", "No"), price_results):
                    prices[label] = 0.0 if isinstance(value, Exception) else float(value)

            blind_maker = False
            # Pick once per cycle/window; on loss streaks, keep the same direction.
            if not state["direction"]:
                eligible_directions = [
                    label for label, price in prices.items()
                    if price > 0 and self._entry_price_allowed(price, min_entry_price, max_entry_price)
                ]
                if not eligible_directions:
                    no_taker_prices = all(float(price or 0.0) <= 0 for price in prices.values())
                    if no_taker_prices and self._can_try_blind_maker(elapsed, entry_end_seconds):
                        state["direction"] = random.choice(["Yes", "No"])
                        state["direction_tentative"] = True
                        blind_maker = True
                        if int(state.get("streak", 0)) == 0:
                            state["loss_bank"] = 0.0
                        self.log(
                            f"[MartingaleMaker] No taker prices for {slug} "
                            f"(Yes={prices.get('Yes', 0):.4f}, No={prices.get('No', 0):.4f}). "
                            f"Trying blind maker at ${self._blind_maker_price(min_entry_price, max_entry_price):.2f}."
                        )
                    else:
                        if not state.get("skip_logged"):
                            self.log(
                                f"[Martingale] Skipping {slug}: entry prices outside "
                                f"{min_entry_price:.2f}-{max_entry_price:.2f} "
                                f"(Yes={prices.get('Yes', 0):.4f}, No={prices.get('No', 0):.4f}). Retrying until cutoff."
                            )
                            state["skip_logged"] = True
                            self.save_state()
                        return
                else:
                    state["direction"] = random.choice(eligible_directions)
                    state["direction_tentative"] = False
                    if int(state.get("streak", 0)) == 0:
                        state["loss_bank"] = 0.0
                
            if not state["direction"]:
                return
            self._ensure_maker_orderbook_recorder(
                slug,
                start_time.timestamp(),
                clob_token_ids,
                target_direction=state.get("direction"),
            )

            # Polymarket tokens: UP/Yes is index 0, DOWN/No is index 1 depending on market
            # Typically for up/down markets, 0 is UP (Yes), 1 is DOWN (No). 
            # We assume UP is 0, DOWN is 1. If not, we still bet on the same token.
            token_index = 0 if state["direction"] == "Yes" else 1
            target_token_id = clob_token_ids[token_index]
            state["target_token_id"] = target_token_id
            
            entry_price = prices.get(state["direction"], 0.0)
            if entry_price <= 0:
                if self._can_try_blind_maker(elapsed, entry_end_seconds):
                    blind_maker = True
                    entry_price = self._blind_maker_price(min_entry_price, max_entry_price)
                    self.log(
                        f"[MartingaleMaker] {state['direction']} taker price unavailable for {slug}; "
                        f"trying blind maker at ${entry_price:.2f}."
                    )
                else:
                    self.log(f"[Martingale] Skipping entry for {slug}: invalid entry price {entry_price}")
                    return
            if not self._entry_price_allowed(entry_price, min_entry_price, max_entry_price):
                if not state.get("skip_logged"):
                    mode = "recovery" if recovery_mode else "new-cycle"
                    self.log(
                        f"[Martingale] Skipping {slug}: {state['direction']} price {entry_price:.4f} outside "
                        f"{min_entry_price:.2f}-{max_entry_price:.2f} ({mode}). Retrying until cutoff."
                    )
                    state["skip_logged"] = True
                    self.save_state()
                return

            raw_usd_to_buy = self._martingale_stake_for_price(state, entry_price)
            usd_to_buy = round(max(raw_usd_to_buy, self.polymarket_min_market_buy_usd), 2)
            estimated_shares = round(usd_to_buy / max(entry_price, 0.01), 4)
            estimated_fee = self._martingale_fee_for_stake(usd_to_buy, entry_price)
            order_type = str(getattr(self, "martingale_order_type", "FOK") or "FOK").upper()
            if order_type not in ("FAK", "FOK"):
                order_type = "FOK"
            
            self.log(
                f"[Martingale] ENTRY | slug={slug} | direction={state['direction']} | "
                f"usd=${usd_to_buy:.2f} | px={entry_price:.4f} | shares~={estimated_shares:.4f} | "
                f"fee~=${estimated_fee:.2f} | order={order_type} | "
                f"loss_bank=${float(state.get('loss_bank', 0.0)):.2f} | target=${self.martingale_initial_amount:.2f} | "
                f"min=${self.polymarket_min_market_buy_usd:.2f} | streak={state['streak']} | elapsed={elapsed:.2f}s"
            )

            if not self.dry_run:
                guard_ok, balance, _, balance_floor, projected_if_loss = await self._martingale_balance_guard(
                    session=session,
                    state=state,
                    stake_usd=usd_to_buy,
                    entry_price=entry_price,
                )
                if not guard_ok:
                    self._abandon_martingale_recovery(state, "balance guard blocked next stake")
                    state["entry_retry_after"] = start_time.timestamp() + entry_end_seconds + 1
                    self.save_state()
                    return
                if balance is not None and balance_floor is not None and projected_if_loss is not None:
                    self.log(
                        f"[Martingale] Balance guard OK | balance=${balance:.2f} | "
                        f"projected_if_loss=${projected_if_loss:.2f} | floor=${balance_floor:.2f}"
                    )

            maker_result = await self._try_maker_entry_probe(
                session=session,
                slug=slug,
                start_time=start_time,
                elapsed=elapsed,
                entry_end_seconds=entry_end_seconds,
                state=state,
                token_ids=clob_token_ids,
                target_token_id=target_token_id,
                entry_price=entry_price,
                usd_to_buy=usd_to_buy,
                min_entry_price=min_entry_price,
                max_entry_price=max_entry_price,
                blind=blind_maker,
            )

            if blind_maker and not maker_result.get("filled"):
                fallback_entry_price = maker_result.get("fallback_entry_price")
                if fallback_entry_price is None:
                    try:
                        fresh_price = float(await self.fetch_buy_price(target_token_id))
                    except Exception:
                        fresh_price = 0.0
                    if fresh_price > 0 and self._entry_price_allowed(fresh_price, min_entry_price, max_entry_price):
                        fallback_entry_price = round(fresh_price, 4)

                if fallback_entry_price is None:
                    self.log(
                        f"[MartingaleMaker] Blind maker for {slug} did not fill and taker price "
                        "is still unavailable. Retrying before cutoff."
                    )
                    if state.get("direction_tentative"):
                        state["direction"] = None
                        state["target_token_id"] = None
                        state["direction_tentative"] = False
                    state["entry_retry_after"] = now_ts + max(
                        float(getattr(self, "martingale_maker_sample_seconds", 0.5) or 0.5),
                        0.5,
                    )
                    self.save_state()
                    return

                entry_price = float(fallback_entry_price)
                raw_usd_to_buy = self._martingale_stake_for_price(state, entry_price)
                usd_to_buy = round(max(raw_usd_to_buy, self.polymarket_min_market_buy_usd), 2)
                estimated_shares = round(usd_to_buy / max(entry_price, 0.01), 4)
                estimated_fee = self._martingale_fee_for_stake(usd_to_buy, entry_price)
                self.log(
                    f"[MartingaleMaker] Blind maker fallback | slug={slug} | "
                    f"taker_px={entry_price:.4f} | usd=${usd_to_buy:.2f} | "
                    f"shares~={estimated_shares:.4f} | fee~=${estimated_fee:.2f}"
                )

                if not self.dry_run:
                    guard_ok, balance, _, balance_floor, projected_if_loss = await self._martingale_balance_guard(
                        session=session,
                        state=state,
                        stake_usd=usd_to_buy,
                        entry_price=entry_price,
                    )
                    if not guard_ok:
                        self._abandon_martingale_recovery(state, "balance guard blocked blind fallback stake")
                        state["entry_retry_after"] = start_time.timestamp() + entry_end_seconds + 1
                        self.save_state()
                        return
                    if balance is not None and balance_floor is not None and projected_if_loss is not None:
                        self.log(
                            f"[Martingale] Balance guard OK | balance=${balance:.2f} | "
                            f"projected_if_loss=${projected_if_loss:.2f} | floor=${balance_floor:.2f}"
                        )

            entry_fee_usd = None
            if maker_result.get("filled") and not maker_result.get("fallback"):
                filled_usd = float(maker_result["filled_usd"])
                filled_shares = float(maker_result["filled_shares"])
                avg_entry_price = float(maker_result["avg_entry_price"])
                entry_fee_usd = float(maker_result.get("entry_fee_usd") or 0.0)
                state["entry_route"] = maker_result.get("route", "maker_simulated")
                state["maker_price"] = maker_result.get("maker_price")
                state["maker_waited_seconds"] = maker_result.get("maker_waited_seconds", 0.0)
                state["maker_touch_signal"] = bool(maker_result.get("maker_touch_signal", False))
                state["direction_tentative"] = False
                self.log(
                    f"[MartingaleMaker] ENTRY filled as maker | usd=${filled_usd:.2f} | "
                    f"px={avg_entry_price:.4f} | shares={filled_shares:.4f} | fee=${entry_fee_usd:.2f}"
                )
            elif not self.dry_run:
                try:
                    maker_filled_usd = float(maker_result.get("filled_usd") or 0.0)
                    maker_filled_shares = float(maker_result.get("filled_shares") or 0.0)
                    maker_avg_price = float(maker_result.get("avg_entry_price") or maker_result.get("maker_price") or entry_price)
                    remaining_usd = (
                        float(maker_result.get("remaining_usd"))
                        if maker_result.get("remaining_usd") is not None
                        else float(usd_to_buy)
                    )
                    if maker_filled_usd > 0:
                        self.log(
                            f"[MartingaleMaker] Maker partial fill will use taker fallback for "
                            f"remaining=${remaining_usd:.2f}."
                        )
                    taker_usd_to_buy = remaining_usd
                    if maker_filled_usd > 0 and 0 < taker_usd_to_buy < self.polymarket_min_market_buy_usd:
                        taker_usd_to_buy = self.polymarket_min_market_buy_usd
                    taker_usd_to_buy = round(max(taker_usd_to_buy, self.polymarket_min_market_buy_usd), 2)
                    order_res = await session.call_tool("place_market_order", arguments={
                        "market_slug": slug, 
                        "side": "BUY",
                        "amount": taker_usd_to_buy,
                        "token_id": target_token_id,
                        "order_type": order_type,
                        "defer_exec": False,
                    })
                    order_text = order_res.content[0].text if order_res and order_res.content else ""
                    order_ok, order_error, order_data = self._parse_order_response(order_text, allow_live=False)
                    if not order_ok:
                        self.log(f"[Error] Martingale order rejected: {order_error[:220]}")
                        if maker_filled_usd > 0:
                            self.log(
                                "[MartingaleMaker] Taker fallback failed after maker partial fill. "
                                "Tracking the maker fill as the open position."
                            )
                            filled_usd = maker_filled_usd
                            filled_shares = maker_filled_shares
                            avg_entry_price = maker_avg_price
                            entry_fee_usd = 0.0
                            state["entry_route"] = "maker_partial_unfilled_remainder"
                            state["maker_price"] = maker_result.get("maker_price")
                            state["maker_waited_seconds"] = maker_result.get("maker_waited_seconds", 0.0)
                            state["maker_touch_signal"] = bool(maker_result.get("maker_touch_signal", False))
                            state["direction_tentative"] = False
                            order_ok = True
                        else:
                            state["entry_attempts"] = int(state.get("entry_attempts", 0) or 0) + 1
                            delay = self._martingale_retry_delay(f"{order_error} {order_text}", state["entry_attempts"])
                            state["entry_retry_after"] = now_ts + delay
                            if self._is_permanent_order_error(f"{order_error} {order_text}"):
                                state["entry_retry_after"] = start_time.timestamp() + entry_end_seconds + 1
                                self.log(f"[Martingale] Permanent order format/min-size error. Skipping entry for {slug}.")
                            state["opened_at"] = None
                            self.save_state()
                            return
                    if order_ok and order_data:
                        filled_usd, filled_shares, avg_entry_price = self._order_fill_from_response(
                            order_data,
                            fallback_usd=taker_usd_to_buy,
                            fallback_price=entry_price,
                        )
                        fill_ratio = filled_usd / taker_usd_to_buy if taker_usd_to_buy > 0 else 1.0
                        if fill_ratio < 0.98:
                            self.log(
                                f"[Warn] Martingale partial fill detected: filled ${filled_usd:.2f}/"
                                f"${taker_usd_to_buy:.2f}. Recovery accounting will use actual fill."
                            )
                        self.log(f"[Martingale] Executed Buy: {order_text[:50]}...")
                        taker_fee_usd = self._martingale_fee_for_fill(filled_shares, avg_entry_price)
                        if maker_filled_usd > 0:
                            combined_usd = round(maker_filled_usd + filled_usd, 4)
                            combined_shares = round(maker_filled_shares + filled_shares, 4)
                            avg_entry_price = (
                                round(
                                    ((maker_filled_shares * maker_avg_price) + (filled_shares * avg_entry_price))
                                    / combined_shares,
                                    6,
                                )
                                if combined_shares > 0
                                else avg_entry_price
                            )
                            filled_usd = combined_usd
                            filled_shares = combined_shares
                            entry_fee_usd = taker_fee_usd
                        else:
                            entry_fee_usd = taker_fee_usd
                        state["entry_route"] = (
                            "maker_partial_taker_fallback"
                            if maker_filled_usd > 0
                            else "taker_fallback"
                            if maker_result.get("attempted")
                            else "taker"
                        )
                        state["maker_price"] = maker_result.get("maker_price")
                        state["maker_waited_seconds"] = maker_result.get("maker_waited_seconds", 0.0)
                        state["maker_touch_signal"] = bool(maker_result.get("maker_touch_signal", False))
                        state["direction_tentative"] = False
                except Exception as e:
                      self.log(f"[Error] Martingale Execution failed: {e}")
                      state["entry_attempts"] = int(state.get("entry_attempts", 0) or 0) + 1
                      state["entry_retry_after"] = now_ts + self._martingale_retry_delay(str(e), state["entry_attempts"])
                      state["opened_at"] = None
                      self.save_state()
                      return
            else:
                route = "taker_fallback" if maker_result.get("attempted") else "taker"
                self.log(f"[Martingale] Dry Run - Simulated BUY via {route}")
                filled_usd = usd_to_buy
                filled_shares = estimated_shares
                avg_entry_price = entry_price
                state["entry_route"] = route
                state["maker_price"] = maker_result.get("maker_price")
                state["maker_waited_seconds"] = maker_result.get("maker_waited_seconds", 0.0)
                state["maker_touch_signal"] = bool(maker_result.get("maker_touch_signal", False))
                state["direction_tentative"] = False

            if entry_fee_usd is None:
                entry_fee_usd = (
                    float(maker_result.get("entry_fee_usd"))
                    if maker_result.get("filled") and maker_result.get("entry_fee_usd") is not None
                    else self._martingale_fee_for_fill(filled_shares, avg_entry_price)
                )
            state["entry_price"] = avg_entry_price
            state["current_amount"] = filled_usd
            state["entry_shares"] = filled_shares
            state["entry_fee_usd"] = entry_fee_usd
            state["opened_at"] = datetime.datetime.now(datetime.timezone.utc)
            state["entry_done"] = True
            state["entry_retry_after"] = 0.0
            state["entry_attempts"] = 0
            state["skip_logged"] = False
            self.save_state()
            
        elif elapsed > entry_end_seconds and not state["entry_done"]:
            if not state.get("skip_logged"):
                if recovery_mode:
                    self.log(
                        f"[Martingale] Recovery entry missed for {slug}: no fill before "
                        f"{entry_end_seconds}s. Carrying loss_bank=${float(state.get('loss_bank', 0.0)):.2f} "
                        "and same direction into the next window."
                    )
                    state["entry_retry_after"] = 0.0
                    state["entry_attempts"] = 0
                    state["skip_logged"] = True
                else:
                    self.log(
                        f"[Martingale] Coverage failed for {slug}: no entry before "
                        f"{self.martingale_entry_end_seconds}s. Waiting next window."
                    )
                    self._reset_martingale_cycle_after_gap(state, slug)
                self.save_state()

        # EXIT LOGIC -> auto-settle at the next 5m boundary. Keeping the trade
        # active avoids a pending-settlement detour right before the next entry.
        elif elapsed >= 270 and state["entry_done"]:
            return

    async def _infer_settlement_result(self, slug: str, token_id: str):
        import datetime
        market_data = await self.get_market_by_slug(slug)
        if market_data:
            start_time = market_data.get("start_time")
            market_elapsed = None
            if isinstance(start_time, datetime.datetime):
                market_elapsed = (datetime.datetime.now(datetime.timezone.utc) - start_time).total_seconds()
            markets = market_data.get("markets", [])
            for m in markets:
                clob_token_ids = m.get("clobTokenIds", [])
                if isinstance(clob_token_ids, str):
                    import json
                    try:
                        clob_token_ids = json.loads(clob_token_ids)
                    except Exception:
                        clob_token_ids = []
                if token_id not in clob_token_ids:
                    continue
                idx = clob_token_ids.index(token_id)
                outcome_prices = m.get("outcomePrices", [])
                if isinstance(outcome_prices, str):
                    import json
                    try:
                        outcome_prices = json.loads(outcome_prices)
                    except Exception:
                        outcome_prices = []
                if isinstance(outcome_prices, list) and len(outcome_prices) > idx:
                    try:
                        p = float(outcome_prices[idx])
                        result = self._binary_result_from_price(p)
                        if result is not None:
                            return result, 1.0 if result else 0.0
                    except Exception:
                        pass
        return None, None

    async def reconcile_pending_settlements(self, session: ClientSession):
        pending = self.app_state.get("pending_settlements", [])
        if not pending:
            return
        proxy_address = os.getenv("POLYMARKET_PROXY_ADDRESS")
        try:
            open_assets = set()
            if proxy_address:
                try:
                    positions_result = await session.call_tool("get_wallet_positions", arguments={"address": proxy_address})
                    content = positions_result.content[0].text if positions_result and positions_result.content else "[]"
                    import json
                    import ast
                    try:
                        positions = json.loads(content) if isinstance(content, str) else content
                    except Exception:
                        positions = ast.literal_eval(content) if isinstance(content, str) else []
                except Exception:
                    positions = []
                    if self.debug_logs:
                        self.log("[Warn] Wallet positions unavailable during settlement reconciliation; using market outcome only.")

                if isinstance(positions, list):
                    for p in positions:
                        try:
                            if abs(float(p.get("size", 0.0))) > 0:
                                open_assets.add(str(p.get("asset")))
                        except Exception:
                            continue

            still_pending = []
            state_changed = False
            for item in pending:
                token_id = str(item.get("token_id"))
                is_win, settled_price = await self._infer_settlement_result(item.get("slug"), token_id)
                if is_win is None:
                    still_pending.append(item)
                    continue
                if (
                    token_id in open_assets
                    and settled_price not in (0.0, 1.0)
                    and not item.get("is_martingale")
                ):
                    still_pending.append(item)
                    continue

                shares = float(item.get("entry_shares", 0.0))
                entry_price = float(item.get("entry_price", 0.0))
                payout = round(shares * (1.0 if is_win else 0.0), 4)
                pnl = round(payout - (shares * entry_price), 4)
                try:
                    self._update_trade_row_in_excel(
                        settlement_key=item.get("settlement_key"),
                        updates={
                            "Result": "WIN" if is_win else "LOSS",
                            "Payout USD (estimate)": payout,
                            "PnL USD (estimate)": pnl,
                            "Exit Price (snapshot)": settled_price,
                            "Close Status": "settled",
                            "Close Error": "",
                        },
                    )
                    if item.get("is_martingale"):
                        m_state = self.app_state.get("martingale_state", {})
                        if "loss_bank_before" in item:
                            m_state["loss_bank"] = float(item.get("loss_bank_before") or 0.0)
                        self._apply_martingale_result(
                            m_state,
                            item.get("slug"),
                            bool(is_win),
                            shares,
                            entry_price,
                        )
                        state_changed = True
                except PermissionError:
                    import time
                    now_ts = time.time()
                    if self._last_excel_lock_warn_ts is None or (now_ts - self._last_excel_lock_warn_ts) >= 60:
                        self.log("[Warn] Excel file is open/locked. Settlement updates will retry every 60s.")
                        self._last_excel_lock_warn_ts = now_ts
                    still_pending.append(item)
                except Exception as e:
                    self.log(f"[Warn] Failed to update settlement row: {e}")
                    still_pending.append(item)

            self.app_state["pending_settlements"] = still_pending
            if state_changed or len(still_pending) != len(pending):
                self.save_state()
        except Exception as e:
            if self.debug_logs:
                self.log(f"[Warn] Settlement reconciliation failed: {e}")

    async def get_market_by_slug(self, slug: str, use_cache: bool = False, cache_ttl: float = 60.0):
        import datetime

        if use_cache:
            import time
            cached = self._martingale_market_cache.get(slug)
            if cached and time.time() - float(cached.get("ts", 0.0)) <= cache_ttl:
                return cached.get("data")

        http_session = await self._get_http_session()
        url = f"https://gamma-api.polymarket.com/events?slug={slug}"
        last_exc = None
        try:
            if not self._polymarket_backoff_active(url):
                async with http_session.get(url) as response:
                    if response.status == 200:
                        data = await response.json()
                        self._mark_polymarket_http_success(url)
                    else:
                        data = None
            else:
                data = None
        except Exception as exc:
            last_exc = exc
            data = None
        if not data and last_exc is not None:
            data = await self._polymarket_get_json_fallback(url, timeout=8.0)
        if isinstance(data, list) and len(data) > 0:
            event = data[0]
            start_str = event.get('startTime') or event.get('startDate')
            if start_str:
                start_time = datetime.datetime.fromisoformat(start_str.replace("Z", "+00:00"))
                result = {'start_time': start_time, 'markets': event.get('markets', [])}
                if use_cache:
                    import time
                    self._martingale_market_cache[slug] = {"ts": time.time(), "data": result}
                    if len(self._martingale_market_cache) > 12:
                        oldest = sorted(
                            self._martingale_market_cache,
                            key=lambda key: self._martingale_market_cache[key]["ts"],
                        )[:4]
                        for key in oldest:
                            self._martingale_market_cache.pop(key, None)
                return result
        return None

    async def fetch_price(self, asset_id: str) -> float:
        import aiohttp

        http_session = await self._get_http_session()
        url = f"https://clob.polymarket.com/price?token_id={asset_id}&side=buy"
        data = None
        last_exc = None
        try:
            if not self._polymarket_backoff_active(url):
                async with http_session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as response:
                    if response.status == 200:
                        data = await response.json()
                        self._mark_polymarket_http_success(url)
        except Exception as exc:
            last_exc = exc
        if not data and last_exc is not None:
            data = await self._polymarket_get_json_fallback(url, timeout=6.0)
        if isinstance(data, dict):
            try:
                return float(data.get('price', 0))
            except Exception:
                return 0.0
        return 0.0

    async def fetch_buy_price(self, asset_id: str) -> float:
        try:
            data = await self.fetch_order_book(asset_id)
            asks = data.get("asks", []) if isinstance(data, dict) else []
            prices = []
            for ask in asks:
                try:
                    price = float(ask.get("price", 0))
                    size = float(ask.get("size", 0))
                    if price > 0 and size > 0:
                        prices.append(price)
                except Exception:
                    continue
            if prices:
                return round(min(prices), 4)
        except Exception:
            pass

        return await self.fetch_price(asset_id)

    async def get_market_data(self, asset_id: str):
        if asset_id in self.market_cache:
            return self.market_cache[asset_id]
            
        # Fetch from Gamma API
        import aiohttp
        import datetime
        async with aiohttp.ClientSession(headers=self.http_headers) as http_session:
            url = f"https://gamma-api.polymarket.com/markets?token_id={asset_id}"
            try:
                async with http_session.get(url, ssl=False) as response:
                    if response.status == 200:
                        data = await response.json()
                        if isinstance(data, list) and len(data) > 0:
                            market = data[0]
                            # Parse dates
                            # startTime usually: 2026-02-16T19:15:00Z
                            start_str = market.get('startTime')
                            if start_str:
                                start_time = datetime.datetime.fromisoformat(start_str.replace("Z", "+00:00"))
                                is_5m = "5m" in market.get('slug', '').lower() or "5m" in market.get('description', '').lower()
                                
                                result = {
                                    "start_time": start_time,
                                    "is_5m": is_5m
                                }
                                self.market_cache[asset_id] = result
                                return result
            except Exception as e:
                self.log(f"Error fetching market data: {e}")
                
        return None

    def update_config(
        self,
        target_wallets: List[str] = None,
        dry_run: bool = None,
        poll_interval: int = None,
        size_mode: str = None,
        size_value: float = None,
        winning_strategy_enabled: bool = None,
        winning_price_threshold: float = None,
        winning_entry_time: float = None,
        winning_exit_time: float = None,
        strategy_mode: str = None,
        winning_size_mode: str = None,
        winning_size_value: float = None,
        martingale_initial_amount: float = None,
        martingale_recovery_entry_end_seconds: int = None,
        martingale_maker_wait_seconds: float = None,
        martingale_maker_price_offset: float = None,
        martingale_maker_sample_seconds: float = None,
        martingale_maker_record_seconds: float = None,
        martingale_maker_blind_price: float = None,
        martingale_maker_blind_end_seconds: float = None,
        fair_value_stake_usd: float = None,
        fair_value_min_taker_edge: float = None,
        fair_value_min_maker_edge: float = None,
        fair_value_min_ev_per_usd: float = None,
        fair_value_taker_guard_min_edge: float = None,
        fair_value_taker_guard_min_ev_per_usd: float = None,
        fair_value_entry_start_seconds: float = None,
        fair_value_entry_end_seconds: float = None,
        fair_value_min_price: float = None,
        fair_value_max_price: float = None,
        fair_value_core_min_price: float = None,
        fair_value_core_max_price: float = None,
        fair_value_maker_wait_seconds: float = None,
        fair_value_sample_seconds: float = None,
        fair_value_model_sensitivity_bps: float = None,
        fair_value_prefer_maker: bool = None,
        fair_value_live_trading_enabled: bool = None,
        fair_value_core_enabled: bool = None,
        fair_value_core_contrarian_only: bool = None,
        fair_value_core_max_entries_per_window: int = None,
        fair_value_min_ev_improvement_per_entry: float = None,
        fair_value_momentum_enabled: bool = None,
        fair_value_momentum_start_seconds: float = None,
        fair_value_momentum_end_seconds: float = None,
        fair_value_momentum_min_edge: float = None,
        fair_value_momentum_min_ev_per_usd: float = None,
        fair_value_momentum_min_confidence: float = None,
        fair_value_momentum_max_price: float = None,
        fair_value_momentum_stake_multiplier: float = None,
        fair_value_momentum_max_entries_per_window: int = None,
        fair_value_momentum_min_ev_improvement_per_entry: float = None,
        fair_value_late_continuation_enabled: bool = None,
        fair_value_late_continuation_start_seconds: float = None,
        fair_value_late_continuation_end_seconds: float = None,
        fair_value_late_continuation_min_abs_delta_bps: float = None,
        fair_value_late_continuation_min_probability: float = None,
        fair_value_late_continuation_max_probability: float = None,
        fair_value_late_continuation_min_ev_per_usd: float = None,
        fair_value_late_continuation_price_buffer: float = None,
        fair_value_late_continuation_max_price: float = None,
        fair_value_late_continuation_stake_multiplier: float = None,
        fair_value_late_continuation_max_entries_per_window: int = None,
        fair_value_late_continuation_min_ev_improvement_per_entry: float = None,
        fair_value_late_continuation_entry_model_min_win_prob: float = None,
        fair_value_giro_probe_enabled: bool = None,
        fair_value_giro_probe_start_seconds: float = None,
        fair_value_giro_probe_end_seconds: float = None,
        fair_value_giro_probe_min_price: float = None,
        fair_value_giro_probe_max_price: float = None,
        fair_value_giro_probe_min_abs_delta_bps: float = None,
        fair_value_giro_probe_min_probability: float = None,
        fair_value_giro_probe_min_ev_per_usd: float = None,
        fair_value_giro_probe_min_confidence: float = None,
        fair_value_giro_probe_max_entries_per_window: int = None,
        fair_value_giro_probe_min_price_step: float = None,
        fair_value_giro_probe_stake_multiplier: float = None,
        fair_value_entry_model_enabled: bool = None,
        fair_value_core_entry_model_min_win_prob: float = None,
        fair_value_core_entry_model_max_win_prob: float = None,
        fair_value_momentum_entry_model_min_win_prob: float = None,
        fair_value_shadow_dynamic_stake_enabled: bool = None,
        fair_value_shadow_dynamic_stake_min_multiplier: float = None,
        fair_value_shadow_dynamic_stake_max_multiplier: float = None,
    ):
        previous_dry_run = bool(self.dry_run)
        previous_strategy_mode = self.strategy_mode
        fair_defaults = {
            "fair_value_stake_usd": 1.0,
            "fair_value_min_taker_edge": 0.10,
            "fair_value_min_maker_edge": 0.10,
            "fair_value_min_ev_per_usd": 0.30,
            "fair_value_taker_guard_min_edge": 0.06,
            "fair_value_taker_guard_min_ev_per_usd": 0.12,
            "fair_value_entry_start_seconds": 0.0,
            "fair_value_entry_end_seconds": 240.0,
            "fair_value_min_price": 0.35,
            "fair_value_max_price": 0.50,
            "fair_value_core_min_price": 0.35,
            "fair_value_core_max_price": 0.50,
            "fair_value_maker_wait_seconds": 3.0,
            "fair_value_sample_seconds": 1.0,
            "fair_value_model_sensitivity_bps": 28.0,
            "fair_value_prefer_maker": True,
            "fair_value_live_trading_enabled": False,
            "fair_value_core_enabled": True,
            "fair_value_core_contrarian_only": False,
            "fair_value_core_max_entries_per_window": 2,
            "fair_value_min_ev_improvement_per_entry": 0.03,
            "fair_value_momentum_enabled": True,
            "fair_value_momentum_start_seconds": 240.0,
            "fair_value_momentum_end_seconds": 300.0,
            "fair_value_momentum_min_edge": 0.03,
            "fair_value_momentum_min_ev_per_usd": 0.12,
            "fair_value_momentum_min_confidence": 0.10,
            "fair_value_momentum_max_price": 0.90,
            "fair_value_momentum_stake_multiplier": 1.0,
            "fair_value_momentum_max_entries_per_window": 2,
            "fair_value_momentum_min_ev_improvement_per_entry": 0.04,
            "fair_value_late_continuation_enabled": True,
            "fair_value_late_continuation_start_seconds": 250.0,
            "fair_value_late_continuation_end_seconds": 300.0,
            "fair_value_late_continuation_min_abs_delta_bps": 5.0,
            "fair_value_late_continuation_min_probability": 0.87,
            "fair_value_late_continuation_max_probability": 0.95,
            "fair_value_late_continuation_min_ev_per_usd": 0.015,
            "fair_value_late_continuation_price_buffer": 0.01,
            "fair_value_late_continuation_max_price": 0.95,
            "fair_value_late_continuation_stake_multiplier": 1.0,
            "fair_value_late_continuation_max_entries_per_window": 2,
            "fair_value_late_continuation_min_ev_improvement_per_entry": 0.03,
            "fair_value_late_continuation_entry_model_min_win_prob": 0.0,
            "fair_value_giro_probe_enabled": True,
            "fair_value_giro_probe_start_seconds": 0.0,
            "fair_value_giro_probe_end_seconds": 300.0,
            "fair_value_giro_probe_min_price": 0.15,
            "fair_value_giro_probe_max_price": 0.42,
            "fair_value_giro_probe_min_abs_delta_bps": 0.5,
            "fair_value_giro_probe_min_probability": 0.45,
            "fair_value_giro_probe_min_ev_per_usd": 0.05,
            "fair_value_giro_probe_min_confidence": 0.10,
            "fair_value_giro_probe_max_entries_per_window": 2,
            "fair_value_giro_probe_min_price_step": 0.03,
            "fair_value_giro_probe_stake_multiplier": 1.0,
            "fair_value_giro_probe_model_enabled": True,
            "fair_value_giro_probe_model_path": os.path.join(
                self.data_dir,
                "cheap_reversal",
                "cheap_reversal_model.json",
            ),
            "fair_value_entry_model_enabled": True,
            "fair_value_core_entry_model_min_win_prob": 0.46,
            "fair_value_core_entry_model_max_win_prob": 0.54,
            "fair_value_momentum_entry_model_min_win_prob": 0.63,
            "fair_value_shadow_dynamic_stake_enabled": True,
            "fair_value_shadow_dynamic_stake_min_multiplier": 1.0,
            "fair_value_shadow_dynamic_stake_max_multiplier": 2.0,
        }
        for attr, default in fair_defaults.items():
            if not hasattr(self, attr):
                setattr(self, attr, default)
        if target_wallets is not None:
            self.target_wallets = target_wallets
        if dry_run is not None:
            self.dry_run = bool(dry_run)
            if self.dry_run != previous_dry_run:
                self._reset_martingale_for_mode_change(previous_dry_run, self.dry_run)
                self._reset_fair_value_for_mode_change(previous_dry_run, self.dry_run)
        if poll_interval is not None:
            self.poll_interval = poll_interval
        if size_mode is not None:
            self.size_mode = size_mode
        if size_value is not None:
            self.size_value = size_value
        if winning_strategy_enabled is not None:
            self.winning_strategy_enabled = winning_strategy_enabled
        if winning_price_threshold is not None:
            self.winning_price_threshold = winning_price_threshold
        if winning_entry_time is not None:
            self.winning_entry_time = winning_entry_time
        if winning_exit_time is not None:
            self.winning_exit_time = winning_exit_time
        if strategy_mode is not None:
            self.strategy_mode = strategy_mode
            self._reset_martingale_for_strategy_change(previous_strategy_mode, self.strategy_mode)
            self._reset_fair_value_for_strategy_change(previous_strategy_mode, self.strategy_mode)
        if winning_size_mode is not None:
            self.winning_size_mode = winning_size_mode
        if winning_size_value is not None:
            self.winning_size_value = winning_size_value
        if martingale_initial_amount is not None:
            self.martingale_initial_amount = martingale_initial_amount
        if martingale_recovery_entry_end_seconds is not None:
            self.martingale_recovery_entry_end_seconds = min(
                max(int(martingale_recovery_entry_end_seconds), 15),
                285,
            )
        if martingale_maker_wait_seconds is not None:
            self.martingale_maker_wait_seconds = min(max(float(martingale_maker_wait_seconds), 0.0), 10.0)
        if martingale_maker_price_offset is not None:
            self.martingale_maker_price_offset = min(max(float(martingale_maker_price_offset), 0.0), 0.10)
        if martingale_maker_sample_seconds is not None:
            self.martingale_maker_sample_seconds = min(max(float(martingale_maker_sample_seconds), 0.1), 5.0)
        if martingale_maker_record_seconds is not None:
            self.martingale_maker_record_seconds = min(max(float(martingale_maker_record_seconds), 1.0), 120.0)
        if martingale_maker_blind_price is not None:
            self.martingale_maker_blind_price = min(max(float(martingale_maker_blind_price), 0.40), 0.60)
        if martingale_maker_blind_end_seconds is not None:
            self.martingale_maker_blind_end_seconds = min(max(float(martingale_maker_blind_end_seconds), 0.0), 30.0)
        if fair_value_stake_usd is not None:
            self.fair_value_stake_usd = min(max(float(fair_value_stake_usd), 0.01), 1000.0)
        if fair_value_min_taker_edge is not None:
            self.fair_value_min_taker_edge = min(max(float(fair_value_min_taker_edge), 0.0), 0.50)
        if fair_value_min_maker_edge is not None:
            self.fair_value_min_maker_edge = min(max(float(fair_value_min_maker_edge), 0.0), 0.50)
        if fair_value_min_ev_per_usd is not None:
            self.fair_value_min_ev_per_usd = min(max(float(fair_value_min_ev_per_usd), 0.0), 5.0)
        if fair_value_taker_guard_min_edge is not None:
            self.fair_value_taker_guard_min_edge = min(max(float(fair_value_taker_guard_min_edge), 0.0), 0.50)
        if fair_value_taker_guard_min_ev_per_usd is not None:
            self.fair_value_taker_guard_min_ev_per_usd = min(max(float(fair_value_taker_guard_min_ev_per_usd), 0.0), 5.0)
        if fair_value_entry_start_seconds is not None:
            self.fair_value_entry_start_seconds = min(max(float(fair_value_entry_start_seconds), 0.0), 300.0)
        if fair_value_entry_end_seconds is not None:
            self.fair_value_entry_end_seconds = min(max(float(fair_value_entry_end_seconds), 1.0), 300.0)
        if self.fair_value_entry_end_seconds < self.fair_value_entry_start_seconds:
            self.fair_value_entry_end_seconds = self.fair_value_entry_start_seconds
        if fair_value_min_price is not None:
            self.fair_value_min_price = min(max(float(fair_value_min_price), 0.01), 0.99)
        if fair_value_max_price is not None:
            self.fair_value_max_price = min(max(float(fair_value_max_price), 0.01), 0.99)
        if self.fair_value_min_price > self.fair_value_max_price:
            self.fair_value_min_price, self.fair_value_max_price = self.fair_value_max_price, self.fair_value_min_price
        if fair_value_core_min_price is not None:
            self.fair_value_core_min_price = min(max(float(fair_value_core_min_price), 0.01), 0.99)
        if fair_value_core_max_price is not None:
            self.fair_value_core_max_price = min(max(float(fair_value_core_max_price), 0.01), 0.99)
        if self.fair_value_core_min_price > self.fair_value_core_max_price:
            self.fair_value_core_min_price, self.fair_value_core_max_price = (
                self.fair_value_core_max_price,
                self.fair_value_core_min_price,
            )
        if fair_value_maker_wait_seconds is not None:
            self.fair_value_maker_wait_seconds = min(max(float(fair_value_maker_wait_seconds), 0.0), 30.0)
        if fair_value_sample_seconds is not None:
            self.fair_value_sample_seconds = min(max(float(fair_value_sample_seconds), 0.1), 10.0)
        if fair_value_model_sensitivity_bps is not None:
            self.fair_value_model_sensitivity_bps = min(max(float(fair_value_model_sensitivity_bps), 1.0), 250.0)
        if fair_value_prefer_maker is not None:
            self.fair_value_prefer_maker = bool(fair_value_prefer_maker)
        if fair_value_live_trading_enabled is not None:
            self.fair_value_live_trading_enabled = bool(fair_value_live_trading_enabled)
        if fair_value_core_enabled is not None:
            self.fair_value_core_enabled = bool(fair_value_core_enabled)
        if fair_value_core_contrarian_only is not None:
            self.fair_value_core_contrarian_only = bool(fair_value_core_contrarian_only)
        if fair_value_core_max_entries_per_window is not None:
            self.fair_value_core_max_entries_per_window = min(
                max(int(fair_value_core_max_entries_per_window), 0),
                50,
            )
        if fair_value_min_ev_improvement_per_entry is not None:
            self.fair_value_min_ev_improvement_per_entry = min(
                max(float(fair_value_min_ev_improvement_per_entry), 0.0),
                5.0,
            )
        if fair_value_momentum_enabled is not None:
            self.fair_value_momentum_enabled = bool(fair_value_momentum_enabled)
        if fair_value_momentum_start_seconds is not None:
            self.fair_value_momentum_start_seconds = min(max(float(fair_value_momentum_start_seconds), 0.0), 300.0)
        if fair_value_momentum_end_seconds is not None:
            self.fair_value_momentum_end_seconds = min(max(float(fair_value_momentum_end_seconds), 1.0), 300.0)
        if self.fair_value_momentum_end_seconds < self.fair_value_momentum_start_seconds:
            self.fair_value_momentum_end_seconds = self.fair_value_momentum_start_seconds
        if fair_value_momentum_min_edge is not None:
            self.fair_value_momentum_min_edge = min(max(float(fair_value_momentum_min_edge), 0.0), 0.50)
        if fair_value_momentum_min_ev_per_usd is not None:
            self.fair_value_momentum_min_ev_per_usd = min(max(float(fair_value_momentum_min_ev_per_usd), 0.0), 5.0)
        if fair_value_momentum_min_confidence is not None:
            self.fair_value_momentum_min_confidence = min(max(float(fair_value_momentum_min_confidence), 0.0), 1.0)
        if fair_value_momentum_max_price is not None:
            self.fair_value_momentum_max_price = min(max(float(fair_value_momentum_max_price), 0.01), 0.99)
        if fair_value_momentum_stake_multiplier is not None:
            self.fair_value_momentum_stake_multiplier = min(max(float(fair_value_momentum_stake_multiplier), 0.01), 25.0)
        if fair_value_momentum_max_entries_per_window is not None:
            self.fair_value_momentum_max_entries_per_window = min(
                max(int(fair_value_momentum_max_entries_per_window), 0),
                50,
            )
        if fair_value_momentum_min_ev_improvement_per_entry is not None:
            self.fair_value_momentum_min_ev_improvement_per_entry = min(
                max(float(fair_value_momentum_min_ev_improvement_per_entry), 0.0),
                5.0,
            )
        if fair_value_late_continuation_enabled is not None:
            self.fair_value_late_continuation_enabled = bool(fair_value_late_continuation_enabled)
        if fair_value_late_continuation_start_seconds is not None:
            self.fair_value_late_continuation_start_seconds = min(
                max(float(fair_value_late_continuation_start_seconds), 0.0),
                300.0,
            )
        if fair_value_late_continuation_end_seconds is not None:
            self.fair_value_late_continuation_end_seconds = min(
                max(float(fair_value_late_continuation_end_seconds), 1.0),
                300.0,
            )
        if self.fair_value_late_continuation_end_seconds < self.fair_value_late_continuation_start_seconds:
            self.fair_value_late_continuation_end_seconds = self.fair_value_late_continuation_start_seconds
        if fair_value_late_continuation_min_abs_delta_bps is not None:
            self.fair_value_late_continuation_min_abs_delta_bps = min(
                max(float(fair_value_late_continuation_min_abs_delta_bps), 0.0),
                250.0,
            )
        if fair_value_late_continuation_min_probability is not None:
            self.fair_value_late_continuation_min_probability = min(
                max(float(fair_value_late_continuation_min_probability), 0.5),
                0.999,
            )
        if fair_value_late_continuation_max_probability is not None:
            self.fair_value_late_continuation_max_probability = min(
                max(float(fair_value_late_continuation_max_probability), 0.5),
                0.999,
            )
        if self.fair_value_late_continuation_min_probability > self.fair_value_late_continuation_max_probability:
            self.fair_value_late_continuation_min_probability, self.fair_value_late_continuation_max_probability = (
                self.fair_value_late_continuation_max_probability,
                self.fair_value_late_continuation_min_probability,
            )
        if fair_value_late_continuation_min_ev_per_usd is not None:
            self.fair_value_late_continuation_min_ev_per_usd = min(
                max(float(fair_value_late_continuation_min_ev_per_usd), 0.0),
                5.0,
            )
        if fair_value_late_continuation_price_buffer is not None:
            self.fair_value_late_continuation_price_buffer = min(
                max(float(fair_value_late_continuation_price_buffer), 0.0),
                0.25,
            )
        if fair_value_late_continuation_max_price is not None:
            self.fair_value_late_continuation_max_price = min(
                max(float(fair_value_late_continuation_max_price), 0.01),
                0.99,
            )
        if fair_value_late_continuation_stake_multiplier is not None:
            self.fair_value_late_continuation_stake_multiplier = min(
                max(float(fair_value_late_continuation_stake_multiplier), 0.01),
                25.0,
            )
        if fair_value_late_continuation_max_entries_per_window is not None:
            self.fair_value_late_continuation_max_entries_per_window = min(
                max(int(fair_value_late_continuation_max_entries_per_window), 0),
                50,
            )
        if fair_value_late_continuation_min_ev_improvement_per_entry is not None:
            self.fair_value_late_continuation_min_ev_improvement_per_entry = min(
                max(float(fair_value_late_continuation_min_ev_improvement_per_entry), 0.0),
                5.0,
            )
        if fair_value_late_continuation_entry_model_min_win_prob is not None:
            self.fair_value_late_continuation_entry_model_min_win_prob = min(
                max(float(fair_value_late_continuation_entry_model_min_win_prob), 0.0),
                1.0,
            )
        if fair_value_giro_probe_enabled is not None:
            self.fair_value_giro_probe_enabled = bool(fair_value_giro_probe_enabled)
        if fair_value_giro_probe_start_seconds is not None:
            self.fair_value_giro_probe_start_seconds = min(max(float(fair_value_giro_probe_start_seconds), 0.0), 300.0)
        if fair_value_giro_probe_end_seconds is not None:
            self.fair_value_giro_probe_end_seconds = min(max(float(fair_value_giro_probe_end_seconds), 1.0), 300.0)
        if self.fair_value_giro_probe_end_seconds < self.fair_value_giro_probe_start_seconds:
            self.fair_value_giro_probe_end_seconds = self.fair_value_giro_probe_start_seconds
        if fair_value_giro_probe_min_price is not None:
            self.fair_value_giro_probe_min_price = min(max(float(fair_value_giro_probe_min_price), 0.01), 0.99)
        if fair_value_giro_probe_max_price is not None:
            self.fair_value_giro_probe_max_price = min(max(float(fair_value_giro_probe_max_price), 0.01), 0.99)
        if self.fair_value_giro_probe_min_price > self.fair_value_giro_probe_max_price:
            self.fair_value_giro_probe_min_price, self.fair_value_giro_probe_max_price = (
                self.fair_value_giro_probe_max_price,
                self.fair_value_giro_probe_min_price,
            )
        if fair_value_giro_probe_min_abs_delta_bps is not None:
            self.fair_value_giro_probe_min_abs_delta_bps = min(max(float(fair_value_giro_probe_min_abs_delta_bps), 0.0), 250.0)
        if fair_value_giro_probe_min_probability is not None:
            self.fair_value_giro_probe_min_probability = min(max(float(fair_value_giro_probe_min_probability), 0.0), 1.0)
        if fair_value_giro_probe_min_ev_per_usd is not None:
            self.fair_value_giro_probe_min_ev_per_usd = min(max(float(fair_value_giro_probe_min_ev_per_usd), -1.0), 10.0)
        if fair_value_giro_probe_min_confidence is not None:
            self.fair_value_giro_probe_min_confidence = min(max(float(fair_value_giro_probe_min_confidence), 0.0), 1.0)
        if fair_value_giro_probe_max_entries_per_window is not None:
            self.fair_value_giro_probe_max_entries_per_window = min(
                max(int(fair_value_giro_probe_max_entries_per_window), 0),
                100,
            )
        if fair_value_giro_probe_min_price_step is not None:
            self.fair_value_giro_probe_min_price_step = min(max(float(fair_value_giro_probe_min_price_step), 0.0), 0.50)
        if fair_value_giro_probe_stake_multiplier is not None:
            self.fair_value_giro_probe_stake_multiplier = min(max(float(fair_value_giro_probe_stake_multiplier), 0.01), 25.0)
        if fair_value_entry_model_enabled is not None:
            self.fair_value_entry_model_enabled = bool(fair_value_entry_model_enabled)
        if fair_value_core_entry_model_min_win_prob is not None:
            self.fair_value_core_entry_model_min_win_prob = min(
                max(float(fair_value_core_entry_model_min_win_prob), 0.0),
                1.0,
            )
        if fair_value_core_entry_model_max_win_prob is not None:
            self.fair_value_core_entry_model_max_win_prob = min(
                max(float(fair_value_core_entry_model_max_win_prob), 0.0),
                1.0,
            )
        if self.fair_value_core_entry_model_min_win_prob > self.fair_value_core_entry_model_max_win_prob:
            self.fair_value_core_entry_model_min_win_prob, self.fair_value_core_entry_model_max_win_prob = (
                self.fair_value_core_entry_model_max_win_prob,
                self.fair_value_core_entry_model_min_win_prob,
            )
        if fair_value_momentum_entry_model_min_win_prob is not None:
            self.fair_value_momentum_entry_model_min_win_prob = min(
                max(float(fair_value_momentum_entry_model_min_win_prob), 0.0),
                1.0,
            )
        if fair_value_shadow_dynamic_stake_enabled is not None:
            self.fair_value_shadow_dynamic_stake_enabled = bool(fair_value_shadow_dynamic_stake_enabled)
        if fair_value_shadow_dynamic_stake_min_multiplier is not None:
            self.fair_value_shadow_dynamic_stake_min_multiplier = float(fair_value_shadow_dynamic_stake_min_multiplier)
        if fair_value_shadow_dynamic_stake_max_multiplier is not None:
            self.fair_value_shadow_dynamic_stake_max_multiplier = float(fair_value_shadow_dynamic_stake_max_multiplier)
        self.fair_value_shadow_dynamic_stake_min_multiplier = min(
            max(float(self.fair_value_shadow_dynamic_stake_min_multiplier), 0.01),
            10.0,
        )
        self.fair_value_shadow_dynamic_stake_max_multiplier = min(
            max(float(self.fair_value_shadow_dynamic_stake_max_multiplier), self.fair_value_shadow_dynamic_stake_min_multiplier),
            10.0,
        )
            
        self.log(
            f"Config Updated: Strat={self.strategy_mode}, Targets={len(self.target_wallets)}, "
            f"DryRun={self.dry_run}, Interval={self.poll_interval}, "
            f"WinSize={self.winning_size_value} ({self.winning_size_mode}), "
            f"Martingale={self.martingale_initial_amount}, FairValue=${self.fair_value_stake_usd:.2f}"
        )
        
        # Save to history
        from datetime import datetime
        entry = {
            "timestamp": datetime.now().isoformat(),
            "target_wallets": self.target_wallets,
            "dry_run": self.dry_run,
            "poll_interval": self.poll_interval,
            "size_mode": self.size_mode,
            "size_value": self.size_value,
            "winning_strategy_enabled": self.winning_strategy_enabled,
            "winning_price_threshold": self.winning_price_threshold,
            "strategy_mode": self.strategy_mode,
            "martingale_initial_amount": self.martingale_initial_amount,
            "martingale_entry_start_seconds": self.martingale_entry_start_seconds,
            "martingale_entry_end_seconds": self.martingale_entry_end_seconds,
            "martingale_min_entry_price": self.martingale_min_entry_price,
            "martingale_max_entry_price": self.martingale_max_entry_price,
            "martingale_recovery_entry_end_seconds": self.martingale_recovery_entry_end_seconds,
            "martingale_recovery_min_entry_price": self.martingale_recovery_min_entry_price,
            "martingale_recovery_max_entry_price": self.martingale_recovery_max_entry_price,
            "martingale_taker_fee_rate": self.martingale_taker_fee_rate,
            "martingale_balance_floor_fraction": self.martingale_balance_floor_fraction,
            "martingale_order_type": self.martingale_order_type,
            "martingale_chainlink_boundary_tolerance_seconds": self.martingale_chainlink_boundary_tolerance_seconds,
            "martingale_chainlink_min_decision_margin": self.martingale_chainlink_min_decision_margin,
            "martingale_maker_wait_seconds": self.martingale_maker_wait_seconds,
            "martingale_maker_price_offset": self.martingale_maker_price_offset,
            "martingale_maker_sample_seconds": self.martingale_maker_sample_seconds,
            "martingale_maker_record_seconds": self.martingale_maker_record_seconds,
            "martingale_maker_blind_price": self.martingale_maker_blind_price,
            "martingale_maker_blind_end_seconds": self.martingale_maker_blind_end_seconds,
            "fair_value_stake_usd": self.fair_value_stake_usd,
            "fair_value_min_taker_edge": self.fair_value_min_taker_edge,
            "fair_value_min_maker_edge": self.fair_value_min_maker_edge,
            "fair_value_entry_start_seconds": self.fair_value_entry_start_seconds,
            "fair_value_entry_end_seconds": self.fair_value_entry_end_seconds,
            "fair_value_min_ev_per_usd": self.fair_value_min_ev_per_usd,
            "fair_value_taker_guard_min_edge": self.fair_value_taker_guard_min_edge,
            "fair_value_taker_guard_min_ev_per_usd": self.fair_value_taker_guard_min_ev_per_usd,
            "fair_value_min_price": self.fair_value_min_price,
            "fair_value_max_price": self.fair_value_max_price,
            "fair_value_core_min_price": self.fair_value_core_min_price,
            "fair_value_core_max_price": self.fair_value_core_max_price,
            "fair_value_maker_wait_seconds": self.fair_value_maker_wait_seconds,
            "fair_value_sample_seconds": self.fair_value_sample_seconds,
            "fair_value_model_sensitivity_bps": self.fair_value_model_sensitivity_bps,
            "fair_value_prefer_maker": self.fair_value_prefer_maker,
            "fair_value_live_trading_enabled": self.fair_value_live_trading_enabled,
            "fair_value_core_enabled": self.fair_value_core_enabled,
            "fair_value_core_contrarian_only": self.fair_value_core_contrarian_only,
            "fair_value_core_max_entries_per_window": self.fair_value_core_max_entries_per_window,
            "fair_value_min_ev_improvement_per_entry": self.fair_value_min_ev_improvement_per_entry,
            "fair_value_momentum_enabled": self.fair_value_momentum_enabled,
            "fair_value_momentum_start_seconds": self.fair_value_momentum_start_seconds,
            "fair_value_momentum_end_seconds": self.fair_value_momentum_end_seconds,
            "fair_value_momentum_min_edge": self.fair_value_momentum_min_edge,
            "fair_value_momentum_min_ev_per_usd": self.fair_value_momentum_min_ev_per_usd,
            "fair_value_momentum_min_confidence": self.fair_value_momentum_min_confidence,
            "fair_value_momentum_max_price": self.fair_value_momentum_max_price,
            "fair_value_momentum_stake_multiplier": self.fair_value_momentum_stake_multiplier,
            "fair_value_momentum_max_entries_per_window": self.fair_value_momentum_max_entries_per_window,
            "fair_value_momentum_min_ev_improvement_per_entry": self.fair_value_momentum_min_ev_improvement_per_entry,
            "fair_value_late_continuation_enabled": self.fair_value_late_continuation_enabled,
            "fair_value_late_continuation_start_seconds": self.fair_value_late_continuation_start_seconds,
            "fair_value_late_continuation_end_seconds": self.fair_value_late_continuation_end_seconds,
            "fair_value_late_continuation_min_abs_delta_bps": self.fair_value_late_continuation_min_abs_delta_bps,
            "fair_value_late_continuation_min_probability": self.fair_value_late_continuation_min_probability,
            "fair_value_late_continuation_max_probability": self.fair_value_late_continuation_max_probability,
            "fair_value_late_continuation_min_ev_per_usd": self.fair_value_late_continuation_min_ev_per_usd,
            "fair_value_late_continuation_price_buffer": self.fair_value_late_continuation_price_buffer,
            "fair_value_late_continuation_max_price": self.fair_value_late_continuation_max_price,
            "fair_value_late_continuation_stake_multiplier": self.fair_value_late_continuation_stake_multiplier,
            "fair_value_late_continuation_max_entries_per_window": self.fair_value_late_continuation_max_entries_per_window,
            "fair_value_late_continuation_min_ev_improvement_per_entry": self.fair_value_late_continuation_min_ev_improvement_per_entry,
            "fair_value_late_continuation_entry_model_min_win_prob": self.fair_value_late_continuation_entry_model_min_win_prob,
            "fair_value_giro_probe_enabled": self.fair_value_giro_probe_enabled,
            "fair_value_giro_probe_start_seconds": self.fair_value_giro_probe_start_seconds,
            "fair_value_giro_probe_end_seconds": self.fair_value_giro_probe_end_seconds,
            "fair_value_giro_probe_min_price": self.fair_value_giro_probe_min_price,
            "fair_value_giro_probe_max_price": self.fair_value_giro_probe_max_price,
            "fair_value_giro_probe_min_abs_delta_bps": self.fair_value_giro_probe_min_abs_delta_bps,
            "fair_value_giro_probe_min_probability": self.fair_value_giro_probe_min_probability,
            "fair_value_giro_probe_min_ev_per_usd": self.fair_value_giro_probe_min_ev_per_usd,
            "fair_value_giro_probe_min_confidence": self.fair_value_giro_probe_min_confidence,
            "fair_value_giro_probe_max_entries_per_window": self.fair_value_giro_probe_max_entries_per_window,
            "fair_value_giro_probe_min_price_step": self.fair_value_giro_probe_min_price_step,
            "fair_value_giro_probe_stake_multiplier": self.fair_value_giro_probe_stake_multiplier,
            "fair_value_entry_model_enabled": self.fair_value_entry_model_enabled,
            "fair_value_core_entry_model_min_win_prob": self.fair_value_core_entry_model_min_win_prob,
            "fair_value_core_entry_model_max_win_prob": self.fair_value_core_entry_model_max_win_prob,
            "fair_value_momentum_entry_model_min_win_prob": self.fair_value_momentum_entry_model_min_win_prob,
            "fair_value_shadow_dynamic_stake_enabled": self.fair_value_shadow_dynamic_stake_enabled,
            "fair_value_shadow_dynamic_stake_min_multiplier": self.fair_value_shadow_dynamic_stake_min_multiplier,
            "fair_value_shadow_dynamic_stake_max_multiplier": self.fair_value_shadow_dynamic_stake_max_multiplier,
        }
        # Prepend to history (newest first)
        self.history.insert(0, entry)
        # Keep history limited to last 50 entries
        if len(self.history) > 50:
            self.history = self.history[:50]
            
        self.save_state()

if __name__ == "__main__":
    # Legacy CLI entry point
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", required=True, help="Target wallet address to copy")
    parser.add_argument("--dry-run", action="store_true", help="Log trades only, do not execute")
    args = parser.parse_args()
    
    trader = CopyTrader(target_wallet=args.target, dry_run=args.dry_run)
    
    try:
        asyncio.run(trader.start())
        # Keep main thread alive since start() spawns a background task
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_forever()
    except KeyboardInterrupt:
        print("Stopping trader...")
