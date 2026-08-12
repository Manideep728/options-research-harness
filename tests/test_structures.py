"""Defined-risk structures: credit spreads, capital at risk, and sizing.

The variance risk premium is collected by SELLING options, which inverts every
failure mode the harness was built for. A short position wins small and often
and loses big and rarely, so the numbers that matter are the maximum loss and
the sign of the profit — not the average, which will look excellent either way.

What these pin:
  1. A naked short is not representable. Its loss is unbounded for a call, and
     a backtest that can express a position the risk rules forbid will
     eventually report a result that depends on taking it.
  2. Every return is quoted against capital at risk, so a long option keeps its
     current meaning and a spread cannot report a 5 dollar credit as a 100
     percent gain on a 450 dollar risk.
  3. Sizing gates on the maximum loss, never on the cash collected.
"""

import pytest

from bot import pricing
from bot.config import Settings
from bot.risk import OpenPosition, size_allowed
from bot.simulator import SimParams, open_structure, unreachable_take_profit

CFG = Settings(api_key="", secret_key="")
SP = SimParams(iv=0.20, otm_pct=0.01, dte_days=10.0, roundtrip_cost=0.0,
               spread_width_pct=0.05)
SPOT = 100.0


# --- the structure itself ---

def test_long_option_risks_exactly_the_premium():
    """The existing meaning of pnl_pct has to survive: for a long option the
    premium paid IS the capital at risk."""
    st = open_structure(SPOT, "call", SP, is_credit=False)
    assert st.long_strike is None
    assert st.max_loss == pytest.approx(st.entry_value)
    assert st.max_gain == float("inf")


def test_credit_spread_buys_a_further_otm_leg_for_protection():
    put = open_structure(SPOT, "put", SP, is_credit=True)
    assert put.short_strike == pytest.approx(99.0)      # 1% OTM
    assert put.long_strike == pytest.approx(94.0)       # 5% further out
    assert put.long_strike < put.short_strike

    call = open_structure(SPOT, "call", SP, is_credit=True)
    assert call.long_strike > call.short_strike


def test_credit_spread_risk_is_the_width_less_the_credit():
    """The number position sizing must use. Getting this wrong is how a bot
    opens many times the intended risk while every check reports it as fine."""
    st = open_structure(SPOT, "put", SP, is_credit=True)
    width = abs(st.short_strike - st.long_strike)
    assert st.entry_value > 0.0                      # it is a credit
    assert st.max_loss == pytest.approx(width - st.entry_value)
    assert st.max_loss > st.entry_value              # risk exceeds reward here


def test_a_naked_short_cannot_be_built():
    """There is no argument that produces one. The only short the model can
    express carries a protective leg, so max_loss is always finite."""
    for call_put in ("call", "put"):
        st = open_structure(SPOT, call_put, SP, is_credit=True)
        assert st.long_strike is not None
        assert 0.0 < st.max_loss < float("inf")


def test_credit_spread_gain_is_capped_at_the_credit():
    st = open_structure(SPOT, "put", SP, is_credit=True)
    assert st.max_gain == pytest.approx(st.entry_value / st.max_loss)
    assert st.max_gain < 1.0


def test_unreachable_take_profit_is_detected():
    """A 50% target on a spread whose best case is 25% never fires, so the
    strategy would be judged only on its stops and its time exits."""
    st = open_structure(SPOT, "put", SP, is_credit=True)
    assert unreachable_take_profit(st, 0.90)
    assert not unreachable_take_profit(st, st.max_gain / 2.0)
    long_call = open_structure(SPOT, "call", SP, is_credit=False)
    assert not unreachable_take_profit(long_call, 5.0)   # a long has no cap


# --- profit and loss runs the other way ---

def test_a_seller_profits_when_the_option_loses_value():
    """The sign test. A short position gains as the price it would cost to buy
    back falls. Reusing the long formula would report every winner as a loser."""
    st = open_structure(SPOT, "put", SP, is_credit=True)
    calm = st.return_at(SPOT * 1.02, days=5.0, sp=SP)     # away from the strike
    assert calm > 0.0
    hurt = st.return_at(SPOT * 0.94, days=5.0, sp=SP)     # through both strikes
    assert hurt < 0.0


def test_a_seller_cannot_lose_more_than_the_width_less_the_credit():
    """The whole point of buying the protective leg. Beyond the long strike the
    two legs move together and the loss stops growing."""
    st = open_structure(SPOT, "put", SP, is_credit=True)
    catastrophe = st.return_at(SPOT * 0.50, days=9.9, sp=SP)
    assert catastrophe >= -1.0
    assert catastrophe == pytest.approx(-1.0, abs=0.02)


def test_holding_a_credit_spread_to_expiry_out_of_the_money_earns_the_credit():
    st = open_structure(SPOT, "put", SP, is_credit=True)
    expired = st.return_at(SPOT * 1.05, days=SP.dte_days, sp=SP)
    assert expired == pytest.approx(st.max_gain, rel=1e-6)


def test_long_option_return_is_unchanged_in_meaning():
    st = open_structure(SPOT, "call", SP, is_credit=False)
    worthless = st.return_at(SPOT * 0.80, days=SP.dte_days, sp=SP)
    assert worthless == pytest.approx(-1.0)


@pytest.mark.parametrize("roundtrip", [0.0, 0.03, 0.06, 0.10])
@pytest.mark.parametrize("is_credit", [True, False])
def test_max_loss_is_a_true_bound_at_every_cost_level(roundtrip, is_credit):
    """A found bug, not a hypothetical. Charging the closing spread on top of a
    structural maximum made a short put spread return -102.1% of its own stated
    max loss. Position sizing gates on that number, so anything below -1 means
    the bot can lose more than the risk system believes is possible.

    Both entry_value and max_loss now carry the spread, so the worst case is
    exactly -1 whatever the cost.
    """
    sp = SimParams(iv=0.20, otm_pct=0.01, dte_days=10.0,
                   roundtrip_cost=roundtrip, spread_width_pct=0.05)
    st = open_structure(SPOT, "put", sp, is_credit=is_credit)
    for move in (-0.90, -0.50, -0.20, -0.06, -0.04, 0.0, 0.50):
        assert st.return_at(SPOT * (1 + move), 9.99, sp) >= -1.0 - 1e-9, (
            f"move {move:+.0%} lost more than the stated max loss")


def test_a_higher_cost_lowers_the_credit_and_raises_the_risk():
    """The spread is paid on the way in and again on the way out, so a dearer
    round trip collects less and risks more. A model that ignored the exit cost
    would report the same risk at every cost level."""
    cheap = open_structure(SPOT, "put", SimParams(roundtrip_cost=0.0,
                                                  spread_width_pct=0.05),
                           is_credit=True)
    dear = open_structure(SPOT, "put", SimParams(roundtrip_cost=0.10,
                                                 spread_width_pct=0.05),
                          is_credit=True)
    assert dear.entry_value < cheap.entry_value
    assert dear.max_loss > cheap.max_loss
    assert dear.max_gain < cheap.max_gain


def test_spread_value_is_never_negative():
    """The short leg is the nearer strike, so it is always worth at least as
    much as the protective one. A negative value would mean the position was a
    debit, and the max-loss arithmetic would then be wrong."""
    for spot in (50.0, 94.0, 99.0, 100.0, 150.0):
        assert pricing.spread_value(spot, 99.0, 94.0, 10 / 365, 0.2, "put") >= 0.0


# --- sizing gates on risk, not on cash ---

def test_sizing_uses_max_loss_for_a_credit_position():
    """A spread that collects 0.50 and risks 4.50 must be gated on 4.50. Gating
    on the credit would permit nine times the intended risk."""
    equity = 100_000.0
    limit = equity * CFG.max_premium_pct_of_equity   # 2000 dollars
    # The credit is identical in both calls; only the risk differs, which is
    # exactly the distinction the old signature could not make.
    assert size_allowed(0.50, equity, CFG, max_loss_per_share=15.0).allowed
    blocked = size_allowed(0.50, equity, CFG, max_loss_per_share=25.0)
    assert not blocked.allowed
    assert "max loss" in blocked.reason
    assert f"{limit:.2f}" in blocked.reason
    # And gating on the credit alone would have waved both through.
    assert size_allowed(0.50, equity, CFG).allowed


def test_sizing_is_unchanged_for_a_long_option():
    equity = 100_000.0
    assert size_allowed(15.0, equity, CFG).allowed
    assert not size_allowed(25.0, equity, CFG).allowed


# --- the position view the engine sees ---

def test_open_position_pnl_inverts_for_a_credit_position():
    short = OpenPosition("X", "SPY", 1, avg_entry_price=0.50, current_price=0.20,
                         days_to_expiry=5, is_credit=True, max_loss_per_share=4.50)
    assert short.pnl_pct == pytest.approx((0.50 - 0.20) / 4.50)

    long_ = OpenPosition("X", "SPY", 1, avg_entry_price=0.50, current_price=0.20,
                         days_to_expiry=5)
    assert long_.pnl_pct == pytest.approx((0.20 - 0.50) / 0.50)
    assert long_.pnl_pct < 0 < short.pnl_pct


def test_open_position_defaults_keep_the_long_meaning():
    """Every existing caller constructs this positionally with six fields."""
    p = OpenPosition("X", "SPY", 1, 2.00, 3.00, 5)
    assert not p.is_credit
    assert p.capital_at_risk == 2.00
    assert p.pnl_pct == pytest.approx(0.5)


def test_credit_position_without_a_stated_max_loss_reports_no_pnl():
    """Rather than silently dividing by the credit, which would overstate the
    return several times over."""
    p = OpenPosition("X", "SPY", 1, 0.50, 0.20, 5, is_credit=True)
    assert p.capital_at_risk == 0.0
    assert p.pnl_pct == 0.0
