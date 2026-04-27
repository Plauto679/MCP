from fastapi import FastAPI, WebSocket, Request, Form
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import Optional
import asyncio
import os
from .orchestrator import CopyTrader

app = FastAPI(title="Polymarket Copy Trader Dashboard")

# Initialize Trader with defaults (will be updated via UI)
# Using a default target if not set in environment
# Retrieve target wallets from env (comma separated) or default
DEFAULT_TARGETS = [t.strip() for t in os.getenv("TARGET_WALLETS", "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045").split(",")]
trader = CopyTrader(target_wallets=DEFAULT_TARGETS, dry_run=True)

# Templates
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "templates"))

class ConfigUpdate(BaseModel):
    target_wallets: list[str]
    dry_run: bool
    poll_interval: int
    size_mode: str
    size_value: float
    winning_strategy_enabled: bool = False
    winning_price_threshold: float = 0.75
    winning_entry_time: float = 3.0
    winning_exit_time: float = 4.5
    strategy_mode: str = "copy" # 'copy', 'winning', or 'martingale'
    winning_size_mode: str = "fixed" # 'fixed' or 'percent'
    winning_size_value: float = 10.0
    martingale_initial_amount: float = 5.0

class StartRequest(BaseModel):
    duration: Optional[int] = None

@app.on_event("startup")
async def startup_event():
    # Start the trader automatically or wait for user? Let's wait for user start.
    pass

@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request):
    return templates.TemplateResponse("index.html", {
        "request": request, 
        "target_wallets": trader.target_wallets,
        "dry_run": trader.dry_run,
        "poll_interval": trader.poll_interval,
        "size_mode": trader.size_mode,
        "size_value": trader.size_value,
        "winning_strategy_enabled": trader.winning_strategy_enabled,
        "winning_price_threshold": trader.winning_price_threshold,
        "winning_entry_time": trader.winning_entry_time,
        "winning_exit_time": trader.winning_exit_time,
        "strategy_mode": trader.strategy_mode,
        "winning_size_mode": trader.winning_size_mode,
        "winning_size_value": trader.winning_size_value,
        "martingale_initial_amount": getattr(trader, "martingale_initial_amount", 5.0),
        "running": trader.running
    })

@app.post("/api/start")
async def start_bot(req: StartRequest = None):
    # req might be None if called without body, handle gracefully
    duration = req.duration if req else None
    
    if not trader.running:
        await trader.start(duration_minutes=duration)
    return {"status": "started", "running": True, "duration": duration}

@app.post("/api/stop")
async def stop_bot():
    if trader.running:
        await trader.stop()
    return {"status": "stopped", "running": False}

@app.post("/api/config")
async def update_config(config: ConfigUpdate):
    trader.update_config(
        target_wallets=config.target_wallets, 
        dry_run=config.dry_run, 
        poll_interval=config.poll_interval,
        size_mode=config.size_mode,
        size_value=config.size_value,
        winning_strategy_enabled=config.winning_strategy_enabled,
        winning_price_threshold=config.winning_price_threshold,
        winning_entry_time=config.winning_entry_time,
        winning_exit_time=config.winning_exit_time,
        strategy_mode=config.strategy_mode,
        winning_size_mode=config.winning_size_mode,
        winning_size_value=config.winning_size_value,
        martingale_initial_amount=config.martingale_initial_amount
    )
    # Restart if running to apply new interval effectively in the loop
    if trader.running:
        await trader.stop()
        await trader.start()
    return {"status": "updated", "config": config}

@app.get("/api/logs")
async def get_logs():
    return {"logs": trader.logs[-50:]}

@app.get("/api/history")
async def get_history():
    return {"history": trader.history}

@app.websocket("/ws/logs")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    try:
        last_index = 0
        while True:
            # Check for new logs
            current_len = len(trader.logs)
            if current_len > last_index:
                new_logs = trader.logs[last_index:]
                for log in new_logs:
                    await websocket.send_text(log)
                last_index = current_len
            await asyncio.sleep(0.5)
    except Exception as e:
        print(f"WebSocket disconnected: {e}")
