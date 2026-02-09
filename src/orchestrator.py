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
            "positions": {}  # tokenId -> size
        }
        self.logs: List[str] = []
        self._task: Optional[asyncio.Task] = None

    def log(self, message: str):
        print(message)
        self.logs.append(message)
        # Keep logs manageable
        if len(self.logs) > 1000:
            self.logs.pop(0)

    async def start(self):
        if self.running:
            return
        self.running = True
        self.log(f"Starting Copy Trader Service...")
        self.log(f"Target: {self.target_wallet} | Dry Run: {self.dry_run} | Interval: {self.poll_interval}s")
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
            
            if isinstance(positions_result.content, list) and len(positions_result.content) > 0:
                data = positions_result.content[0].text
                # Simple log for now, can be parsed later
                self.log(f"[Tick] Positions fetched for {self.target_wallet[:6]}...")
                # data processing logic placeholder
            else:
                self.log(f"[Tick] No content or error fetching positions.")

        except Exception as e:
            self.log(f"Error in tick: {e}")

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
