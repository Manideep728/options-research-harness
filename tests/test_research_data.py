"""Tests for the research bar cache: splits, embargo, holdout quarantine."""

from datetime import datetime, timedelta, timezone
from pathlib import Path

from research import data


def _bars(n: int) -> tuple[list[float], list[datetime]]:
    start = datetime(2026, 1, 5, 14, 30, tzinfo=timezone.utc)
    times = [start + timedelta(minutes=15 * i) for i in range(n)]
    closes = [100.0 + i for i in range(n)]
    return closes, times


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
