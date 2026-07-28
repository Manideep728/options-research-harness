"""Strategy families: signal correctness, causality shape, clamping."""

from bot.strategy import Action
from research import families


def _sawtooth(cycles: int = 6, up: int = 12, down: int = 12) -> list[float]:
    closes = []
    for _ in range(cycles):
        closes += [100.0 + i for i in range(up)]
        closes += [100.0 + up - 1 - i for i in range(down)]
    return closes


def test_signal_length_matches_input():
    closes = _sawtooth()
    for family in families.FAMILIES.values():
        params = families.signal_candidates(family)[0]
        assert len(family.signal(closes, None, params)) == len(closes)


def test_short_input_yields_no_signals():
    for family in families.FAMILIES.values():
        params = families.signal_candidates(family)[0]
        assert set(family.signal([100.0, 101.0], None, params)) == {Action.NONE}


def test_donchian_breakout_triggers():
    closes = [100.0] * 30 + [105.0]  # clean break above a flat 30-bar window
    signals = families.donchian_breakout(closes, None, {"lookback": 20})
    assert signals[-1] == Action.BUY_CALL
    closes = [100.0] * 30 + [95.0]
    signals = families.donchian_breakout(closes, None, {"lookback": 20})
    assert signals[-1] == Action.BUY_PUT


def test_donchian_inside_range_is_none():
    closes = [100.0, 102.0] * 15 + [101.0]
    signals = families.donchian_breakout(closes, None, {"lookback": 20})
    assert signals[-1] == Action.NONE


def test_rsi_no_trend_fires_both_directions():
    signals = families.rsi_no_trend(
        _sawtooth(), None, {"rsi_bull_level": 40.0, "rsi_bear_level": 60.0}
    )
    assert Action.BUY_CALL in signals
    assert Action.BUY_PUT in signals


def test_ema_cross_only_fires_with_trend():
    # strictly rising series: never a downtrend, so no puts possible
    closes = [100.0 + 0.5 * i + (2.0 if i % 7 == 0 else 0.0) for i in range(120)]
    signals = families.ema_cross_rsi(
        closes, None,
        {"ema_fast": 9, "ema_slow": 21, "rsi_bull_level": 45.0, "rsi_bear_level": 55.0},
    )
    assert Action.BUY_PUT not in signals


def test_clamp_params_bounds_and_int_preservation():
    out = families.clamp_params(
        {"lookback": 500, "junk": 1.0}, {"lookback": (10, 100)}
    )
    assert out == {"lookback": 100}
    assert isinstance(out["lookback"], int)


def test_signal_candidates_expand_ema_pair_and_respect_bounds():
    candidates = families.signal_candidates(families.FAMILIES["baseline"])
    assert len(candidates) == 3 * 3 * 3
    for c in candidates:
        assert "ema_pair" not in c
        assert 5 <= c["ema_fast"] <= 15
        assert 18 <= c["ema_slow"] <= 30
        assert c["rsi_bull_level"] <= 50.0 <= c["rsi_bear_level"]
