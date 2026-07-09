"""The "analyze why it failed" step — a report for the HUMAN improver.

Buckets the candidate's TRAIN trades (never validation: if the human reads
validation failures and designs the next family from them, validation is no
longer out-of-sample for that family) and shows where the losses live:
exit reason, direction, symbol, entry hour, hold time, trend regime, and
volatility tercile at entry.
"""

import statistics
from datetime import datetime

from bot.indicators import ema
from bot.simulator import SimTrade

_VOL_LOOKBACK = 14  # same lookback the scanner uses


def _bucket_lines(title: str, trades: list[SimTrade], key_fn) -> list[str]:
    buckets: dict[str, list[SimTrade]] = {}
    for t in trades:
        buckets.setdefault(str(key_fn(t)), []).append(t)
    lines = [f"\n-- by {title} --"]
    for name in sorted(buckets):
        group = buckets[name]
        losses = sum(1 for t in group if t.pnl_pct < 0)
        avg = sum(t.pnl_pct for t in group) / len(group)
        lines.append(
            f"  {name:<14} n={len(group):<4} loss_rate={losses / len(group):.0%} "
            f"avg_pnl={avg:+.1%}"
        )
    return lines


def _hold_bucket(t: SimTrade) -> str:
    days = (t.exit_time - t.entry_time).total_seconds() / 86400
    if days < 0.5:
        return "<0.5d"
    if days < 1:
        return "0.5-1d"
    if days < 2:
        return "1-2d"
    return ">=2d"


def _entry_context(trades: list[SimTrade],
                   bars_by_symbol: dict[str, tuple[list[float], list[datetime]]],
                   ema_fast: int, ema_slow: int) -> dict[SimTrade, tuple[str, float]]:
    """(trend regime, entry volatility) per trade, computed once per symbol."""
    out: dict[SimTrade, tuple[str, float]] = {}
    for symbol, (closes, times) in bars_by_symbol.items():
        index_of = {t: i for i, t in enumerate(times)}
        ema_f, ema_s = ema(closes, ema_fast), ema(closes, ema_slow)
        for trade in trades:
            if trade.symbol != symbol:
                continue
            i = index_of.get(trade.entry_time)
            if i is None:
                continue
            regime = "uptrend" if ema_f[i] > ema_s[i] else "downtrend"
            window = closes[max(0, i - _VOL_LOOKBACK):i + 1]
            returns = [
                (window[j] - window[j - 1]) / window[j - 1]
                for j in range(1, len(window)) if window[j - 1] != 0
            ]
            vol = statistics.pstdev(returns) if len(returns) >= 2 else 0.0
            out[trade] = (regime, vol)
    return out


def build_report(trades: list[SimTrade],
                 bars_by_symbol: dict[str, tuple[list[float], list[datetime]]],
                 ema_fast: int = 9, ema_slow: int = 21) -> str:
    if not trades:
        return "no trades to analyze"

    losses = sum(1 for t in trades if t.pnl_pct < 0)
    avg = sum(t.pnl_pct for t in trades) / len(trades)
    lines = [
        "=== FAILURE ANALYSIS (train trades only) ===",
        f"trades={len(trades)} loss_rate={losses / len(trades):.0%} expectancy={avg:+.1%}",
    ]

    lines += _bucket_lines("exit reason", trades, lambda t: t.reason)
    lines += _bucket_lines("direction", trades, lambda t: t.direction)
    lines += _bucket_lines("symbol", trades, lambda t: t.symbol or "?")
    lines += _bucket_lines("entry hour (UTC)", trades, lambda t: f"{t.entry_time.hour:02d}h")
    lines += _bucket_lines("hold time", trades, _hold_bucket)

    context = _entry_context(trades, bars_by_symbol, ema_fast, ema_slow)
    matched = [t for t in trades if t in context]
    if matched:
        lines += _bucket_lines("trend regime", matched, lambda t: context[t][0])
        vols = sorted(context[t][1] for t in matched)
        lo = vols[len(vols) // 3]
        hi = vols[2 * len(vols) // 3]

        def tercile(t: SimTrade) -> str:
            v = context[t][1]
            return "low vol" if v <= lo else "high vol" if v > hi else "mid vol"

        lines += _bucket_lines("entry volatility", matched, tercile)

    lines.append("\nRead this, form a hypothesis, design the next family. "
                 "Do NOT tweak params to fix individual buckets — that's curve fitting.")
    return "\n".join(lines)
