"""The active shortlist, persisted to JSON so the dashboard — which runs as a
separate process from the engine — can display exactly what's being polled
without re-ranking the whole universe itself.

Same pattern as state.py / control.py: the engine writes, the dashboard reads,
and a missing or corrupt file degrades to an empty shortlist rather than an
error (the engine simply hasn't run its first scan yet)."""

import json
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from bot.atomic import atomic_write_text


@dataclass(frozen=True)
class ActiveEntry:
    symbol: str
    score: float
    detail: str


@dataclass(frozen=True)
class ActiveShortlist:
    ranked_at: str | None          # ISO timestamp of the last re-rank, or None
    entries: list[ActiveEntry]


def save_active(path: str, ranked_at: datetime, entries: list[ActiveEntry]) -> None:
    payload = {
        "ranked_at": ranked_at.isoformat(),
        "entries": [asdict(e) for e in entries],
    }
    atomic_write_text(path, json.dumps(payload, indent=2))


def load_active(path: str) -> ActiveShortlist:
    file = Path(path)
    if not file.exists():
        return ActiveShortlist(ranked_at=None, entries=[])
    try:
        raw = json.loads(file.read_text())
        entries = [ActiveEntry(**e) for e in raw.get("entries", [])]
        return ActiveShortlist(ranked_at=raw.get("ranked_at"), entries=entries)
    except (json.JSONDecodeError, TypeError, ValueError, KeyError):
        return ActiveShortlist(ranked_at=None, entries=[])
