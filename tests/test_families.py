"""Strategy families: signal correctness, causality shape, clamping."""

import pytest

from bot.config import TUNABLE_BOUNDS
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
        kwargs = {"ivs": [0.2] * len(closes)} if family.needs_iv else {}
        assert len(family.signal(closes, None, params, **kwargs)) == len(closes)


def test_a_family_with_no_grid_still_has_one_candidate():
    """Returning an empty list would silently skip the family in the search,
    and the passive short-premium family has no parameters at all."""
    assert families.signal_candidates(families.FAMILIES["short_put_passive"]) == [{}]


# --- short premium ---

def test_passive_family_sells_a_spread_on_every_bar():
    """The benchmark: collect the premium blindly. The simulator holds one
    position at a time, so this enters, exits, and re-enters."""
    signals = families.always_short_put_spread([100.0] * 30, None, {})
    assert set(signals) == {Action.SELL_PUT_SPREAD}


def test_iv_rank_fires_only_in_the_upper_part_of_its_own_range():
    """Relative, not absolute: 20% implied vol is calm for one name and
    alarming for another, so the rule ranks each symbol against itself."""
    n = 60
    ivs = [0.10] * 40 + [0.30] * 20        # calm, then stressed
    params = {"iv_lookback": 20, "iv_rank_min": 0.8}
    signals = families.iv_rank_short_put_spread([100.0] * n, None, params, ivs=ivs)
    fired = [i for i, a in enumerate(signals) if a != Action.NONE]
    assert fired, "the rule never fired"
    assert min(fired) >= 40, "fired before implied vol rose"


def test_iv_rank_is_silent_without_an_implied_vol_series():
    """A symbol with no volatility index has no rank. Firing anyway would turn
    this into the passive family while still reporting under this name."""
    signals = families.iv_rank_short_put_spread(
        [100.0] * 60, None, {"iv_lookback": 20, "iv_rank_min": 0.5}, ivs=None)
    assert set(signals) == {Action.NONE}


def test_iv_rank_needs_a_full_lookback_and_a_real_range():
    ivs = [0.2] * 50
    assert families.iv_rank(ivs, i=10, lookback=20) is None      # too early
    assert families.iv_rank(ivs, i=30, lookback=20) is None      # flat: no range
    rising = [0.1 + 0.01 * i for i in range(50)]
    assert families.iv_rank(rising, i=30, lookback=20) == pytest.approx(1.0)


def _calm_then_crash(calm: int = 200, crash: int = 20):
    """A quiet series that ends in a violent one, with implied vol following.

    The moves alternate in sign so the level stays near 100 and only the
    VOLATILITY changes — the crash is 5% a day, which annualizes to about 79%,
    well above the 60% the index is quoting for it.
    """
    closes = [100.0]
    for i in range(calm):
        closes.append(closes[-1] * (1.002 if i % 2 else 0.998))
    for i in range(crash):
        closes.append(closes[-1] * (1.05 if i % 2 else 0.95))
    ivs = [0.15] * (calm + 1) + [0.60] * crash
    return closes, ivs


def test_vrp_spread_stands_down_in_the_crash_that_iv_rank_leans_into():
    """The entire reason this third rule exists. IV rank fires when implied is
    at the top of its own range, which is exactly when realized has already
    overtaken it. Comparing implied against DELIVERED volatility inverts that.
    """
    closes, ivs = _calm_then_crash()
    ranked = families.iv_rank_short_put_spread(
        closes, None, {"iv_lookback": 60, "iv_rank_min": 0.8}, ivs=ivs)
    spread = families.vrp_spread_short_put_spread(
        closes, None, {"rv_lookback": 10, "vrp_min": 0.05}, ivs=ivs)

    assert ranked[-1] == Action.SELL_PUT_SPREAD, "IV rank should fire in the crash"
    assert spread[-1] == Action.NONE, "implied is BELOW realized here; do not sell"
    # and the calm stretch, where options are genuinely rich, is the opposite way
    assert ranked[150] == Action.NONE
    assert spread[150] == Action.SELL_PUT_SPREAD


def test_vrp_spread_respects_its_minimum():
    """A floor above the premium on offer must silence the rule completely,
    otherwise the parameter is decorative and the search would report a tuning
    result that was really the passive family."""
    closes, ivs = _calm_then_crash(calm=100, crash=0)
    rich = families.vrp_spread_short_put_spread(
        closes, None, {"rv_lookback": 10, "vrp_min": 0.05}, ivs=ivs)
    unreachable = families.vrp_spread_short_put_spread(
        closes, None, {"rv_lookback": 10, "vrp_min": 0.90}, ivs=ivs)
    assert Action.SELL_PUT_SPREAD in rich
    assert set(unreachable) == {Action.NONE}


def test_vrp_spread_is_causal():
    """Element i may not change when later bars are appended. A realized-vol
    window that centred on i instead of ending at it would fail this, and the
    P&L engine cannot detect the difference."""
    closes, ivs = _calm_then_crash()
    params = {"rv_lookback": 21, "vrp_min": 0.02}
    prefix = families.vrp_spread_short_put_spread(
        closes[:150], None, params, ivs=ivs[:150])
    full = families.vrp_spread_short_put_spread(closes, None, params, ivs=ivs)
    assert prefix == full[:150]


def test_vrp_spread_is_silent_without_an_implied_vol_series():
    closes, _ = _calm_then_crash(calm=60, crash=0)
    signals = families.vrp_spread_short_put_spread(
        closes, None, {"rv_lookback": 10, "vrp_min": 0.0}, ivs=None)
    assert set(signals) == {Action.NONE}


def test_trailing_realized_vol_needs_a_full_window_and_is_a_fraction():
    """Implied volatility reaches the signal as a fraction and research.vrp
    reports percentage points, so a missing division by 100 would make every
    comparison in this family off by 100x — and it would still run."""
    closes = [100.0] * 20 + [100.0 * (1.01 ** i) for i in range(1, 31)]
    assert families.trailing_realized_vol(closes, i=5, lookback=10) is None
    assert families.trailing_realized_vol(closes, i=15, lookback=10) == 0.0
    # A steady 1%/bar drift is not zero here: the estimator is zero-MEAN, which
    # is what a variance swap pays on. 0.00995 log return * sqrt(252) ~= 0.158.
    steady = families.trailing_realized_vol(closes, i=45, lookback=10)
    assert steady == pytest.approx(0.158, abs=0.002)


def test_credit_exits_are_reachable_and_live_bounds_are_not_applied():
    """A credit spread's best case is about +19% of the capital it risks, so
    the live take-profit floor of 0.30 would make every target unreachable."""
    lo, hi = families.CREDIT_EXIT_BOUNDS["take_profit_pct"]
    assert hi < TUNABLE_BOUNDS["take_profit_pct"][0]
    assert all(lo <= v <= hi for v in families.CREDIT_EXIT_GRID["take_profit_pct"])


def test_short_input_yields_no_signals():
    """A family must not fire before its own indicators have warmed up —
    otherwise it reads an EMA or an RSI computed from almost nothing.

    short_put_passive is excluded because it genuinely has no indicator and no
    warm-up: it sells a spread on every bar by design, and that is the
    benchmark every timing rule gets measured against.
    """
    for name, family in families.FAMILIES.items():
        if name == "short_put_passive":
            continue
        params = families.signal_candidates(family)[0]
        kwargs = {"ivs": None} if family.needs_iv else {}
        assert set(family.signal([100.0, 101.0], None, params, **kwargs)) == {Action.NONE}


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
