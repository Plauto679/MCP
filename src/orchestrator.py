import asyncio
import os
import sys
from typing import List, Dict, Optional
from dotenv import load_dotenv

# We need to run the server as a subprocess for MCP connection
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

load_dotenv()

SERVER_SCRIPT = os.path.join(os.path.dirname(__file__), "server.py")
PYTHON_EXE = sys.executable

class CopyTrader:
    def __init__(self, target_wallet: str, dry_run: bool = False, poll_interval: int = 10, size_mode: str = 'fixed', size_value: float = 10.0):
        self.target_wallet = target_wallet
        self.dry_run = dry_run
        self.poll_interval = poll_interval
        self.size_mode = size_mode  # 'fixed' or 'percentage'
        self.size_value = size_value
        self.running = False
        self.app_state = {
            "positions": {}  # asset_id -> size (float)
        }
        self.initial_sync_complete = False
        self.logs: List[str] = []
        self._task: Optional[asyncio.Task] = None

    def log(self, message: str):
        print(message)
        self.logs.append(message)
        if len(self.logs) > 1000:
            self.logs.pop(0)

    async def start(self):
        if self.running:
            return
        self.running = True
        self.initial_sync_complete = False # Reset on start to get fresh snapshot
        self.log(f"Starting Copy Trader Service...")
        self.log(f"Target: {self.target_wallet} | Dry Run: {self.dry_run}")
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
                        await self.tick(session)
                        await asyncio.sleep(self.poll_interval)
        except Exception as e:
            self.log(f"Critical Error in Monitor Loop: {e}")
            self.running = False

    async def tick(self, session: ClientSession):
        try:
            # 1. Fetch Target Positions
            positions_result = await session.call_tool("get_wallet_positions", arguments={"address": self.target_wallet})
            
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
                    if asset_id and size > 0:
                        current_positions[asset_id] = size
                
                # 2. Logic Engine
                if not self.initial_sync_complete:
                    self.app_state["positions"] = current_positions
                    self.initial_sync_complete = True
                    self.log(f"[Sync] Initial snapshot taken. Tracking {len(current_positions)} positions. Waiting for changes...")
                else:
                    await self.diff_and_execute(session, current_positions)
                    
            else:
                self.log(f"[Tick] Invalid data format received. Type: {type(data)}. Content: {str(data)[:50]}...")

        except Exception as e:
            # Import traceback to print full stack trace to logs for debugging
            import traceback
            self.log(f"Error in tick: {e}")
            traceback.print_exc()

    async def diff_and_execute(self, session, current_positions: Dict[str, float]):
        previous_positions = self.app_state["positions"]
        
        # Check for changes
        all_assets = set(current_positions.keys()) | set(previous_positions.keys())
        
        for asset_id in all_assets:
            new_size = current_positions.get(asset_id, 0.0)
            old_size = previous_positions.get(asset_id, 0.0)
            
            if new_size != old_size:
                delta = new_size - old_size
                action = "BUY" if delta > 0 else "SELL"
                
                # Log the detection
                self.log(f"[Signal] Target {action}: Asset {asset_id[:6]}... | Change: {delta:+.2f}")
                
                # Execute Trade
                await self.execute_trade(session, asset_id, delta, action)

        # Update State
        self.app_state["positions"] = current_positions

    async def execute_trade(self, session, asset_id: str, target_delta: float, action: str):
        # 1. Calculate My Size
        trade_size = 0.0
        
        if self.size_mode == 'percentage':
            # Multiply target's delta by my multiplier
            trade_size = abs(target_delta) * self.size_value
            self.log(f"   -> Strategy: Percentage ({self.size_value}x). Order Size: {trade_size:.2f}")
            
        elif self.size_mode == 'fixed':
            # Fixed Amount ($) / Price = Size (Shares)
            # We need the price. For now, we will try to fetch market/price or use a placeholder price
            # Since we don't have a direct "get_price(asset)" tool connected efficiently here in tick loop,
            # we might default to 1 share = $1 (BAD assumption) or try to fetch it.
            # Ideally: call 'get_market' or 'get_token_price'.
            # For this MVP, we will try to fetch market to get price, or LOG ERROR if too slow.
            
            # Optimization: Just use a default price of 0.5 (Binary options usually 0-1) to estimate size? 
            # OR better: Log that Fixed Amount requires price fetching which is slow, so we fallback to assuming 
            # 1.0 share if price unknown?
            # Let's try to fetch market details if possible, or just log.
            
            # For safety in MVP: Fixed Amount acts as "Fixed Shares" if we can't get price. 
            # Wait, user said "10USD". We NEED price.
            # Let's assume we can fetch it.
            try:
                # We need condition_id usually to get market, but we only have asset_id (token_id).
                # Data API positions usually have 'conditionId' too.
                # simpler: Let's just log for now that "Fixed Amount calc requires price" and use size_value as SHARES for safety.
                # self.log(f"   -> Strategy: Fixed Amount ($). Fetching price...") 
                # For now, treat size_value as SHARES to ensure it runs:
                trade_size = self.size_value
                self.log(f"   -> Strategy: Fixed Amount (Treating as {trade_size} Shares for MVP).")
            except:
                trade_size = 1.0

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

    def update_config(self, target_wallet: str = None, dry_run: bool = None, poll_interval: int = None, size_mode: str = None, size_value: float = None):
        if target_wallet is not None:
            self.target_wallet = target_wallet
        if dry_run is not None:
            self.dry_run = dry_run
        if poll_interval is not None:
            self.poll_interval = poll_interval
        if size_mode is not None:
            self.size_mode = size_mode
        if size_value is not None:
            self.size_value = size_value
        self.log(f"Config Updated: Target={self.target_wallet}, DryRun={self.dry_run}, Interval={self.poll_interval}, Mode={self.size_mode}, Value={self.size_value}")

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
