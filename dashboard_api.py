"""FastAPI backend for the local trading dashboard."""

import os
import subprocess
import sys
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from bot import lock
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

# CSRF guard: this API binds to localhost and has no auth, so without this any
# website a browser has open could POST to /control/bot/start, /positions/*
# /close, etc. — CORS alone doesn't stop that, since it only governs whether
# the calling page may *read* the response, not whether the browser *sends*
# the request. Requiring a custom header forces the browser to run a CORS
# preflight for every mutating request, and our allow_origins above rejects
# that preflight for any origin except the dashboard itself — so a
# cross-origin page can no longer get a state-changing request through, with
# or without JS reading the reply.
_CSRF_HEADER = "x-dashboard-client"


@app.middleware("http")
async def require_dashboard_header(request: Request, call_next):
    if request.method in ("POST", "PUT", "PATCH", "DELETE") and _CSRF_HEADER not in request.headers:
        return JSONResponse(status_code=403, content={"detail": "missing dashboard client header"})
    return await call_next(request)

# The engine (bot/engine.py's trading loop) runs as its own OS process,
# started with `python main.py`. This API only tracks and controls that
# child process's lifecycle — it never runs the loop itself, so opening the
# dashboard (which boots this API) never starts trading on its own.
#
# Liveness is checked via the engine's PID file (bot/lock.py), not this
# process's own subprocess.Popen handle: the handle is lost whenever the API
# restarts, which would otherwise make a still-running engine look "stopped"
# and let a Start click spawn a second one racing the same account.
_engine_process: subprocess.Popen | None = None


def _engine_running() -> bool:
    return lock.is_running(cfg.lock_file)


def _terminate_engine(timeout_sec: float = 5.0) -> None:
    """Stop the engine by PID, however it was started (this API's own
    subprocess, a previous API instance, or a manually-run `python main.py`)."""
    pid = lock.read_pid(cfg.lock_file)
    if pid is None:
        return
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True, check=False,
        )
    else:
        import signal

        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return
    deadline = time.monotonic() + timeout_sec
    while lock.is_running(cfg.lock_file) and time.monotonic() < deadline:
        time.sleep(0.2)


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
    _terminate_engine()
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
        _terminate_engine()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)