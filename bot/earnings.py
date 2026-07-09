"""Earnings blackout: don't enter a single name about to report.

We hold options up to max_hold_days across overnights, so an earnings gap can
open straight through the stop-loss. This gate skips entries on any symbol
whose earnings date is within cfg.earnings_blackout_days of today.

Data source: a hand-maintained JSON map { "AAPL": "2026-07-31", ... }. Alpaca
has no clean earnings feed, so you update this file each cycle of the season.
ETFs and any symbol absent from the map are never blacked out — the safe
default, since ETFs have no earnings.
"""

import json
import logging
from datetime import date
from pathlib import Path

log = logging.getLogger("bot.earnings")


def load_earnings_dates(path: str) -> dict[str, date]:
    """Read the symbol -> earnings-date map. A missing or unreadable file is
    treated as 'no known earnings' (empty map) rather than an error, so the
    bot keeps trading if the file is absent."""
    file = Path(path)
    if not file.exists():
        return {}
    try:
        raw = json.loads(file.read_text())
    except (json.JSONDecodeError, OSError) as e:
        log.warning("ignoring unreadable %s: %s", path, e)
        return {}

    out: dict[str, date] = {}
    for symbol, iso in raw.items():
        try:
            out[symbol.upper()] = date.fromisoformat(iso)
        except (ValueError, TypeError):
            log.warning("skipping bad earnings date for %s: %r", symbol, iso)
    return out


def in_blackout(symbol: str, today: date, dates: dict[str, date], window_days: int) -> bool:
    """True if `symbol`'s earnings fall within +/- window_days of today.

    Symmetric on purpose: the days *before* earnings risk holding into the
    report; the days *after* risk entering just as a fresh gap is still
    settling. A symbol with no known date is never in blackout."""
    earnings = dates.get(symbol.upper())
    if earnings is None:
        return False
    return abs((earnings - today).days) <= window_days
