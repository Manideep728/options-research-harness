"""Local operator control state for the dashboard."""

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from bot.atomic import atomic_write_text


@dataclass
class ControlState:
    paused: bool = False
    updated_at: str = ""


def load_control(path: str) -> ControlState:
    file = Path(path)
    if not file.exists():
        return ControlState()
    try:
        raw = json.loads(file.read_text())
        return ControlState(
            paused=bool(raw.get("paused", False)),
            updated_at=str(raw.get("updated_at", "")),
        )
    except (json.JSONDecodeError, TypeError, ValueError):
        return ControlState()


def save_control(path: str, paused: bool) -> ControlState:
    state = ControlState(paused=paused, updated_at=datetime.now(timezone.utc).isoformat())
    atomic_write_text(path, json.dumps(asdict(state), indent=2))
    return state