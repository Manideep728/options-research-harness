"""Black-Scholes pricing.

The properties pinned here are the ones the linear-delta model did not have,
and whose absence produced the phantom edge:
  1. Convexity — the payoff bends, so a big move is not priced as a straight
     line through the middle of a curve.
  2. A price floor of zero that falls out of the model, instead of a max()
     applied on top of a formula that could return -300%.
  3. Decay that accelerates into expiry, rather than a flat fraction per day.
"""

import math

import pytest

from bot import pricing

RATE = pricing.DEFAULT_RATE


# --- known values and identities ---

def test_textbook_call_value():
    """S=100 K=100 T=1 r=0.05 sigma=0.20 prices at 10.4506 in every reference.
    A sign slip or a missing discount factor moves this by whole dollars."""
    value = pricing.price(100.0, 100.0, 1.0, 0.20, "call", rate=0.05)
    assert value == pytest.approx(10.4506, abs=1e-4)


def test_put_call_parity_holds():
    """C - P = S - K*exp(-rT). Parity is the single strongest check that the
    call and put branches agree; it fails for almost any algebra error."""
    for spot, strike, years, vol in ((100.0, 100.0, 1.0, 0.20),
                                     (612.5, 620.0, 10 / 365, 0.18),
                                     (50.0, 40.0, 0.5, 0.65)):
        call = pricing.price(spot, strike, years, vol, "call")
        put = pricing.price(spot, strike, years, vol, "put")
        assert call - put == pytest.approx(
            spot - strike * math.exp(-RATE * years), abs=1e-9)


def test_price_never_goes_below_zero():
    """The defect this whole module replaces: the linear model could return
    -300%, so the simulator clamped it. A price cannot be negative, so the
    floor is now a property of the model rather than a correction."""
    for vol in (0.05, 0.2, 0.9):
        for years in (1e-6, 0.01, 0.5):
            assert pricing.price(100.0, 200.0, years, vol, "call", RATE) >= 0.0
            assert pricing.price(200.0, 100.0, years, vol, "put", RATE) >= 0.0


# --- the shape the linear model got wrong ---

def test_payoff_is_convex_not_linear():
    """A straight line between two points on a convex curve lies above the
    curve's midpoint. Constant delta drew exactly that line, which is what
    mispriced large moves in both directions."""
    strike, years, vol = 100.0, 30 / 365, 0.20
    low, mid, high = 95.0, 100.0, 105.0
    p_low = pricing.price(low, strike, years, vol, "call")
    p_mid = pricing.price(mid, strike, years, vol, "call")
    p_high = pricing.price(high, strike, years, vol, "call")
    assert (p_low + p_high) / 2.0 > p_mid


def test_real_premium_is_far_above_the_constant_simparams_assumed():
    """SimParams.premium_pct_of_spot = 0.005 for a first-OTM 7-14 DTE contract.
    Priced properly the same contract costs about 0.93% of spot, so the real
    gearing is near 43x rather than the 80x the simulator assumed. Every return
    the old model produced was scaled by roughly two."""
    spot, vol, years = 100.0, 0.20, 10 / 365
    strike = pricing.strike_for(spot, 0.01, "call")
    premium = pricing.price(spot, strike, years, vol, "call")
    assert premium / spot == pytest.approx(0.0093, abs=0.0005)
    gearing = pricing.delta(spot, strike, years, vol, "call") / (premium / spot)
    assert 40.0 < gearing < 47.0


def test_linear_model_overstates_the_return_in_both_directions():
    """The constant-delta rule `move * 80` exaggerates the magnitude of every
    outcome, up and down, because it divides by a premium roughly half the
    real one. Booking those inflated numbers against fixed barriers is what
    let volatility alone read as expectancy."""
    spot, vol, years = 100.0, 0.20, 10 / 365
    strike = pricing.strike_for(spot, 0.01, "call")
    entry = pricing.price(spot, strike, years, vol, "call")
    for move in (-0.03, -0.01, -0.005, 0.005, 0.01, 0.03):
        actual = pricing.price(spot * (1 + move), strike, years, vol, "call") / entry - 1.0
        linear = max(move * 80.0, -1.0)
        assert abs(actual) < abs(linear), f"move {move:+.1%}"


def test_losses_decelerate_as_the_option_approaches_worthless():
    """Convexity on the downside: doubling an adverse move must lose less than
    twice as much, because the option is running out of value to lose. The
    linear model hit its -100% clamp instead, which is a cliff, not a curve."""
    spot, vol, years = 100.0, 0.20, 10 / 365
    strike = pricing.strike_for(spot, 0.01, "call")
    entry = pricing.price(spot, strike, years, vol, "call")
    small = pricing.price(spot * 0.995, strike, years, vol, "call") / entry - 1.0
    large = pricing.price(spot * 0.99, strike, years, vol, "call") / entry - 1.0
    assert large > 2.0 * small          # both negative; the loss is not doubled
    assert large > -1.0


def test_delta_rises_as_the_option_moves_into_the_money():
    """Gamma, which the constant-delta model did not have at all."""
    strike, years, vol = 100.0, 30 / 365, 0.20
    deltas = [pricing.delta(s, strike, years, vol, "call") for s in (90.0, 100.0, 110.0)]
    assert deltas[0] < deltas[1] < deltas[2]
    assert deltas[0] >= 0.0
    assert deltas[2] <= 1.0


def test_put_delta_is_negative_and_bounded():
    d = pricing.delta(100.0, 100.0, 30 / 365, 0.20, "put")
    assert -1.0 <= d <= 0.0


def test_decay_accelerates_into_expiry():
    """Flat theta per calendar day was wrong precisely in the 7-14 DTE window
    the live bot trades. An ATM option loses value with the square root of the
    time left, so the last days cost far more than the first."""
    strike, vol = 100.0, 0.20
    at = lambda days: pricing.price(100.0, strike, days / 365, vol, "call")  # noqa: E731
    far_week = at(60) - at(53)
    near_week = at(8) - at(1)
    assert near_week > far_week


def test_value_falls_to_intrinsic_at_expiry():
    assert pricing.price(110.0, 100.0, 0.0, 0.20, "call") == pytest.approx(10.0)
    assert pricing.price(90.0, 100.0, 0.0, 0.20, "call") == 0.0
    assert pricing.price(90.0, 100.0, -1.0, 0.20, "put") == pytest.approx(10.0)


def test_zero_volatility_is_discounted_intrinsic():
    """A real state in a backtest, not a hypothetical: it must not divide by
    zero inside d1."""
    value = pricing.price(110.0, 100.0, 1.0, 0.0, "call")
    assert value == pytest.approx(110.0 - 100.0 * math.exp(-RATE))
    assert pricing.price(90.0, 100.0, 1.0, 0.0, "call") == 0.0


def test_degenerate_inputs_do_not_raise():
    assert pricing.price(0.0, 100.0, 1.0, 0.2, "call") == 0.0
    assert pricing.price(100.0, 0.0, 1.0, 0.2, "call") == 0.0
    assert pricing.delta(0.0, 100.0, 1.0, 0.2, "call") == 0.0
    assert pricing.vega(100.0, 100.0, 0.0, 0.2) == 0.0


# --- monotonicity ---

def test_price_rises_with_volatility():
    """The property the whole variance risk premium thesis rests on: a seller
    is paid more when implied volatility is higher."""
    prices = [pricing.price(100.0, 100.0, 30 / 365, v, "call")
              for v in (0.10, 0.20, 0.40)]
    assert prices[0] < prices[1] < prices[2]


def test_price_rises_with_time_to_expiry():
    prices = [pricing.price(100.0, 105.0, t / 365, 0.20, "call")
              for t in (7, 30, 90)]
    assert prices[0] < prices[1] < prices[2]


def test_call_rises_and_put_falls_with_spot():
    calls = [pricing.price(s, 100.0, 30 / 365, 0.2, "call") for s in (95.0, 105.0)]
    puts = [pricing.price(s, 100.0, 30 / 365, 0.2, "put") for s in (95.0, 105.0)]
    assert calls[0] < calls[1]
    assert puts[0] > puts[1]


def test_vega_is_positive_and_equal_for_calls_and_puts():
    """Parity forces it: the call and put differ by a term with no volatility
    in it, so their sensitivity to volatility must be identical."""
    v = pricing.vega(100.0, 100.0, 30 / 365, 0.20)
    assert v > 0.0
    bump = 0.0001
    call_move = (pricing.price(100.0, 100.0, 30 / 365, 0.20 + bump, "call")
                 - pricing.price(100.0, 100.0, 30 / 365, 0.20, "call"))
    put_move = (pricing.price(100.0, 100.0, 30 / 365, 0.20 + bump, "put")
                - pricing.price(100.0, 100.0, 30 / 365, 0.20, "put"))
    assert call_move == pytest.approx(put_move, abs=1e-9)
    assert call_move / bump == pytest.approx(v, rel=1e-3)


# --- strike selection ---

def test_strike_for_is_otm_on_both_sides_and_rounded():
    assert pricing.strike_for(612.345, 0.01, "call") == 618.47
    assert pricing.strike_for(612.345, 0.01, "put") == 606.22
    assert pricing.strike_for(100.0, 0.0, "call") == 100.0
