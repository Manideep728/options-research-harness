from datetime import UTC, datetime, timedelta

from bot.config import Settings
from bot.simulator import SimParams, signal_series, simulate
from bot.strategy import evaluate

CFG = Settings(api_key="", secret_key="")
SP = SimParams(iv=0.20, otm_pct=0.01, dte_days=10.0, roundtrip_cost=0.03)
T0 = datetime(2026, 1, 5, 15, 0, tzinfo=UTC)


def bounce_closes() -> list[float]:
    closes = [100.0 + 0.3 * (i + 1) for i in range(60)]
    px = closes[-1]
    closes += [px - 1.0 * (i + 1) for i in range(6)]
    closes += [closes[-1] + 3.0]
    return closes


def times_for(closes, minutes=15):
    return [T0 + timedelta(minutes=minutes * i) for i in range(len(closes))]


def times_with_overnight_gap(closes, minutes=15):
    """Bars `minutes` apart except the LAST one, which lands a day later — so
    the final step is a session gap the engine cannot trade through."""
    earlier = times_for(closes[:-1], minutes)
    return [*earlier, earlier[-1] + timedelta(days=1)]


def test_signal_series_agrees_with_live_evaluate():
    """The fast per-bar series must match strategy.evaluate at every prefix —
    this is what makes backtest results transferable to the live bot."""
    closes = bounce_closes()
    series = signal_series(closes, CFG)
    for i in range(len(closes)):
        assert series[i] == evaluate(closes[: i + 1], CFG).action, f"bar {i}"


def test_take_profit_path():
    # Signal bar, then a strong rally. At ~43x gearing a +1.2% move on the
    # underlying clears the +50% target comfortably.
    closes = bounce_closes()
    entry_px = closes[-1]
    closes += [entry_px * 1.005, entry_px * 1.012]
    result = simulate(closes, times_for(closes), CFG, SP)
    assert result.n == 1
    assert result.trades[0].reason == "take profit"
    # Intra-session: booked AT the barrier, not at the bar's overshoot.
    assert result.trades[0].pnl_pct == CFG.take_profit_pct


def test_stop_loss_path():
    closes = bounce_closes()
    entry_px = closes[-1]
    closes += [entry_px * 0.998, entry_px * 0.994]
    result = simulate(closes, times_for(closes), CFG, SP)
    assert result.n == 1
    assert result.trades[0].reason == "stop loss"
    assert result.trades[0].pnl_pct == -CFG.stop_loss_pct


def test_intra_session_barrier_ignores_overshoot_size():
    """The regression test for the phantom-edge bug: a stop is tripped by a
    move well under 1%, so the size of the overshoot past it is a property of
    the bar, not the strategy. Two very different overshoots must book the same
    loss. Booking the overshoot instead scored these as -0.51 and -1.0, and
    because gains had no matching cap, a coin flip made money."""
    booked = []
    for final_move in (0.994, 0.98):
        closes = bounce_closes()
        closes += [closes[-1] * 0.998, closes[-1] * final_move]
        result = simulate(closes, times_for(closes), CFG, SP)
        assert result.trades[0].reason == "stop loss"
        booked.append(result.trades[0].pnl_pct)
    assert booked == [-CFG.stop_loss_pct, -CFG.stop_loss_pct]


def test_overnight_gap_books_the_full_move_not_the_barrier():
    """Across a session gap the engine is not polling and cannot exit at the
    barrier, so the gap's real damage is booked. With no open column supplied
    the open falls back to the close, which is the close-only behaviour."""
    closes = bounce_closes()
    closes += [closes[-1] * 0.994]
    result = simulate(closes, times_with_overnight_gap(closes), CFG, SP)
    assert result.trades[0].reason == "stop loss"
    assert result.trades[0].pnl_pct < -CFG.stop_loss_pct


def test_max_hold_path_and_theta_drain():
    # Price frozen after entry; only theta and costs act. Spread bars a day
    # apart so max_hold (2 days) triggers before end of data.
    closes = bounce_closes()
    entry_px = closes[-1]
    closes += [entry_px] * 4
    times = times_for(closes[:-4]) + [
        times_for(closes[:-4])[-1] + timedelta(days=d + 1) for d in range(4)
    ]
    result = simulate(closes, times, CFG, SP)
    assert result.n == 1
    trade = result.trades[0]
    assert trade.reason == "max hold"
    # Flat price, so only decay and the spread act. Decay is now derived from
    # the pricing model rather than a flat rate per day.
    assert trade.pnl_pct < -SP.roundtrip_cost


def test_loss_capped_at_full_premium():
    """A long option cannot lose more than the premium, whatever the gap.

    Under Black-Scholes the floor is a property of the model rather than a
    clamp: the exit price cannot go below zero, so the return cannot go below
    -100%. It stops just short of -100% because an option keeps a little time
    value until expiry, which the old linear model could not represent."""
    closes = bounce_closes()
    closes += [closes[-1] * 0.90]
    result = simulate(closes, times_with_overnight_gap(closes), CFG, SP)
    pnl = result.trades[0].pnl_pct
    assert pnl >= -1.0
    assert pnl < -0.99


# --- intra-bar barrier resolution (needs the high and low columns) ---

def _entered(extra_closes, highs=None, lows=None, opens=None, gap=False):
    """One trade opened on the bounce, then `extra_closes` more bars.

    Any of the extra columns may be given for the appended bars only; the
    bounce bars themselves get their close for every column, which is what a
    cache without those columns would supply.
    """
    base = bounce_closes()
    closes = base + extra_closes
    pad = len(base)

    def column(values):
        return base + (list(values) if values is not None else list(extra_closes))

    times = (times_with_overnight_gap(closes) if gap else times_for(closes))
    result = simulate(closes, times, CFG, SP, highs=column(highs),
                      lows=column(lows), opens=column(opens))
    assert result.n == 1, f"fixture opened {result.n} trades"
    assert len(column(highs)) == len(closes) == pad + len(extra_closes)
    return result.trades[0]


def test_a_bar_whose_low_breaches_the_stop_exits_even_if_the_close_recovers():
    """The reason the high and low columns were added. Close-only data cannot
    see a spike through the stop that reverses before the bar ends, so the
    backtest kept a position the live engine — polling every 30 seconds —
    would already have closed."""
    entry = bounce_closes()[-1]
    trade = _entered([entry], lows=[entry * 0.98])
    assert trade.reason == "stop loss"
    assert trade.pnl_pct == -CFG.stop_loss_pct


def test_close_only_data_reproduces_the_old_behaviour():
    """Omitting the columns must degrade, not lie: the same flat bar with no
    low supplied has to leave the position open."""
    entry = bounce_closes()[-1]
    closes = [*bounce_closes(), entry, entry]
    result = simulate(closes, times_for(closes), CFG, SP)
    assert result.trades[0].reason != "stop loss"


def test_when_one_bar_touches_both_barriers_the_stop_wins():
    """OHLC cannot say which came first. Assuming the profit would let every
    wide bar book a win, which is the same class of error as booking the
    overshoot past a barrier."""
    entry = bounce_closes()[-1]
    trade = _entered([entry], highs=[entry * 1.05], lows=[entry * 0.95])
    assert trade.reason == "stop loss"
    assert trade.pnl_pct == -CFG.stop_loss_pct


def test_a_gap_through_the_stop_is_booked_at_the_open():
    """Across a session gap the engine is not polling, so it cannot exit at the
    barrier — the first price it can act on is the open, and that loss was not
    avoidable."""
    entry = bounce_closes()[-1]
    trade = _entered([entry * 0.97], opens=[entry * 0.97], gap=True)
    assert trade.reason == "stop loss"
    assert trade.pnl_pct < -CFG.stop_loss_pct


def test_a_gap_that_opens_safely_still_exits_at_the_barrier_intraday():
    """The other half of the gap rule: if the open did NOT breach, the engine
    is polling again, so the rest of that bar is barrier-limited rather than
    booked at its extreme."""
    entry = bounce_closes()[-1]
    trade = _entered([entry], opens=[entry], lows=[entry * 0.97], gap=True)
    assert trade.reason == "stop loss"
    assert trade.pnl_pct == -CFG.stop_loss_pct


# --- per-bar implied volatility ---

def test_higher_implied_volatility_makes_the_same_move_pay_less():
    """A dearer option needs a bigger move to return the same percentage, so
    feeding real implied volatility per bar changes the answer. If `ivs` were
    ignored these two would be identical."""
    entry = bounce_closes()[-1]
    closes = [*bounce_closes(), entry * 1.01]
    times = times_for(closes)
    cheap = simulate(closes, times, CFG, SP, ivs=[0.10] * len(closes))
    dear = simulate(closes, times, CFG, SP, ivs=[0.40] * len(closes))
    assert cheap.trades[0].pnl_pct > dear.trades[0].pnl_pct


def test_no_signals_no_trades():
    closes = [100.0] * 80
    result = simulate(closes, times_for(closes), CFG, SP)
    assert result.n == 0


def test_metrics_math():
    from bot.simulator import SimResult, SimTrade

    def trade(pnl):
        return SimTrade(T0, T0, "call", 1, 1, pnl, "x")

    r = SimResult(trades=[trade(0.5), trade(-0.25), trade(0.5), trade(-0.25)])
    assert r.win_rate == 0.5
    assert abs(r.expectancy - 0.125) < 1e-9
    assert abs(r.profit_factor - 2.0) < 1e-9
