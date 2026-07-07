"""Daily counters persisted to a JSON file so a restart mid-day keeps limits."""

import json
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path


@dataclass
class DayState:
    day: str                      # ISO date the counters belong to
    trades_today: int = 0
    day_start_equity: float = 0.0


def load_day_state(path: str, today: date, current_equity: float) -> DayState:
    """Load counters for `today`; roll over to a fresh day if the date changed."""
    file = Path(path)
    if file.exists():
        try:
            raw = json.loads(file.read_text())
            state = DayState(**raw)
            if state.day == today.isoformat():
                return state
        except (json.JSONDecodeError, TypeError):
            pass  # corrupt/legacy file -> start fresh
    state = DayState(day=today.isoformat(), trades_today=0, day_start_equity=current_equity)
    save_day_state(path, state)
    return state


def save_day_state(path: str, state: DayState) -> None:
    Path(path).write_text(json.dumps(asdict(state), indent=2))
