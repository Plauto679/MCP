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
        if target_wallets is None:
            if target_wallet:
                self.target_wallets = [target_wallet]
            else:
                self.target_wallets = []
        else:
            self.target_wallets = target_wallets
            
        self.dry_run = dry_run
        self.poll_interval = poll_interval
        self.size_mode = size_mode  # 'fixed' or 'percentage'
        self.size_value = size_value
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
                "size_value": self.size_value
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
                        # We mark sync complete after first full loop? 
                        # Actually we should track sync state PER wallet or just accept first run behavior.
                        # For simplicity, let's keep one global flag but only set it True after loop finishes?
                        # Or simpler: if 'positions' for this wallet was empty/missing, consider it a sync.
                        self.log(f"[Sync] {target_wallet[:6]}... : Tracking {len(current_positions)} positions.")
                    else:
                        await self.diff_and_execute(session, target_wallet, current_positions)
                        
                else:
                    self.log(f"[Tick] Invalid data format received for {target_wallet[:6]}...")
            
            # After processing all wallets
            self.initial_sync_complete = True

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

    def update_config(self, target_wallets: List[str] = None, dry_run: bool = None, poll_interval: int = None, size_mode: str = None, size_value: float = None):
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
        self.log(f"Config Updated: Targets={len(self.target_wallets)}, DryRun={self.dry_run}, Interval={self.poll_interval}, Mode={self.size_mode}, Value={self.size_value}")
        
        # Save to history
        from datetime import datetime
        entry = {
            "timestamp": datetime.now().isoformat(),
            "target_wallets": self.target_wallets,
            "dry_run": self.dry_run,
            "poll_interval": self.poll_interval,
            "size_mode": self.size_mode,
            "size_value": self.size_value
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
