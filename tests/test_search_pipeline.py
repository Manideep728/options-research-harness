"""What run_family passes down to the P&L engine.

Both of these were missing, and each one on its own was enough to make a coin
flip look profitable:

  1. The high, low and open columns. Without them a barrier touched inside a
     bar is invisible, so every exit books the full close-to-close move —
     unbounded up, floored at -100% down. On the 30-symbol daily cache that
     asymmetry alone put the null at +12.55% per trade; with the columns
     threaded through it is -5.96%.
  2. A per-symbol implied volatility. One constant across a universe
     underprices whatever realizes more than it, and a random buyer collects
     that pricing error as though it were skill.
"""

from datetime import UTC, datetime, timedelta

import pytest

from bot.config import Settings
from bot.simulator import SimParams
from bot.strategy import Action
from research import data, search
from research.families import Family

CFG = Settings(api_key="", secret_key="")
SP = SimParams(iv=0.20, otm_pct=0.01, dte_days=10.0, roundtrip_cost=0.03)
T0 = datetime(2026, 3, 2, 14, 30, tzinfo=UTC)


def _times(n: int, minutes: int = 15) -> list[datetime]:
    return [T0 + timedelta(minutes=minutes * i) for i in range(n)]


def _fires_at(index: int, action=Action.BUY_CALL) -> Family:
    def signal(closes, times, params):
        out = [Action.NONE] * len(closes)
        out[index] = action
        return out
    return Family(name="fires-once", description="", signal=signal, grid={}, bounds={})


# --- the intra-bar columns ---

def test_run_family_uses_the_high_and_low_when_given_full_bars():
    """A bar whose LOW breaches the stop but whose close recovers must exit at
    the barrier. With closes only the position stays open, which is the whole
    +12.55% versus -5.96% difference on the daily cache."""
    n = 40
    closes = [100.0] * n
    times = _times(n)
    bars = data.OhlcBars(times=times, opens=list(closes),
                         highs=list(closes),
                         lows=[100.0] * 20 + [97.0] + [100.0] * (n - 21),
                         closes=list(closes), volumes=[0.0] * n,
                         has_intrabar=True)
    fam = _fires_at(19)

    with_range = search.run_family({"X": bars}, CFG, SP, fam, {})
    closes_only = search.run_family({"X": bars.bars}, CFG, SP, fam, {})

    assert with_range.n == 1
    assert with_range.trades[0].reason == "stop loss"
    assert with_range.trades[0].pnl_pct == pytest.approx(-CFG.stop_loss_pct)
    assert closes_only.trades[0].reason != "stop loss"


def test_as_ohlc_accepts_both_window_shapes():
    closes, times = [1.0, 2.0], _times(2)
    wrapped = search.as_ohlc((closes, times))
    assert wrapped.closes == closes
    assert not wrapped.has_intrabar        # a pair carries no real range
    assert wrapped.highs == closes and wrapped.lows == closes

    full = data.OhlcBars(times, [1.0, 2.0], [3.0, 4.0], [0.5, 1.0],
                         closes, [0.0, 0.0], has_intrabar=True)
    assert search.as_ohlc(full) is full


# --- per-symbol implied volatility ---

def test_run_family_prices_each_symbol_at_its_own_implied_vol():
    """If `ivs` were ignored these two runs would be identical. A dearer option
    needs a bigger move to return the same percentage."""
    n = 40
    closes = [100.0] * 20 + [101.0] * (n - 20)
    times = _times(n)
    windows = {"X": (closes, times)}
    fam = _fires_at(19)

    cheap = search.run_family(windows, CFG, SP, fam, {}, ivs={"X": [0.10] * n})
    dear = search.run_family(windows, CFG, SP, fam, {}, ivs={"X": [0.60] * n})
    assert cheap.n == dear.n == 1
    assert cheap.trades[0].pnl_pct > dear.trades[0].pnl_pct


def test_a_symbol_missing_from_ivs_falls_back_to_the_constant():
    n = 40
    windows = {"X": ([100.0] * n, _times(n))}
    fam = _fires_at(19)
    supplied = search.run_family(windows, CFG, SP, fam, {}, ivs={"OTHER": [0.9] * n})
    plain = search.run_family(windows, CFG, SP, fam, {})
    assert [t.pnl_pct for t in supplied.trades] == [t.pnl_pct for t in plain.trades]


# --- the implied vol series itself ---

def _write_index(tmp_path, name="VIX", values=((1, 15.0), (2, 16.0), (5, 20.0))):
    days = [datetime(2026, 3, d, 5, 0, tzinfo=UTC) for d, _ in values]
    data.save_bars(tmp_path / "volidx" / f"{name}.csv", [v for _, v in values], days)


def test_implied_vol_series_aligns_by_day_and_carries_forward(tmp_path):
    """The index publishes a daily close; bars are intraday and some days have
    no print. A value is carried FORWARD only — pricing a bar with a volatility
    from later that week would be a look-ahead leak."""
    _write_index(tmp_path)
    times = [datetime(2026, 3, d, 15, 0, tzinfo=UTC) for d in (1, 2, 3, 4, 5, 6)]
    series = data.implied_vol_series("SPY", times, data_dir=tmp_path)
    assert series == [0.15, 0.16, 0.16, 0.16, 0.20, 0.20]


def test_implied_vol_series_is_none_when_the_symbol_has_no_index(tmp_path):
    """Silently substituting a constant is what made a coin flip earn 10% per
    trade. A caller must be able to see that no real series exists."""
    _write_index(tmp_path)
    assert data.implied_vol_series("AAPL", _times(3), data_dir=tmp_path) is None


def test_implied_vol_series_is_none_when_the_index_starts_too_late(tmp_path):
    _write_index(tmp_path, values=((20, 15.0),))
    times = [datetime(2026, 3, d, 15, 0, tzinfo=UTC) for d in (1, 2)]
    assert data.implied_vol_series("SPY", times, data_dir=tmp_path) is None


def test_implied_vol_series_is_none_without_a_cached_index(tmp_path):
    assert data.implied_vol_series("SPY", _times(3), data_dir=tmp_path) is None


# --- the OHLC split ---

def test_split_all_ohlc_keeps_one_shared_cutoff_across_every_column():
    n = 100
    times = _times(n)
    bars = {
        "LONG": data.OhlcBars(times, [1.0] * n, [2.0] * n, [0.5] * n,
                              [float(i) for i in range(n)], [0.0] * n, True),
        "SHORT": data.OhlcBars(times[:40], [1.0] * 40, [2.0] * 40, [0.5] * 40,
                               [float(i) for i in range(40)], [0.0] * 40, True),
    }
    train, val = data.split_all_ohlc(bars, fraction=0.75, embargo=0)
    last_train = max(b.times[-1] for b in train.values() if len(b))
    first_val = min(b.times[0] for b in val.values() if len(b))
    assert last_train < first_val
    for symbol in bars:
        assert len(train[symbol].highs) == len(train[symbol].closes)
        assert len(val[symbol].lows) == len(val[symbol].closes)


def test_split_all_ohlc_on_empty_input():
    assert data.split_all_ohlc({}, fraction=0.75) == ({}, {})
