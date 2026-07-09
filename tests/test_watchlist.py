"""Active-shortlist persistence tests."""

from datetime import datetime, timezone

from bot.watchlist import ActiveEntry, load_active, save_active

RANKED_AT = datetime(2026, 7, 9, 15, 0, tzinfo=timezone.utc)


def test_missing_file_is_empty_shortlist(tmp_path):
    sl = load_active(str(tmp_path / "nope.json"))
    assert sl.ranked_at is None
    assert sl.entries == []


def test_save_then_load_round_trips(tmp_path):
    path = str(tmp_path / "active.json")
    entries = [ActiveEntry("SPY", 0.0123, "trend=0.5"), ActiveEntry("NVDA", 0.009, "x")]
    save_active(path, RANKED_AT, entries)
    sl = load_active(path)
    assert sl.ranked_at == RANKED_AT.isoformat()
    assert sl.entries == entries


def test_corrupt_file_degrades_to_empty(tmp_path):
    path = tmp_path / "active.json"
    path.write_text("{not valid json")
    assert load_active(str(path)).entries == []


def test_empty_shortlist_round_trips(tmp_path):
    # An empty shortlist (e.g. everything blacked out) is a valid state.
    path = str(tmp_path / "active.json")
    save_active(path, RANKED_AT, [])
    sl = load_active(path)
    assert sl.entries == []
    assert sl.ranked_at == RANKED_AT.isoformat()
