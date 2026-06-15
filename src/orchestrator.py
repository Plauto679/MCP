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
        self.strategy_mode = 'copy' # 'copy', 'winning', or 'martingale'
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
        self.martingale_entry_end_seconds = 15
        self.martingale_min_entry_price = 0.40
        self.martingale_max_entry_price = 0.60
        self.martingale_recovery_entry_end_seconds = 120
        self.martingale_recovery_min_entry_price = 0.40
        self.martingale_recovery_max_entry_price = 0.60
        self.martingale_limit_slippage = 0.05
        self.polymarket_min_market_buy_usd = 1.0
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
        while True:
            opening_price = self._chainlink_boundary_price(start_ts)
            closing_price = self._chainlink_boundary_price(end_ts)
            if opening_price is not None and closing_price is not None:
                up_won = closing_price >= opening_price
                is_win = up_won if direction == "Yes" else not up_won
                return is_win, opening_price, closing_price
            if asyncio.get_running_loop().time() >= deadline:
                return None, opening_price, closing_price
            await asyncio.sleep(0.05)

    def _martingale_stake_for_price(self, state: Dict, price: float) -> float:
        target_profit = max(float(self.martingale_initial_amount), 0.01)
        accumulated_losses = max(float(state.get("loss_bank", 0.0)), 0.0)
        p = min(max(float(price), 0.01), 0.99)
        stake = (accumulated_losses + target_profit) * p / (1.0 - p)
        return round(max(stake, 0.01), 2)

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

    def _order_fill_from_response(self, order_data: Dict, fallback_usd: float, fallback_price: float) -> Tuple[float, float, float]:
        def parse_amount(value) -> float:
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

        rows = order_data.get("items") if isinstance(order_data, dict) else None
        if not rows:
            rows = [order_data] if isinstance(order_data, dict) else []

        filled_usd = 0.0
        filled_shares = 0.0
        for row in rows:
            if not isinstance(row, dict):
                continue
            filled_usd += parse_amount(row.get("makingAmount"))
            filled_shares += parse_amount(row.get("takingAmount"))

        if filled_usd <= 0:
            filled_usd = round(float(fallback_usd), 4)
        if filled_shares <= 0:
            filled_shares = round(float(fallback_usd) / max(float(fallback_price), 0.01), 4)

        avg_price = round(filled_usd / filled_shares, 6) if filled_shares > 0 else float(fallback_price)
        return round(filled_usd, 4), round(filled_shares, 4), avg_price

    def _martingale_in_recovery(self, state: Dict) -> bool:
        return int(state.get("streak", 0) or 0) > 0 or float(state.get("loss_bank", 0.0) or 0.0) > 0

    def _entry_price_bounds(self, state: Dict, elapsed: float) -> Tuple[float, float]:
        if self._martingale_in_recovery(state) and elapsed > self.martingale_entry_end_seconds:
            return self.martingale_recovery_min_entry_price, self.martingale_recovery_max_entry_price
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
        state["entry_retry_after"] = 0.0
        state["entry_attempts"] = 0
        state["skip_logged"] = True
        self.log(
            f"[Martingale] Coverage gap on {slug}. Resetting probability cycle "
            f"(streak was {previous_streak}, loss_bank was ${previous_loss_bank:.2f})."
        )

    def _apply_martingale_result(self, state: Dict, slug: str, is_win: bool, shares: float, entry_price: float):
        if is_win:
            state["streak"] = 0
            state["direction"] = None
            state["loss_bank"] = 0.0
            state["current_amount"] = self.martingale_initial_amount
            self.log(
                f"[Martingale] WIN settled for {slug}. "
                f"Resetting cycle; next target profit: ${self.martingale_initial_amount:.2f}."
            )
        else:
            state["streak"] = int(state.get("streak", 0) or 0) + 1
            loss_bank = float(state.get("loss_bank", 0.0) or 0.0)
            loss_bank += float(shares) * float(entry_price)
            state["loss_bank"] = round(loss_bank, 4)
            state["current_amount"] = 0.0
            self.log(
                f"[Martingale] LOSS settled for {slug}. "
                f"Loss bank: ${state['loss_bank']:.2f}. Next stake will target "
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
        settlement_source = "chainlink_boundary"
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
        if settlement_source == "chainlink_boundary":
            self.log(
                f"[Martingale] Chainlink boundary | open={opening_price:.8f} | "
                f"close={closing_price:.8f} | "
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

        current_cost = float(state.get("entry_shares", 0.0)) * float(state.get("entry_price", 0.0))
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
                "loss_bank": round(loss_bank_before + current_cost, 4),
                "recovery_target": round(
                    loss_bank_before + current_cost + float(self.martingale_initial_amount),
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

        entry_notional_usd = round(entry_shares * entry_price, 4)
        exit_value_est_usd = round(entry_shares * exit_price, 4)
        payout_est_usd = round(entry_shares * (1.0 if is_win is True else 0.0), 4)
        if is_win is None:
            pnl_est_usd = round(exit_value_est_usd - entry_notional_usd, 4)
        else:
            pnl_est_usd = round(payout_est_usd - entry_notional_usd, 4)
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
            "Exit Price (snapshot)": round(exit_price, 6),
            "Entry Notional USD": entry_notional_usd,
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
                    if self.martingale_entry_end_seconds > 15:
                        self.martingale_entry_end_seconds = 15
                    if self.martingale_min_entry_price < 0.40:
                        self.martingale_min_entry_price = 0.40
                    if self.martingale_max_entry_price > 0.60:
                        self.martingale_max_entry_price = 0.60
                    self.martingale_recovery_entry_end_seconds = min(
                        max(int(self.martingale_recovery_entry_end_seconds), 15),
                        120,
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
                        self.app_state["martingale_state"] = martingale_state
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
                "martingale_recovery_max_entry_price": self.martingale_recovery_max_entry_price
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
        self.logs.append(message)
        if len(self.logs) > 300:
            self.logs.pop(0)

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
                    if self.strategy_mode == "martingale":
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
                            or (self.strategy_mode == "martingale" and has_pending_settlements)
                        )
                        if should_reconcile:
                            await self.reconcile_pending_settlements(session)
                            self._last_reconcile_ts = now_ts
                        
                        await self.tick(session)
                        if self._last_heartbeat_ts is None or (now_ts - self._last_heartbeat_ts) >= 60:
                            self.log(
                                f"[Heartbeat] running=True | strategy={self.strategy_mode} | "
                                f"poll={self.poll_interval}s | logs={len(self.logs)}"
                            )
                            self._last_heartbeat_ts = now_ts
                        tick_elapsed = time.time() - now_ts
                        sleep_for = max(0.05, float(self.poll_interval) - tick_elapsed)
                        if self.strategy_mode == "martingale":
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
            if self.strategy_mode == 'martingale':
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
            self.app_state["martingale_state"] = {
                "active_slug": None,
                "entry_done": False,
                "streak": 0,
                "current_amount": self.martingale_initial_amount,  # USD notional
                "direction": None, # "Yes" or "No" (UP or DOWN)
                "entry_price": 0.0,
                "target_token_id": None,
                "entry_shares": 0.0,
                "loss_bank": 0.0,
                "entry_retry_after": 0.0,
                "entry_attempts": 0,
                "skip_logged": False,
                "opened_at": None,
            }
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
                            f"[Martingale] Previous window {previous_slug} has no exact Chainlink boundary "
                            "sample or official outcome yet. Holding entry without guessing."
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

            # Pick once per cycle/window; on loss streaks, keep the same direction.
            if not state["direction"]:
                eligible_directions = [
                    label for label, price in prices.items()
                    if price > 0 and self._entry_price_allowed(price, min_entry_price, max_entry_price)
                ]
                if not eligible_directions:
                    if not state.get("skip_logged"):
                        self.log(
                            f"[Martingale] Skipping {slug}: entry prices outside "
                            f"{min_entry_price:.2f}-{max_entry_price:.2f} "
                            f"(Yes={prices.get('Yes', 0):.4f}, No={prices.get('No', 0):.4f}). Retrying until cutoff."
                        )
                        state["skip_logged"] = True
                        self.save_state()
                    return
                state["direction"] = random.choice(eligible_directions)
                if int(state.get("streak", 0)) == 0:
                    state["loss_bank"] = 0.0
                
            # Polymarket tokens: UP/Yes is index 0, DOWN/No is index 1 depending on market
            # Typically for up/down markets, 0 is UP (Yes), 1 is DOWN (No). 
            # We assume UP is 0, DOWN is 1. If not, we still bet on the same token.
            token_index = 0 if state["direction"] == "Yes" else 1
            target_token_id = clob_token_ids[token_index]
            state["target_token_id"] = target_token_id
            
            entry_price = prices.get(state["direction"], 0.0)
            if entry_price <= 0:
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
            
            self.log(
                f"[Martingale] ENTRY | slug={slug} | direction={state['direction']} | "
                f"usd=${usd_to_buy:.2f} | px={entry_price:.4f} | shares~={estimated_shares:.4f} | "
                f"loss_bank=${float(state.get('loss_bank', 0.0)):.2f} | target=${self.martingale_initial_amount:.2f} | "
                f"min=${self.polymarket_min_market_buy_usd:.2f} | streak={state['streak']} | elapsed={elapsed:.2f}s"
            )
            
            if not self.dry_run:
                try:
                    order_res = await session.call_tool("place_market_order", arguments={
                        "market_slug": slug, 
                        "side": "BUY",
                        "amount": usd_to_buy,
                        "token_id": target_token_id,
                        "order_type": "FAK",
                        "defer_exec": False,
                    })
                    order_text = order_res.content[0].text if order_res and order_res.content else ""
                    order_ok, order_error, order_data = self._parse_order_response(order_text, allow_live=False)
                    if not order_ok:
                        self.log(f"[Error] Martingale order rejected: {order_error[:220]}")
                        state["entry_attempts"] = int(state.get("entry_attempts", 0) or 0) + 1
                        delay = self._martingale_retry_delay(f"{order_error} {order_text}", state["entry_attempts"])
                        state["entry_retry_after"] = now_ts + delay
                        if self._is_permanent_order_error(f"{order_error} {order_text}"):
                            state["entry_retry_after"] = start_time.timestamp() + entry_end_seconds + 1
                            self.log(f"[Martingale] Permanent order format/min-size error. Skipping entry for {slug}.")
                        state["opened_at"] = None
                        self.save_state()
                        return
                    filled_usd, filled_shares, avg_entry_price = self._order_fill_from_response(
                        order_data,
                        fallback_usd=usd_to_buy,
                        fallback_price=entry_price,
                    )
                    self.log(f"[Martingale] Executed Buy: {order_text[:50]}...")
                except Exception as e:
                      self.log(f"[Error] Martingale Execution failed: {e}")
                      state["entry_attempts"] = int(state.get("entry_attempts", 0) or 0) + 1
                      state["entry_retry_after"] = now_ts + self._martingale_retry_delay(str(e), state["entry_attempts"])
                      state["opened_at"] = None
                      self.save_state()
                      return
            else:
                self.log("[Martingale] Dry Run - Simulated BUY")
                filled_usd = usd_to_buy
                filled_shares = estimated_shares
                avg_entry_price = entry_price

            state["entry_price"] = avg_entry_price
            state["current_amount"] = filled_usd
            state["entry_shares"] = filled_shares
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
        import aiohttp

        http_session = await self._get_http_session()
        try:
            url = f"https://clob.polymarket.com/book?token_id={asset_id}"
            async with http_session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as response:
                if response.status == 200:
                    data = await response.json()
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

    def update_config(self, target_wallets: List[str] = None, dry_run: bool = None, poll_interval: int = None, size_mode: str = None, size_value: float = None, winning_strategy_enabled: bool = None, winning_price_threshold: float = None, winning_entry_time: float = None, winning_exit_time: float = None, strategy_mode: str = None, winning_size_mode: str = None, winning_size_value: float = None, martingale_initial_amount: float = None):
        if target_wallets is not None:
            self.target_wallets = target_wallets
        if dry_run is not None:
            self.dry_run = dry_run
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
        if winning_size_mode is not None:
            self.winning_size_mode = winning_size_mode
        if winning_size_value is not None:
            self.winning_size_value = winning_size_value
        if martingale_initial_amount is not None:
            self.martingale_initial_amount = martingale_initial_amount
            
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
            "martingale_recovery_max_entry_price": self.martingale_recovery_max_entry_price
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
