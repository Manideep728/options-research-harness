"""Earnings blackout tests."""

import json
from datetime import date

from bot.earnings import in_blackout, load_earnings_dates

TODAY = date(2026, 7, 9)


def test_missing_file_returns_empty(tmp_path):
    assert load_earnings_dates(str(tmp_path / "nope.json")) == {}


def test_load_parses_dates_and_skips_bad(tmp_path):
    f = tmp_path / "earnings.json"
    f.write_text(json.dumps({"AAPL": "2026-07-31", "MSFT": "not-a-date"}))
    dates = load_earnings_dates(str(f))
    assert dates == {"AAPL": date(2026, 7, 31)}  # bad entry silently dropped


def test_unknown_symbol_is_never_in_blackout():
    assert in_blackout("SPY", TODAY, {"AAPL": date(2026, 7, 10)}, 3) is False


def test_in_blackout_when_earnings_within_window():
    # Earnings 2 days out, window 3 -> blacked out (we'd hold into it).
    assert in_blackout("AAPL", TODAY, {"AAPL": date(2026, 7, 11)}, 3) is True


def test_not_in_blackout_when_earnings_outside_window():
    assert in_blackout("AAPL", TODAY, {"AAPL": date(2026, 7, 20)}, 3) is False


def test_blackout_is_symmetric_around_earnings():
    # Earnings 2 days AGO also blacks out — a fresh gap may still be settling.
    assert in_blackout("AAPL", TODAY, {"AAPL": date(2026, 7, 7)}, 3) is True
