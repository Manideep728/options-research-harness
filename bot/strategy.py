"""Signal generation: trend filter (EMA) + RSI-bounce trigger. Pure logic."""

from dataclasses import dataclass
from enum import Enum

from bot.config import Settings
from bot.indicators import ema, rsi


class Action(Enum):
    BUY_CALL = "BUY_CALL"
    BUY_PUT = "BUY_PUT"
    NONE = "NONE"


@dataclass(frozen=True)
class Signal:
    action: Action
    reason: str


def evaluate(closes: list[float], cfg: Settings) -> Signal:
    """Decide on the latest CLOSED bar.

    BUY_CALL: EMA(fast) > EMA(slow) and RSI crossed up through rsi_bull_level.
    BUY_PUT:  EMA(fast) < EMA(slow) and RSI crossed down through rsi_bear_level.
    """
    needed = max(cfg.ema_slow, cfg.rsi_period + 1) + 1
    if len(closes) < needed:
        return Signal(Action.NONE, f"not enough bars ({len(closes)} < {needed})")

    ema_fast = ema(closes, cfg.ema_fast)[-1]
    ema_slow = ema(closes, cfg.ema_slow)[-1]
    rsi_series = rsi(closes, cfg.rsi_period)
    rsi_prev, rsi_now = rsi_series[-2], rsi_series[-1]

    uptrend = ema_fast > ema_slow
    trend = "up" if uptrend else "down"
    detail = (
        f"EMA{cfg.ema_fast}={ema_fast:.2f} EMA{cfg.ema_slow}={ema_slow:.2f} "
        f"RSI {rsi_prev:.1f}->{rsi_now:.1f}"
    )

    if uptrend and rsi_prev < cfg.rsi_bull_level <= rsi_now:
        return Signal(
            Action.BUY_CALL,
            f"uptrend + RSI crossed up through {cfg.rsi_bull_level} ({detail})",
        )
    if not uptrend and rsi_prev > cfg.rsi_bear_level >= rsi_now:
        return Signal(
            Action.BUY_PUT,
            f"downtrend + RSI crossed down through {cfg.rsi_bear_level} ({detail})",
        )
    return Signal(Action.NONE, f"no trigger (trend={trend}, {detail})")
