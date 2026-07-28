"""The dashboard API is the highest-risk surface in the repo: it spawns and
kills OS processes and can close real paper positions, with no auth in front
of it. These tests pin the behaviour that keeps that safe — the CSRF guard,
the PID-file liveness check that prevents a second engine, and the fact that
every broker-touching route reports failures instead of swallowing them.

Nothing here may spawn a real process or reach the network: the engine
lifecycle is faked through the lock file, and the broker through a stub.
"""

import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import dashboard_api
from bot.control import load_control

PROJECT_ROOT = Path(__file__).resolve().parent.parent

CSRF_HEADER = {"x-dashboard-client": "test"}

# Every mutating route, so a newly added one can't silently skip the guard.
MUTATING_ROUTES = [
    "/control/bot/start",
    "/control/bot/stop",
    "/control/pause",
    "/control/resume",
    "/orders/abc/cancel",
    "/positions/XYZ/close",
]


class FakePosition:
    def __init__(self, symbol="AAPL260717C00200000"):
        self.symbol = symbol
        self.underlying = "AAPL"
        self.qty = 1
        self.avg_entry_price = 1.0
        self.current_price = 1.5
        self.days_to_expiry = 7


class FakeBroker:
    """Records calls; raises only when a test asks it to."""

    def __init__(self):
        self.canceled: list[str] = []
        self.closed: list[tuple[str, int, float]] = []
        self.positions: list[FakePosition] = []
        self.raise_on_cancel = False
        self.raise_on_close = False

    def get_option_positions(self):
        return self.positions

    def cancel_order(self, order_id):
        if self.raise_on_cancel:
            raise RuntimeError("broker rejected cancel")
        self.canceled.append(order_id)

    def close_option(self, symbol, qty, price):
        if self.raise_on_close:
            raise RuntimeError("broker rejected close")
        self.closed.append((symbol, qty, price))


@pytest.fixture
def api(tmp_path, monkeypatch):
    """A client wired to temp state files, a stub broker, and a stopped
    engine. Popen is blocked outright so a bug can never spawn a real bot."""
    # Settings is frozen, so swap in a whole replacement rather than mutating.
    cfg = replace(
        dashboard_api.cfg,
        lock_file=str(tmp_path / "engine.pid"),
        control_file=str(tmp_path / "control.json"),
    )
    monkeypatch.setattr(dashboard_api, "cfg", cfg)

    # Seed the lazy broker cache so get_broker() never builds a real client.
    broker = FakeBroker()
    monkeypatch.setattr(dashboard_api, "_broker", broker)

    spawned: list[list[str]] = []

    def fake_popen(args, **kwargs):
        spawned.append(args)
        return object()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    terminated: list[bool] = []
    monkeypatch.setattr(dashboard_api, "_terminate_engine",
                        lambda *a, **k: terminated.append(True))

    running = {"value": False}
    monkeypatch.setattr(dashboard_api, "_engine_running", lambda: running["value"])

    with TestClient(dashboard_api.app) as client:
        client.broker = broker
        client.spawned = spawned
        client.terminated = terminated
        client.running = running
        client.cfg = cfg
        yield client


# --- import purity ------------------------------------------------------

def test_module_imports_without_credentials():
    """Importing must not require credentials.

    Building the Alpaca client at module scope made this module unimportable
    without a configured .env — alpaca-py raises on an empty key pair. That is
    why it went untested for so long, and why CI (which has no .env) could not
    even collect this file. Runs in a subprocess with the keys blanked, since
    the parent process has already imported the module.
    """
    env = dict(os.environ)
    env["ALPACA_API_KEY"] = ""
    env["ALPACA_SECRET_KEY"] = ""

    result = subprocess.run(
        [sys.executable, "-c", "import dashboard_api"],
        cwd=str(PROJECT_ROOT),
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


# --- CSRF guard ---------------------------------------------------------

@pytest.mark.parametrize("route", MUTATING_ROUTES)
def test_mutating_route_rejected_without_csrf_header(api, route):
    """The guard is the ONLY thing standing between a random open tab and
    /control/bot/start. Without the header every mutation must 403 — and
    must not have taken effect."""
    response = api.post(route)
    assert response.status_code == 403
    assert "dashboard client header" in response.json()["detail"]
    assert api.spawned == []
    assert api.terminated == []
    assert api.broker.canceled == []
    assert api.broker.closed == []


def test_get_routes_do_not_require_csrf_header(api):
    """Reads are safe and must stay usable without the header, or the guard
    would be indistinguishable from simply breaking the dashboard."""
    assert api.get("/health").status_code == 200


def test_mutating_route_allowed_with_csrf_header(api):
    response = api.post("/control/pause", headers=CSRF_HEADER)
    assert response.status_code == 200
    assert response.json()["paused"] is True


# --- engine lifecycle ---------------------------------------------------

def test_start_spawns_engine_when_not_running(api):
    response = api.post("/control/bot/start", headers=CSRF_HEADER)
    assert response.status_code == 200
    assert response.json() == {
        "ok": True, "engine_running": True, "already_running": False
    }
    assert len(api.spawned) == 1
    assert str(api.spawned[0][1]).endswith("main.py")


def test_start_refuses_to_spawn_a_second_engine(api):
    """The whole reason liveness is read from the PID file: two engines on one
    paper account would race every order. A Start click while one is already
    running must be a no-op, not a second process."""
    api.running["value"] = True

    response = api.post("/control/bot/start", headers=CSRF_HEADER)

    assert response.json() == {
        "ok": True, "engine_running": True, "already_running": True
    }
    assert api.spawned == []


def test_stop_terminates_a_running_engine(api):
    api.running["value"] = True

    response = api.post("/control/bot/stop", headers=CSRF_HEADER)

    assert response.json()["already_stopped"] is False
    assert api.terminated == [True]


def test_stop_is_a_noop_when_already_stopped(api):
    response = api.post("/control/bot/stop", headers=CSRF_HEADER)

    assert response.json() == {
        "ok": True, "engine_running": False, "already_stopped": True
    }
    assert api.terminated == []


# --- pause / resume -----------------------------------------------------

def test_pause_then_resume_round_trips_through_the_control_file(api):
    """The engine reads this file, not the API's memory — so the write has to
    actually land on disk for a pause to reach a running bot."""
    api.post("/control/pause", headers=CSRF_HEADER)
    assert load_control(api.cfg.control_file).paused is True

    api.post("/control/resume", headers=CSRF_HEADER)
    assert load_control(api.cfg.control_file).paused is False


# --- broker-touching routes --------------------------------------------

def test_cancel_order_forwards_the_id_to_the_broker(api):
    response = api.post("/orders/order-123/cancel", headers=CSRF_HEADER)
    assert response.status_code == 200
    assert api.broker.canceled == ["order-123"]


def test_cancel_order_surfaces_broker_failure_as_500(api):
    """A failed cancel must never report ok — the operator would believe a
    live order was pulled when it is still working."""
    api.broker.raise_on_cancel = True

    response = api.post("/orders/order-123/cancel", headers=CSRF_HEADER)

    assert response.status_code == 500
    assert "broker rejected cancel" in response.json()["detail"]


def test_close_position_closes_the_matching_position(api):
    position = FakePosition()
    api.broker.positions = [position]

    response = api.post(f"/positions/{position.symbol}/close", headers=CSRF_HEADER)

    assert response.status_code == 200
    assert api.broker.closed == [(position.symbol, 1, 1.5)]


def test_close_unknown_position_is_404_and_closes_nothing(api):
    """Closing by a symbol the account does not hold must not fall through to
    the broker with a bad symbol."""
    api.broker.positions = [FakePosition()]

    response = api.post("/positions/NOT_HELD/close", headers=CSRF_HEADER)

    assert response.status_code == 404
    assert api.broker.closed == []


def test_close_position_surfaces_broker_failure_as_500(api):
    position = FakePosition()
    api.broker.positions = [position]
    api.broker.raise_on_close = True

    response = api.post(f"/positions/{position.symbol}/close", headers=CSRF_HEADER)

    assert response.status_code == 500
    assert "broker rejected close" in response.json()["detail"]
