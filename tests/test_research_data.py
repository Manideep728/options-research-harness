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


def test_split_at_no_overlap_and_embargo_gap():
    closes, times = _bars(100)
    (r_closes, r_times), (h_closes, h_times) = data.split_at(
        closes, times, cutoff=times[80], embargo=5
    )
    assert len(r_closes) == 80
    assert h_closes == closes[85:]          # 5 embargo bars dropped entirely
    assert r_times[-1] < h_times[0]         # strictly chronological
    assert not set(r_times) & set(h_times)  # no shared bars


def test_split_embargo_larger_than_remainder_gives_empty_holdout():
    closes, times = _bars(20)
    _, (h_closes, h_times) = data.split_at(
        closes, times, cutoff=times[16], embargo=10
    )
    assert h_closes == [] and h_times == []


def test_union_cutoff_is_one_timestamp_for_every_symbol():
    """THE bug this replaces: cutting each symbol at a fraction of its OWN bar
    count. Real bar counts ranged 5,043 to 6,738, so the train/val cut landed on
    9 different dates and the same market move sat in one symbol's train window
    and another's validation window. A per-symbol fraction cannot fix that; a
    shared cutoff can."""
    long_closes, long_times = _bars(100)
    short_closes, short_times = _bars(40)   # same start, far fewer bars
    bars_by_symbol = {"LONG": (long_closes, long_times),
                      "SHORT": (short_closes, short_times)}

    train_w, val_w = data.split_all(bars_by_symbol, fraction=0.75, embargo=0)

    # Every symbol's training window ends before every symbol's validation
    # window begins — across symbols, not just within one.
    last_train = max(times[-1] for _, times in train_w.values() if times)
    first_val = min(times[0] for _, times in val_w.values() if times)
    assert last_train < first_val

    # And the old per-symbol-fraction behaviour would NOT have held that:
    # 75% of 100 bars is a much later timestamp than 75% of 40 bars.
    assert long_times[75] > short_times[30]


def test_union_cutoff_pools_bars_rather_than_symbols():
    """The cutoff is a quantile of the pooled timeline, so a symbol with more
    bars pulls it later — that is correct, because the quantile is about how
    much DATA is on each side, not how many tickers."""
    _, dense = _bars(100)
    sparse_closes, sparse_times = [1.0, 2.0], [dense[0], dense[-1]]
    cutoff = data.union_cutoff({"DENSE": dense, "SPARSE": sparse_times}, 0.5)
    assert cutoff is not None
    assert dense[40] <= cutoff <= dense[60]
    assert data.union_cutoff({}, 0.5) is None
    assert data.union_cutoff({"EMPTY": []}, 0.5) is None
    assert sparse_closes == [1.0, 2.0]      # fixture untouched


def test_split_all_on_empty_input_is_empty_not_a_crash():
    assert data.split_all({}, fraction=0.75) == ({}, {})


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


# --- unadjusted splits ---

def test_suspicious_moves_flags_a_split_and_ignores_a_real_crash():
    """The live defect this was written for: the daily cache still held
    GOOGL 2235.49 -> 109.05 (-95.1%), its 20:1 split, years after the fetch
    started sending Adjustment.ALL. A split cannot be repaired on read, so the
    only defence is to refuse to load it quietly.

    A genuine crash must NOT be flagged. Deciding that a real move was a data
    defect is its own kind of lie."""
    times = _days_ = [_at(f"2026-01-{d:02d}T05:00:00+00:00") for d in (5, 6, 7, 8)]
    split = [2235.49, 109.05, 110.0, 111.0]
    flagged = data.suspicious_moves(split, times)
    assert len(flagged) == 1
    assert flagged[0][0] == times[1]
    assert flagged[0][1] < -0.9

    covid = [100.0, 88.0, 95.0, 82.0]        # -12%, +8%, -14%: all real
    assert data.suspicious_moves(covid, _days_) == []


def test_suspicious_moves_handles_degenerate_series():
    assert data.suspicious_moves([], []) == []
    assert data.suspicious_moves([100.0], [_at("2026-01-05T05:00:00+00:00")]) == []


def test_loading_a_split_corrupted_cache_warns(tmp_path: Path, caplog):
    """Silence is what let this survive. Loading must say so."""
    times = [_at(f"2026-01-{d:02d}T05:00:00+00:00") for d in (5, 6)]
    data.save_bars(tmp_path / "daily" / "GOOGL.csv", [2235.49, 109.05], times)
    with caplog.at_level("WARNING"):
        data.load_ohlc("daily", "GOOGL", data_dir=tmp_path)
    assert "unadjusted split" in caplog.text
    assert "GOOGL" in caplog.text


def test_clean_data_loads_without_a_warning(tmp_path: Path, caplog):
    closes, times = _bars(10)
    data.save_bars(tmp_path / "daily" / "SPY.csv", closes, times)
    with caplog.at_level("WARNING"):
        data.load_ohlc("daily", "SPY", data_dir=tmp_path)
    assert "unadjusted split" not in caplog.text


# --- full OHLC bars ---

def _ohlc(n: int) -> data.OhlcBars:
    _, times = _bars(n)
    return data.OhlcBars(
        times=times,
        opens=[100.0 + i for i in range(n)],
        highs=[100.5 + i for i in range(n)],
        lows=[99.5 + i for i in range(n)],
        closes=[100.2 + i for i in range(n)],
        volumes=[1000.0 + i for i in range(n)],
        has_intrabar=True,
    )


def test_take_selects_the_same_rows_from_every_column():
    """Splitting and filtering pick rows. Applying the choice column by column
    is how one series ends up offset from another."""
    kept = _ohlc(6).take([0, 3, 5])
    assert kept.closes == [100.2, 103.2, 105.2]
    assert kept.highs == [100.5, 103.5, 105.5]
    assert kept.lows == [99.5, 102.5, 104.5]
    assert kept.volumes == [1000.0, 1003.0, 1005.0]
    assert len(kept.times) == 3
    assert kept.has_intrabar


def test_ohlc_roundtrips_through_the_cache(tmp_path: Path):
    bars = _ohlc(5)
    data.save_ohlc(tmp_path / "daily" / "SPY.csv", bars)
    loaded = data.load_ohlc("daily", "SPY", data_dir=tmp_path)
    assert loaded.closes == bars.closes
    assert loaded.highs == bars.highs
    assert loaded.lows == bars.lows
    assert loaded.volumes == bars.volumes
    assert loaded.has_intrabar


def test_close_only_cache_still_loads_and_says_it_has_no_intrabar(tmp_path: Path):
    """The 9 MB cache on disk predates the open/high/low columns. It must keep
    working, and a caller must be able to tell that its high and low are copies
    of the close rather than a real range — otherwise intra-bar barrier logic
    silently degrades back to close-only guessing."""
    closes, times = _bars(4)
    data.save_bars(tmp_path / "daily" / "SPY.csv", closes, times)
    loaded = data.load_ohlc("daily", "SPY", data_dir=tmp_path)
    assert loaded.closes == closes
    assert loaded.highs == closes and loaded.lows == closes
    assert not loaded.has_intrabar


def test_load_bars_is_unchanged_by_the_ohlc_schema(tmp_path: Path):
    """Every existing caller reads (closes, times) and must not notice."""
    bars = _ohlc(5)
    data.save_ohlc(tmp_path / "daily" / "SPY.csv", bars)
    assert data.load_bars("daily", "SPY", data_dir=tmp_path) == (bars.closes, bars.times)


def test_ohlc_loader_keeps_the_holdout_kind_guard(tmp_path: Path):
    try:
        data.load_ohlc("holdout", "SPY", data_dir=tmp_path)
    except ValueError:
        return
    raise AssertionError("load_ohlc must not reach the holdout directory")


def test_filter_ohlc_to_session_drops_whole_rows():
    times = [_at("2026-01-05T13:00:00+00:00"),   # 08:00 ET — out
             _at("2026-01-05T14:30:00+00:00"),   # 09:30 ET — in
             _at("2026-01-05T22:45:00+00:00")]   # 17:45 ET — out
    bars = data.OhlcBars(times=times, opens=[1.0, 2.0, 3.0], highs=[1.1, 2.1, 3.1],
                         lows=[0.9, 1.9, 2.9], closes=[1.0, 2.0, 3.0],
                         volumes=[10.0, 20.0, 30.0], has_intrabar=True)
    kept = data.filter_ohlc_to_session(bars)
    assert kept.closes == [2.0] and kept.highs == [2.1] and kept.lows == [1.9]


def test_split_indices_agrees_with_split_at():
    """split_at now delegates to split_indices. A divergence would move the
    train/validation boundary without any test noticing."""
    closes, times = _bars(100)
    before, after = data.split_indices(times, cutoff=times[80], embargo=5)
    (r_closes, _), (h_closes, _) = data.split_at(closes, times, times[80], embargo=5)
    assert [closes[i] for i in before] == r_closes
    assert [closes[i] for i in after] == h_closes


def test_ohlc_before_cutoff_trims_every_column():
    bars = _ohlc(10)
    kept = data.ohlc_before(bars, cutoff=bars.times[6])
    assert len(kept) == 6
    assert kept.highs == bars.highs[:6]


def test_holdout_loader_reads_only_holdout_dir(tmp_path: Path):
    closes, times = _bars(4)
    data.save_bars(tmp_path / "holdout" / "SPY.csv", closes, times)
    assert data.load_holdout_bars("SPY", data_dir=tmp_path) == (closes, times)
    # and the ordinary loader cannot see it
    assert data.load_bars("intraday", "SPY", data_dir=tmp_path) == ([], [])
