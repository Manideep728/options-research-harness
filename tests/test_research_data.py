"""Tests for the research bar cache: splits, embargo, holdout quarantine, and
the regular-session filter.

The bug the session tests pin: Alpaca returns pre- and post-market bars, US
listed options do not trade then, and 6.3% of the cached bars were outside the
session — carrying three of the four trades that made up 89.5% of the pending
candidate's validation edge.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path

from research import data


def _bars(n: int) -> tuple[list[float], list[datetime]]:
    start = datetime(2026, 1, 5, 14, 30, tzinfo=UTC)  # 09:30 ET (EST)
    times = [start + timedelta(minutes=15 * i) for i in range(n)]
    closes = [100.0 + i for i in range(n)]
    return closes, times


def _at(iso: str) -> datetime:
    return datetime.fromisoformat(iso)


# --- session filter ---

def test_in_session_boundaries():
    # Bar timestamps are start-of-bar, so 15:45 ET is the last session bar and
    # 16:00 ET has already crossed into after-hours.
    assert data.in_session(_at("2026-01-05T14:30:00+00:00"))       # 09:30 ET
    assert data.in_session(_at("2026-01-05T20:45:00+00:00"))       # 15:45 ET
    assert not data.in_session(_at("2026-01-05T14:15:00+00:00"))   # 09:15 ET
    assert not data.in_session(_at("2026-01-05T21:00:00+00:00"))   # 16:00 ET
    assert not data.in_session(_at("2026-01-05T13:00:00+00:00"))   # 08:00 ET


def test_in_session_follows_dst_not_a_fixed_utc_offset():
    """The same UTC wall-clock is inside the session in summer and outside it
    in winter. A hard-coded 13:30-20:00 UTC window would get one of these
    wrong for half the year."""
    assert data.in_session(_at("2026-07-15T13:30:00+00:00"))       # 09:30 EDT
    assert not data.in_session(_at("2026-01-15T13:30:00+00:00"))   # 08:30 EST


def test_filter_to_session_drops_outside_bars():
    closes = [1.0, 2.0, 3.0, 4.0]
    times = [_at("2026-01-05T13:00:00+00:00"),   # 08:00 ET — pre-market
             _at("2026-01-05T14:30:00+00:00"),   # 09:30 ET — in
             _at("2026-01-05T20:45:00+00:00"),   # 15:45 ET — in
             _at("2026-01-05T22:45:00+00:00")]   # 17:45 ET — after-hours
    assert data.filter_to_session(closes, times) == ([2.0, 3.0], [times[1], times[2]])


def test_filter_to_session_all_outside_gives_empty():
    times = [_at("2026-01-05T13:00:00+00:00"), _at("2026-01-05T22:45:00+00:00")]
    assert data.filter_to_session([1.0, 2.0], times) == ([], [])


def test_load_bars_repairs_a_cache_written_before_the_filter_existed(tmp_path: Path):
    """The 9 MB cache on disk predates this rule, so filtering has to happen on
    READ too — otherwise every backtest keeps consuming un-tradeable bars until
    someone remembers to refetch."""
    closes = [1.0, 2.0, 3.0]
    times = [_at("2026-01-05T13:00:00+00:00"),   # 08:00 ET
             _at("2026-01-05T14:30:00+00:00"),   # 09:30 ET
             _at("2026-01-05T21:30:00+00:00")]   # 16:30 ET
    data.save_bars(tmp_path / "intraday" / "SPY.csv", closes, times)
    assert data.load_bars("intraday", "SPY", data_dir=tmp_path) == ([2.0], [times[1]])


def test_daily_bars_are_never_session_filtered(tmp_path: Path):
    """Alpaca stamps daily bars at 00:00 ET. Applying the intraday filter to
    them would delete the entire daily cache — and with it the only multi-year
    validation data there is."""
    closes = [100.0, 101.0, 102.0]
    times = [_at("2026-01-05T05:00:00+00:00"),   # 00:00 ET
             _at("2026-01-06T05:00:00+00:00"),
             _at("2026-01-07T05:00:00+00:00")]
    data.save_bars(tmp_path / "daily" / "SPY.csv", closes, times)
    assert data.load_bars("daily", "SPY", data_dir=tmp_path) == (closes, times)


def test_holdout_bars_are_session_filtered(tmp_path: Path):
    closes = [1.0, 2.0]
    times = [_at("2026-01-05T13:00:00+00:00"), _at("2026-01-05T14:30:00+00:00")]
    data.save_bars(tmp_path / "holdout" / "SPY.csv", closes, times)
    assert data.load_holdout_bars("SPY", data_dir=tmp_path) == ([2.0], [times[1]])


def test_split_history_no_overlap_and_embargo_gap():
    closes, times = _bars(100)
    (r_closes, r_times), (h_closes, h_times) = data.split_history(
        closes, times, research_fraction=0.8, embargo=5
    )
    assert len(r_closes) == 80
    assert h_closes == closes[85:]          # 5 embargo bars dropped entirely
    assert r_times[-1] < h_times[0]         # strictly chronological
    assert not set(r_times) & set(h_times)  # no shared bars


def test_split_train_val_embargo():
    closes, times = _bars(100)
    (t_closes, _), (v_closes, v_times) = data.split_train_val(
        closes, times, train_fraction=0.75, embargo=5
    )
    assert len(t_closes) == 75
    assert v_closes == closes[80:]
    assert v_times[0] == times[80]


def test_split_embargo_larger_than_remainder_gives_empty_holdout():
    closes, times = _bars(20)
    _, (h_closes, h_times) = data.split_history(
        closes, times, research_fraction=0.8, embargo=10
    )
    assert h_closes == [] and h_times == []


def test_bars_before_cutoff():
    closes, times = _bars(10)
    kept_closes, kept_times = data.bars_before(closes, times, cutoff=times[6])
    assert kept_closes == closes[:6]
    assert all(t < times[6] for t in kept_times)


def test_bars_before_cutoff_before_all_data_is_empty():
    closes, times = _bars(5)
    early = times[0] - timedelta(days=1)
    assert data.bars_before(closes, times, cutoff=early) == ([], [])


def test_save_load_roundtrip(tmp_path: Path):
    closes, times = _bars(7)
    data.save_bars(tmp_path / "intraday" / "SPY.csv", closes, times)
    loaded_closes, loaded_times = data.load_bars("intraday", "SPY", data_dir=tmp_path)
    assert loaded_closes == closes
    assert loaded_times == times  # tz-aware datetimes survive the roundtrip


def test_load_missing_symbol_degrades_to_empty(tmp_path: Path):
    assert data.load_bars("daily", "NOPE", data_dir=tmp_path) == ([], [])


def test_load_bars_rejects_unknown_kind(tmp_path: Path):
    try:
        data.load_bars("holdout", "SPY", data_dir=tmp_path)
    except ValueError:
        return
    raise AssertionError("load_bars must not reach the holdout directory")


def test_holdout_loader_reads_only_holdout_dir(tmp_path: Path):
    closes, times = _bars(4)
    data.save_bars(tmp_path / "holdout" / "SPY.csv", closes, times)
    assert data.load_holdout_bars("SPY", data_dir=tmp_path) == (closes, times)
    # and the ordinary loader cannot see it
    assert data.load_bars("intraday", "SPY", data_dir=tmp_path) == ([], [])
