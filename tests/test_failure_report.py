"""Failure report: bucket math and bar-context joins."""

from datetime import datetime, timedelta, timezone

from bot.simulator import SimTrade

from research import failure_report


def _times(n: int) -> list[datetime]:
    start = datetime(2026, 1, 5, 14, 30, tzinfo=timezone.utc)
    return [start + timedelta(minutes=15 * i) for i in range(n)]


def _trade(times, entry_i, pnl, reason="stop loss", direction="call",
           symbol="AAA", hold_bars=4) -> SimTrade:
    return SimTrade(
        entry_time=times[entry_i], exit_time=times[entry_i + hold_bars],
        direction=direction, entry_px=100.0, exit_px=100.0,
        pnl_pct=pnl, reason=reason, symbol=symbol,
    )


def test_empty_trades():
    assert failure_report.build_report([], {}) == "no trades to analyze"


def test_report_buckets_and_rates():
    times = _times(120)
    closes = [100.0 + 0.3 * i for i in range(120)]  # steady uptrend
    bars = {"AAA": (closes, times)}
    trades = [
        _trade(times, 60, -0.25, reason="stop loss"),
        _trade(times, 70, -0.25, reason="stop loss"),
        _trade(times, 80, +0.50, reason="take profit"),
    ]
    text = failure_report.build_report(trades, bars, ema_fast=9, ema_slow=21)
    assert "trades=3 loss_rate=67%" in text
    assert "stop loss" in text and "take profit" in text
    assert "uptrend" in text            # regime join found the entry bars
    assert "by entry volatility" in text


def test_trades_without_matching_bars_still_report():
    times = _times(50)
    trades = [_trade(times, 10, -0.1, symbol="ZZZ")]  # no ZZZ bars supplied
    text = failure_report.build_report(trades, {}, ema_fast=9, ema_slow=21)
    assert "trades=1" in text
    assert "trend regime" not in text   # context section skipped, no crash
