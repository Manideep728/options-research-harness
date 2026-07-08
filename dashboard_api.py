"""FastAPI backend for the local trading dashboard."""

import subprocess
import sys
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from bot.broker import AlpacaBroker
from bot.config import Settings
from bot.control import save_control
from bot.dashboard import build_dashboard_snapshot

cfg = Settings.load()
broker = AlpacaBroker(cfg)

ROOT = Path(__file__).resolve().parent
MAIN_SCRIPT = ROOT / "main.py"

app = FastAPI(title="Trading Bot Dashboard API", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# The engine (bot/engine.py's trading loop) runs as its own OS process,
# started with `python main.py`. This API only tracks and controls that
# child process's lifecycle — it never runs the loop itself, so opening the
# dashboard (which boots this API) never starts trading on its own.
_engine_process: subprocess.Popen | None = None


def _engine_running() -> bool:
    return _engine_process is not None and _engine_process.poll() is None


@app.get("/health")
def health() -> dict:
    return {"ok": True}


@app.get("/dashboard")
def dashboard() -> dict:
    snapshot = build_dashboard_snapshot(broker, cfg)
    data = snapshot.__dict__
    data["bot"]["engine_running"] = _engine_running()
    return data


@app.post("/control/bot/start")
def start_bot() -> dict:
    global _engine_process
    if _engine_running():
        return {"ok": True, "engine_running": True, "already_running": True}
    _engine_process = subprocess.Popen([sys.executable, str(MAIN_SCRIPT)], cwd=str(ROOT))
    return {"ok": True, "engine_running": True, "already_running": False}


@app.post("/control/bot/stop")
def stop_bot() -> dict:
    global _engine_process
    if not _engine_running():
        _engine_process = None
        return {"ok": True, "engine_running": False, "already_stopped": True}
    _engine_process.terminate()
    try:
        _engine_process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        _engine_process.kill()
        _engine_process.wait(timeout=5)
    _engine_process = None
    return {"ok": True, "engine_running": False, "already_stopped": False}


@app.post("/control/pause")
def pause() -> dict:
    state = save_control(cfg.control_file, True)
    return {"ok": True, "paused": state.paused, "updated_at": state.updated_at}


@app.post("/control/resume")
def resume() -> dict:
    state = save_control(cfg.control_file, False)
    return {"ok": True, "paused": state.paused, "updated_at": state.updated_at}


@app.post("/orders/{order_id}/cancel")
def cancel_order(order_id: str) -> dict:
    try:
        broker.cancel_order(order_id)
    except Exception as exc:  # pragma: no cover - surfaces broker errors verbatim
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"ok": True, "order_id": order_id}


@app.post("/positions/{symbol}/close")
def close_position(symbol: str) -> dict:
    positions = broker.get_option_positions()
    match = next((p for p in positions if p.symbol == symbol), None)
    if match is None:
        raise HTTPException(status_code=404, detail="position not found")
    try:
        broker.close_option(match.symbol, match.qty, match.current_price)
    except Exception as exc:  # pragma: no cover - surfaces broker errors verbatim
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"ok": True, "symbol": symbol}


@app.on_event("shutdown")
def _stop_engine_on_shutdown() -> None:
    if _engine_running():
        _engine_process.terminate()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)