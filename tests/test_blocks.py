"""Spec vocabulary: validation, clamping, candidate expansion, composition."""

from datetime import datetime, timedelta, timezone

from bot.strategy import Action

from research import blocks, families


def _spec(**overrides) -> dict:
    base = {
        "name": "test-spec",
        "hypothesis": "testing",
        "trend": "ema_cross",
        "trigger": "rsi_cross",
        "filters": [],
        "grid": {
            "ema_fast": [9], "ema_slow": [21],
            "rsi_bull_level": [45.0], "rsi_bear_level": [55.0],
        },
    }
    base.update(overrides)
    return base


def _sawtooth(n: int = 240) -> tuple[list[float], list[datetime]]:
    closes: list[float] = []
    while len(closes) < n:
        closes += [100.0 + j for j in range(12)]
        closes += [111.0 - j for j in range(12)]
    start = datetime(2026, 1, 5, 14, 30, tzinfo=timezone.utc)
    return closes[:n], [start + timedelta(minutes=15 * i) for i in range(n)]


# --- validation ---

def test_valid_spec_passes():
    assert blocks.validate_spec(_spec()) == []


def test_unknown_blocks_rejected():
    assert blocks.validate_spec(_spec(trigger="macd"))
    assert blocks.validate_spec(_spec(trend="volume"))
    assert blocks.validate_spec(_spec(filters=["magic"]))


def test_missing_grid_param_rejected():
    spec = _spec(filters=["max_entry_vol"])  # grid lacks max_entry_vol
    assert any("max_entry_vol" in e for e in blocks.validate_spec(spec))


def test_unused_grid_param_rejected():
    spec = _spec()
    spec["grid"]["lookback"] = [20]  # donchian param on an rsi_cross spec
    assert any("lookback" in e for e in blocks.validate_spec(spec))


def test_conflicting_direction_filters_rejected():
    spec = _spec(filters=["calls_only", "puts_only"])
    assert any("mutually exclusive" in e for e in blocks.validate_spec(spec))


def test_grid_size_cap():
    spec = _spec()
    spec["grid"]["rsi_bull_level"] = [float(v) for v in range(25, 50)]
    spec["grid"]["rsi_bear_level"] = [float(v) for v in range(51, 76)]
    assert any("candidates" in e for e in blocks.validate_spec(spec))


def test_inverted_ema_pairs_dropped():
    spec = _spec()
    spec["grid"]["ema_fast"] = [15]
    spec["grid"]["ema_slow"] = [18]  # gap of 3 -> no valid candidate
    assert any("zero valid" in e for e in blocks.validate_spec(spec))


def test_candidates_clamped_into_bounds():
    spec = _spec()
    spec["grid"]["rsi_bull_level"] = [5.0]  # below the 25.0 floor
    candidates = blocks.spec_candidates(spec)
    assert all(c["rsi_bull_level"] == 25.0 for c in candidates)


# --- composed signals ---

def test_spec_matches_builtin_when_equivalent():
    closes, times = _sawtooth()
    params = {"ema_fast": 9, "ema_slow": 21,
              "rsi_bull_level": 45.0, "rsi_bear_level": 55.0}
    assert blocks.spec_signal(closes, times, _spec(), params) == \
        families.ema_cross_rsi(closes, times, params)


def test_calls_only_filter_strips_puts():
    closes, times = _sawtooth()
    spec = _spec(trend="none", filters=["calls_only"],
                 grid={"rsi_bull_level": [40.0], "rsi_bear_level": [60.0]})
    params = {"rsi_bull_level": 40.0, "rsi_bear_level": 60.0}
    signals = blocks.spec_signal(closes, times, spec, params)
    assert Action.BUY_CALL in signals
    assert Action.BUY_PUT not in signals


def test_max_entry_vol_suppresses_wild_bars():
    closes, times = _sawtooth()
    spec = _spec(trend="none", filters=["max_entry_vol"],
                 grid={"rsi_bull_level": [40.0], "rsi_bear_level": [60.0],
                       "max_entry_vol": [0.0005]})
    # sawtooth moves ~1% per bar; a 0.05% vol cap should kill every entry
    params = {"rsi_bull_level": 40.0, "rsi_bear_level": 60.0,
              "max_entry_vol": 0.0005}
    assert set(blocks.spec_signal(closes, times, spec, params)) == {Action.NONE}


def test_entry_hours_filter_uses_timestamps():
    closes, times = _sawtooth()
    spec = _spec(trend="none", filters=["entry_hours"],
                 grid={"rsi_bull_level": [40.0], "rsi_bear_level": [60.0],
                       "entry_hours": [[23]]})
    params = {"rsi_bull_level": 40.0, "rsi_bear_level": 60.0, "entry_hours": [23]}
    filtered = blocks.spec_signal(closes, times, spec, params)
    fired_hours = {times[i].hour for i, a in enumerate(filtered) if a != Action.NONE}
    assert fired_hours <= {23}

    params["entry_hours"] = sorted({t.hour for t in times})  # allow everything
    unfiltered = blocks.spec_signal(closes, times, spec, params)
    assert any(a != Action.NONE for a in unfiltered)


def test_donchian_with_trend_gate():
    # rising staircase: uptrend, so only calls should survive an ema_cross gate
    closes = [100.0 + i * 0.5 for i in range(120)]
    times = _sawtooth(120)[1]
    spec = _spec(trigger="donchian",
                 grid={"ema_fast": [9], "ema_slow": [21], "lookback": [20]})
    params = {"ema_fast": 9, "ema_slow": 21, "lookback": 20}
    signals = blocks.spec_signal(closes, times, spec, params)
    assert Action.BUY_CALL in signals
    assert Action.BUY_PUT not in signals


def test_spec_to_family_name_and_signal():
    family = blocks.spec_to_family(_spec())
    assert family.name == "spec:test-spec"
    closes, times = _sawtooth()
    params = blocks.spec_candidates(_spec())[0]
    assert len(family.signal(closes, times, params)) == len(closes)
