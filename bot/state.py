"""Daily counters persisted to a JSON file so a restart mid-day keeps limits."""

import json
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path

from bot.atomic import atomic_write_text


@dataclass
class DayState:
    day: str                      # ISO date the counters belong to
    trades_today: int = 0
    day_start_equity: float = 0.0


def load_day_state(path: str, today: date, current_equity: float) -> DayState:
    """Load counters for `today`; roll over to a fresh day if the date
    changed, persisting the rollover so a restart mid-day keeps the daily
    limits. The engine is the only caller that should use this (single-writer
    principle) — read-only callers (e.g. the dashboard) should call
    peek_day_state instead, which never writes."""
    state, is_fresh = _read_or_init(path, today, current_equity)
    if is_fresh:
        save_day_state(path, state)
    return state


def peek_day_state(path: str, today: date, current_equity: float) -> DayState:
    """Same lookup as load_day_state but never writes — safe for a second
    process (the dashboard) to call without racing the engine's own writes."""
    state, _ = _read_or_init(path, today, current_equity)
    return state


def _read_or_init(path: str, today: date, current_equity: float) -> tuple[DayState, bool]:
    file = Path(path)
    if file.exists():
        try:
            raw = json.loads(file.read_text())
            state = DayState(**raw)
            if state.day == today.isoformat():
                return state, False
        except (json.JSONDecodeError, TypeError):
            pass  # corrupt/legacy file -> start fresh
    return DayState(day=today.isoformat(), trades_today=0, day_start_equity=current_equity), True


def save_day_state(path: str, state: DayState) -> None:
    atomic_write_text(path, json.dumps(asdict(state), indent=2))
