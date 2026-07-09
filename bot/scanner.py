"""Universe ranking: score each symbol on how likely it is to produce a
tradeable signal *soon*, so the fast loop only polls the best few.

Pure logic (input: closes; output: a number). No I/O, like strategy.py.

The score is a hybrid — "primed to fire" weighted by "actually moving":

    score = trend_clarity * rsi_proximity * recent_volatility

Each factor answers a different question:
  - trend_clarity   : is there a real EMA trend, not a coin-flip?  (0..1)
  - rsi_proximity   : is RSI near the trigger it would cross?      (0..1)
  - recent_volatility: is the name moving enough to reach targets? (~0..0.05)

A symbol set up but dead-flat scores low; a wild mover with no setup also
scores low. Only names that are both primed AND moving rise to the top.
"""

import statistics
from dataclasses import dataclass

from bot.config import Settings
from bot.indicators import ema, rsi

# EMA separation of this fraction of price counts as a full-strength trend.
# ~0.5%: on a $500 name that's a $2.50 gap between the fast and slow EMA.
_TREND_FULL_STRENGTH = 0.005
# RSI points from the trigger beyond which "proximity" decays to zero.
_RSI_PROXIMITY_WINDOW = 15.0
# Bars of returns used to gauge how much the name is currently moving.
_VOL_LOOKBACK = 14


@dataclass(frozen=True)
class ScanScore:
    symbol: str
    score: float
    detail: str


def _recent_volatility(closes: list[float], lookback: int) -> float:
    """Std-dev of the last `lookback` simple returns — a unitless measure of
    how much the name moves per bar. Returns 0.0 if there isn't enough data."""
    window = closes[-(lookback + 1):]
    if len(window) < 3:
        return 0.0
    returns = [
        (window[i] - window[i - 1]) / window[i - 1]
        for i in range(1, len(window))
        if window[i - 1] != 0
    ]
    if len(returns) < 2:
        return 0.0
    return statistics.pstdev(returns)


def score_symbol(symbol: str, closes: list[float], cfg: Settings) -> ScanScore:
    """Rank score for one symbol. Higher = more likely to fire soon.

    Mirrors strategy.evaluate's data needs so a top-ranked name is one the
    strategy could actually trigger on, not just a random mover."""
    needed = max(cfg.ema_slow, cfg.rsi_period + 1) + 1
    if len(closes) < needed:
        return ScanScore(symbol, 0.0, f"not enough bars ({len(closes)} < {needed})")

    price = closes[-1]
    ema_fast = ema(closes, cfg.ema_fast)[-1]
    ema_slow = ema(closes, cfg.ema_slow)[-1]
    rsi_now = rsi(closes, cfg.rsi_period)[-1]

    # Which trigger is even in play depends on the trend direction. A flat
    # market (equal EMAs, price 0) is untradeable -> score 0.
    if price <= 0 or ema_fast == ema_slow:
        return ScanScore(symbol, 0.0, "no trend")
    target = cfg.rsi_bull_level if ema_fast > ema_slow else cfg.rsi_bear_level

    # trend_clarity: EMA gap as a fraction of price, capped at 1.0 so one
    # runaway name can't dominate purely on trend.
    trend_clarity = min(1.0, abs(ema_fast - ema_slow) / (price * _TREND_FULL_STRENGTH))
    # rsi_proximity: 1.0 when RSI sits on the trigger, decaying linearly to 0
    # once it's _RSI_PROXIMITY_WINDOW points away (either side).
    rsi_proximity = max(0.0, 1.0 - abs(rsi_now - target) / _RSI_PROXIMITY_WINDOW)
    volatility = _recent_volatility(closes, _VOL_LOOKBACK)

    score = trend_clarity * rsi_proximity * volatility
    detail = (
        f"trend={trend_clarity:.2f} rsiprox={rsi_proximity:.2f}"
        f" vol={volatility:.4f} (RSI {rsi_now:.1f} vs {target:.0f})"
    )
    return ScanScore(symbol, score, detail)


def rank_symbols(
    closes_by_symbol: dict[str, list[float]], cfg: Settings
) -> list[ScanScore]:
    """Score every symbol and return them best-first. Ties break on symbol
    name so the ordering is deterministic (important for tests)."""
    scores = [
        score_symbol(sym, closes_by_symbol.get(sym, []), cfg)
        for sym in cfg.symbols
    ]
    return sorted(scores, key=lambda s: (-s.score, s.symbol))
