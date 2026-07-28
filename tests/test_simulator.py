from datetime import UTC, datetime, timedelta

from bot.config import Settings
from bot.simulator import SimParams, signal_series, simulate
from bot.strategy import evaluate

CFG = Settings(api_key="", secret_key="")
SP = SimParams(delta=0.40, premium_pct_of_spot=0.005, theta_daily=0.05, roundtrip_cost=0.03)
T0 = datetime(2026, 1, 5, 15, 0, tzinfo=UTC)


def bounce_closes() -> list[float]:
    closes = [100.0 + 0.3 * (i + 1) for i in range(60)]
    px = closes[-1]
    closes += [px - 1.0 * (i + 1) for i in range(6)]
    closes += [closes[-1] + 3.0]
    return closes


def times_for(closes, minutes=15):
    return [T0 + timedelta(minutes=minutes * i) for i in range(len(closes))]


def test_signal_series_agrees_with_live_evaluate():
    """The fast per-bar series must match strategy.evaluate at every prefix —
    this is what makes backtest results transferable to the live bot."""
    closes = bounce_closes()
    series = signal_series(closes, CFG)
    for i in range(len(closes)):
        assert series[i] == evaluate(closes[: i + 1], CFG).action, f"bar {i}"


def test_take_profit_path():
    # Signal bar, then a strong rally: +1% underlying ~= +80% option (gross).
    closes = bounce_closes()
    entry_px = closes[-1]
    closes += [entry_px * 1.005, entry_px * 1.012]
    result = simulate(closes, times_for(closes), CFG, SP)
    assert result.n == 1
    assert result.trades[0].reason == "take profit"
    assert result.trades[0].pnl_pct >= CFG.take_profit_pct


def test_stop_loss_path():
    closes = bounce_closes()
    entry_px = closes[-1]
    closes += [entry_px * 0.998, entry_px * 0.994]
    result = simulate(closes, times_for(closes), CFG, SP)
    assert result.n == 1
    assert result.trades[0].reason == "stop loss"
    assert result.trades[0].pnl_pct <= -CFG.stop_loss_pct


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
    # flat price -> pnl = -(theta * days) - roundtrip cost
    assert trade.pnl_pct < -SP.roundtrip_cost


def test_loss_capped_at_full_premium():
    closes = bounce_closes()
    entry_px = closes[-1]
    closes += [entry_px * 0.90]  # catastrophic gap against the position
    result = simulate(closes, times_for(closes), CFG, SP)
    assert result.trades[0].pnl_pct == -1.0


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
