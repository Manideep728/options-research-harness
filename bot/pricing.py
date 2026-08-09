"""Black-Scholes option pricing — the model that replaces the linear delta.

Why this exists. bot/simulator.py approximated an option's return as
`underlying_move * delta / premium_pct`, with delta and premium held constant.
That approximation has three defects, and all three flatter the result:

  - Constant delta means no gamma. A long option gains delta as it moves into
    the money and loses it going the other way, so the real payoff bends. A
    straight line through the middle of a curve is wrong at both ends.
  - A first-OTM contract must travel to its strike before it holds intrinsic
    value. The linear model gives it that value immediately, so it pays out too
    much on large favourable moves.
  - Theta was a flat fraction per calendar day at any distance from expiry.
    Real decay accelerates as expiry approaches, and the live bot trades 7-14
    days to expiry, which is where the acceleration is steepest.

The floor also stops being a special case. `option_return` used to clamp at
-100% with max(), because the linear formula could return -300%. A price from
this module cannot go below zero, so the floor is a property of the model
rather than a correction applied on top of it.

What is still not modelled, stated rather than glossed: the volatility smile
(one implied volatility serves every strike), term structure, discrete
dividends, and early exercise. American options on liquid ETFs rarely pay to
exercise early when there is no dividend, so European pricing is close enough
for a backtest and wrong for anything holding through an ex-dividend date.
"""

import math
from statistics import NormalDist

_NORMAL = NormalDist()

# Short-dated risk-free rate. It barely moves a 7-14 day option's value, but
# leaving it out entirely biases puts and calls in opposite directions.
DEFAULT_RATE = 0.04


def _pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def intrinsic(spot: float, strike: float, call_put: str) -> float:
    """Value at expiry. Also the value when volatility is zero and the option
    has nothing left to be uncertain about."""
    return max(spot - strike, 0.0) if call_put == "call" else max(strike - spot, 0.0)


def _d1_d2(spot: float, strike: float, years: float, vol: float,
           rate: float) -> tuple[float, float]:
    sigma_root_t = vol * math.sqrt(years)
    d1 = (math.log(spot / strike) + (rate + 0.5 * vol * vol) * years) / sigma_root_t
    return d1, d1 - sigma_root_t


def price(spot: float, strike: float, years: float, vol: float,
          call_put: str, rate: float = DEFAULT_RATE) -> float:
    """Black-Scholes price of one European option, per share.

    `years` is time to expiry in years, `vol` is annualized implied volatility
    as a fraction (0.18, not 18). Degenerate inputs collapse to the value the
    contract must have rather than raising: at or past expiry it is worth its
    intrinsic value, and with no volatility it is worth the discounted
    intrinsic value. Both are real states a backtest walks through.
    """
    if spot <= 0.0 or strike <= 0.0:
        return 0.0
    if years <= 0.0:
        return intrinsic(spot, strike, call_put)
    if vol <= 0.0:
        discounted = strike * math.exp(-rate * years)
        return (max(spot - discounted, 0.0) if call_put == "call"
                else max(discounted - spot, 0.0))

    d1, d2 = _d1_d2(spot, strike, years, vol, rate)
    discounted = strike * math.exp(-rate * years)
    if call_put == "call":
        return spot * _NORMAL.cdf(d1) - discounted * _NORMAL.cdf(d2)
    return discounted * _NORMAL.cdf(-d2) - spot * _NORMAL.cdf(-d1)


def delta(spot: float, strike: float, years: float, vol: float,
          call_put: str, rate: float = DEFAULT_RATE) -> float:
    """Change in option price per 1.0 change in the underlying."""
    if spot <= 0.0 or strike <= 0.0:
        return 0.0
    if years <= 0.0 or vol <= 0.0:
        in_the_money = spot > strike if call_put == "call" else spot < strike
        if not in_the_money:
            return 0.0
        return 1.0 if call_put == "call" else -1.0
    d1, _ = _d1_d2(spot, strike, years, vol, rate)
    return _NORMAL.cdf(d1) if call_put == "call" else _NORMAL.cdf(d1) - 1.0


def vega(spot: float, strike: float, years: float, vol: float,
         rate: float = DEFAULT_RATE) -> float:
    """Change in option price per 1.0 change in volatility (i.e. per 100 vol
    points). Identical for a call and a put at the same strike."""
    if spot <= 0.0 or strike <= 0.0 or years <= 0.0 or vol <= 0.0:
        return 0.0
    d1, _ = _d1_d2(spot, strike, years, vol, rate)
    return spot * _pdf(d1) * math.sqrt(years)


def strike_for(spot: float, otm_pct: float, call_put: str) -> float:
    """The strike `otm_pct` out of the money, rounded to a cent.

    Rounding matters: real strikes are discrete, and pricing an option at a
    strike no exchange lists produces a premium no one could have paid.
    """
    moved = spot * (1.0 + otm_pct) if call_put == "call" else spot * (1.0 - otm_pct)
    return round(moved, 2)
