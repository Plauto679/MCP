import asyncio
import os
import sys
import math
import json
import time
from typing import List, Dict, Optional, Tuple
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

# We need to run the server as a subprocess for MCP connection
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

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
        self._martingale_prefetch_attempts = {}
        self._martingale_entry_plans = {}
        self._martingale_maker_recorder_tasks = {}
        self._martingale_maker_recorder_context = {}
        self._chainlink_price_samples = {}
        self._chainlink_feed_task: Optional[asyncio.Task] = None
        self._chainlink_connected = False
        self._last_chainlink_warn_ts: Optional[float] = None
        self._background_tasks = set()
        self.http_headers = {
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36",
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
        self.polymarket_min_market_buy_usd = 1.0
        self.polymarket_min_limit_order_shares = 5.0
        self.data_dir = os.path.join(os.path.dirname(__file__), "..", "data")
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
        file_exists = os.path.exists(path)
        with open(path, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(row.keys()))
            if not file_exists:
                writer.writeheader()
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
        try:
            async with http_session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as response:
                if response.status == 200:
                    data = await response.json()
                    if isinstance(data, dict):
                        return data
        except Exception:
            pass
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
                "martingale_maker_blind_end_seconds": self.martingale_maker_blind_end_seconds
            },
            "multipliers": self.app_state["multipliers"],
            "history": self.history,
            "pending_settlements": self.app_state.get("pending_settlements", []),
            "martingale_state": martingale_state
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
                        if self._is_martingale_mode():
                            now_after_tick = time.time()
                            seconds_to_boundary = 300.0 - (now_after_tick % 300.0)
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
        try:
            async with http_session.get(url) as response:
                if response.status == 200:
                    data = await response.json()
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
        except Exception:
            pass
        return None

    async def fetch_price(self, asset_id: str) -> float:
        import aiohttp

        http_session = await self._get_http_session()
        url = f"https://clob.polymarket.com/price?token_id={asset_id}&side=buy"
        try:
            async with http_session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as response:
                if response.status == 200:
                    data = await response.json()
                    return float(data.get('price', 0))
        except Exception:
            pass
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
    ):
        previous_dry_run = bool(self.dry_run)
        previous_strategy_mode = self.strategy_mode
        if target_wallets is not None:
            self.target_wallets = target_wallets
        if dry_run is not None:
            self.dry_run = bool(dry_run)
            if self.dry_run != previous_dry_run:
                self._reset_martingale_for_mode_change(previous_dry_run, self.dry_run)
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
            
        self.log(f"Config Updated: Strat={self.strategy_mode}, Targets={len(self.target_wallets)}, DryRun={self.dry_run}, Interval={self.poll_interval}, WinSize={self.winning_size_value} ({self.winning_size_mode}), Martingale={self.martingale_initial_amount}")
        
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
            "martingale_maker_blind_end_seconds": self.martingale_maker_blind_end_seconds
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
