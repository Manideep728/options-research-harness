"""FastAPI backend for the local trading dashboard."""

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from bot.broker import AlpacaBroker
from bot.config import Settings
from bot.control import save_control
from bot.dashboard import build_dashboard_snapshot

cfg = Settings.load()
broker = AlpacaBroker(cfg)

app = FastAPI(title="Trading Bot Dashboard API", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health() -> dict:
    return {"ok": True}


@app.get("/dashboard")
def dashboard() -> dict:
    snapshot = build_dashboard_snapshot(broker, cfg)
    return snapshot.__dict__


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


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)