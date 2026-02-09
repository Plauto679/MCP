from fastapi import FastAPI, WebSocket, Request, Form
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import asyncio
import os
from .orchestrator import CopyTrader

app = FastAPI(title="Polymarket Copy Trader Dashboard")

# Initialize Trader with defaults (will be updated via UI)
# Using a default target if not set in environment
DEFAULT_TARGET = "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045" 
trader = CopyTrader(target_wallet=DEFAULT_TARGET, dry_run=True)

# Templates
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "templates"))

class ConfigUpdate(BaseModel):
    target_wallet: str
    dry_run: bool
    poll_interval: int
    size_mode: str
    size_value: float

@app.on_event("startup")
async def startup_event():
    # Start the trader automatically or wait for user? Let's wait for user start.
    pass

@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request):
    return templates.TemplateResponse("index.html", {
        "request": request, 
        "target_wallet": trader.target_wallet,
        "dry_run": trader.dry_run,
        "poll_interval": trader.poll_interval,
        "size_mode": trader.size_mode,
        "size_value": trader.size_value,
        "running": trader.running
    })

@app.post("/api/start")
async def start_bot():
    if not trader.running:
        await trader.start()
    return {"status": "started", "running": True}

@app.post("/api/stop")
async def stop_bot():
    if trader.running:
        await trader.stop()
    return {"status": "stopped", "running": False}

@app.post("/api/config")
async def update_config(config: ConfigUpdate):
    trader.update_config(
        target_wallet=config.target_wallet, 
        dry_run=config.dry_run, 
        poll_interval=config.poll_interval,
        size_mode=config.size_mode,
        size_value=config.size_value
    )
    # Restart if running to apply new interval effectively in the loop
    if trader.running:
        await trader.stop()
        await trader.start()
    return {"status": "updated", "config": config}

@app.get("/api/logs")
async def get_logs():
    return {"logs": trader.logs[-50:]}

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
