import asyncio
import os
import sys
import math
from typing import List, Dict, Optional
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
            "multipliers": {} # asset_id -> ratio (my_shares / target_shares)
        }
        self.initial_sync_complete = False
        self.logs: List[str] = []
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
                    self.app_state["multipliers"] = data.get('multipliers', {})
                    self.history = data.get('history', [])
                    self.log(f"State loaded from {self.data_file}")
            except Exception as e:
                self.log(f"Error loading state: {e}")

    def save_state(self):
        import json
        from datetime import datetime
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
                "winning_exit_time": self.winning_exit_time
            },
            "multipliers": self.app_state["multipliers"],
            "history": self.history
        }
        try:
            with open(self.data_file, 'w') as f:
                json.dump(data, f, indent=4)
        except Exception as e:
            self.log(f"Error saving state: {e}")

    def log(self, message: str):
        print(message)
        self.logs.append(message)
        if len(self.logs) > 1000:
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
                    
                    while self.running:
                        # Check timer
                        if self.stop_time:
                            import time
                            if time.time() > self.stop_time:
                                self.log("Timer expired. Stopping Copy Trader Service...")
                                self.running = False
                                break
                        
                        await self.tick(session)
                        await asyncio.sleep(self.poll_interval)
        except Exception as e:
            self.log(f"Critical Error in Monitor Loop: {e}")
            self.running = False

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
        slug = f"btc-updown-5m-{window_start}"
        
        # Check if already traded this window in logic memory or app state
        if "winning_trades" not in self.app_state:
            self.app_state["winning_trades"] = {}

        # 2. Fetch Market Details
        market_data = await self.get_market_by_slug(slug)
        if not market_data:
            return
            
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

                self.log(f"[Winning Strat] BUY {target_token_id[:10]}... Size: {size_to_buy:.2f} @ {price_found}")
                
                if not self.dry_run:
                    try:
                        order_res = await session.call_tool("place_order", arguments={
                            "market_slug": slug, 
                            "side": "BUY",
                            "size": size_to_buy,
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
        import time
        import datetime
        import pandas as pd
        import random
        import os
        
        now_ts = time.time()
        # Round down to nearest 300s (5 min)
        window_start = int(now_ts // 300) * 300
        slug = f"btc-updown-5m-{window_start}"
        
        if "martingale_state" not in self.app_state:
            self.app_state["martingale_state"] = {
                "active_slug": None,
                "entry_done": False,
                "streak": 0,
                "current_amount": self.martingale_initial_amount,
                "direction": None, # "Yes" or "No" (UP or DOWN)
                "entry_price": 0.0,
                "target_token_id": None
            }
            
        state = self.app_state["martingale_state"]
        
        # Reset state for a new window if active_slug is different and we haven't processed exit
        if state["active_slug"] != slug and not str(state["active_slug"]).startswith("closed_"):
            state["active_slug"] = slug
            state["entry_done"] = False
            state["target_token_id"] = None
            
        # Prevent re-running in a closed window
        if state["active_slug"] == f"closed_{slug}":
            return
            
        elapsed = now_ts - window_start
        
        # ENTRY LOGIC -> between 5 and 60 seconds
        if 5 <= elapsed <= 60 and not state["entry_done"]:
            market_data = await self.get_market_by_slug(slug)
            if not market_data: return
            
            markets = market_data.get('markets', [])
            if not markets: return
            clob_token_ids = markets[0].get('clobTokenIds', [])
            if len(clob_token_ids) != 2: return
            
            # Use random direction if first trade or won previous.
            if state["streak"] == 0 or not state["direction"]:
                state["direction"] = random.choice(["Yes", "No"])
                state["current_amount"] = self.martingale_initial_amount
                
            # Polymarket tokens: UP/Yes is index 0, DOWN/No is index 1 depending on market
            # Typically for up/down markets, 0 is UP (Yes), 1 is DOWN (No). 
            # We assume UP is 0, DOWN is 1. If not, we still bet on the same token.
            token_index = 0 if state["direction"] == "Yes" else 1
            target_token_id = clob_token_ids[token_index]
            state["target_token_id"] = target_token_id
            
            entry_price = await self.fetch_price(target_token_id)
            state["entry_price"] = entry_price
            size_to_buy = state["current_amount"]
            
            self.log(f"[Martingale] ENTRY | slug={slug} | direction={state['direction']} | amt=${size_to_buy} | streak={state['streak']}")
            
            if not self.dry_run:
                try:
                    order_res = await session.call_tool("place_order", arguments={
                        "market_slug": slug, 
                        "side": "BUY",
                        "size": size_to_buy,
                        "price": 0.99, # Aggressive FOK
                        "token_id": target_token_id
                    })
                    self.log(f"[Martingale] Executed Buy: {order_res.content[0].text[:50]}...")
                except Exception as e:
                     self.log(f"[Error] Martingale Execution failed: {e}")
            else:
                self.log("[Martingale] Dry Run - Simulated BUY")
                
            state["entry_done"] = True
            
        # EXIT LOGIC -> between 270s and 300s (4m30s to 5m)
        elif 270 <= elapsed <= 300 and state["entry_done"]:
            target_token_id = state["target_token_id"]
            if not target_token_id:
                return
                
            exit_price = await self.fetch_price(target_token_id)
            
            # Simple heuristic for win/loss before resolution: if price > 0.5 near expiry, it's very likely a win.
            is_win = exit_price >= 0.5
            
            self.log(f"[Martingale] EXIT | slug={slug} | direction={state['direction']} | exit_price={exit_price} | WIN={is_win}")
            
            if not self.dry_run:
                try:
                    order_res = await session.call_tool("place_order", arguments={
                        "market_slug": slug, 
                        "side": "SELL",
                        "size": state["current_amount"],
                        "price": 0.01, # Aggressive FOK Sell
                        "token_id": target_token_id
                    })
                    self.log(f"[Martingale] Executed Sell: {order_res.content[0].text[:50]}...")
                except Exception as e:
                    self.log(f"[Error] Martingale Sell failed: {e}")
            
            # Logging to Excel
            try:
                excel_path = os.path.join(os.path.dirname(__file__), "..", "historical_performance.xlsx")
                new_row = pd.DataFrame([{
                    "Timestamp": datetime.datetime.now(),
                    "Market": slug,
                    "Direction": state["direction"],
                    "Amount": state["current_amount"],
                    "Entry Price": state["entry_price"],
                    "Exit Price": exit_price,
                    "Outcome": "WIN" if is_win else "LOSS",
                    "Streak_Level": state["streak"]
                }])
                
                if os.path.exists(excel_path):
                    df = pd.read_excel(excel_path)
                    df = pd.concat([df, new_row], ignore_index=True)
                else:
                    df = new_row
                df.to_excel(excel_path, index=False)
            except Exception as e:
                self.log(f"[Error] Failed saving to excel: {e}")
            
            # Update state for next window
            if is_win:
                state["streak"] = 0
                state["direction"] = None
                state["current_amount"] = self.martingale_initial_amount
            else:
                state["streak"] += 1
                state["current_amount"] *= 2
                
            state["entry_done"] = False
            state["active_slug"] = f"closed_{slug}"

    async def get_market_by_slug(self, slug: str):
        import aiohttp
        import datetime
        async with aiohttp.ClientSession() as http_session:
            url = f"https://gamma-api.polymarket.com/events?slug={slug}"
            try:
                async with http_session.get(url) as response:
                    if response.status == 200:
                        data = await response.json()
                        if isinstance(data, list) and len(data) > 0:
                            event = data[0]
                            start_str = event.get('startDate') or event.get('startTime')
                            if start_str:
                                start_time = datetime.datetime.fromisoformat(start_str.replace("Z", "+00:00"))
                                return {'start_time': start_time, 'markets': event.get('markets', [])}
            except Exception as e:
                pass
        return None

    async def fetch_price(self, asset_id: str) -> float:
        import aiohttp
        async with aiohttp.ClientSession() as http_session:
            url = f"https://clob.polymarket.com/price?token_id={asset_id}&side=buy"
            try:
                async with http_session.get(url) as response:
                    if response.status == 200:
                        data = await response.json()
                        return float(data.get('price', 0))
            except:
                pass
        return 0.0

    async def get_market_data(self, asset_id: str):
        if asset_id in self.market_cache:
            return self.market_cache[asset_id]
            
        # Fetch from Gamma API
        import aiohttp
        import datetime
        async with aiohttp.ClientSession() as http_session:
            url = f"https://gamma-api.polymarket.com/markets?token_id={asset_id}"
            try:
                async with http_session.get(url) as response:
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
            "martingale_initial_amount": self.martingale_initial_amount
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
