"""Signal generation: trend filter (EMA) + RSI-bounce trigger. Pure logic."""

from dataclasses import dataclass
from enum import Enum

from bot.config import Settings
from bot.indicators import ema, rsi


class Action(Enum):
    """What a signal asks the engine to open.

    The two SELL actions are defined-risk vertical CREDIT SPREADS, never naked
    shorts: short one strike, long a further-OTM one. A naked short call has no
    bounded loss, and a model that can express a position the risk rules forbid
    will eventually report a result that depends on taking it.

    Direction is about the underlying, not about the option type. A short put
    spread is bullish and a short call spread is bearish, the same way a long
    call is bullish and a long put is bearish.
    """

    BUY_CALL = "BUY_CALL"
    BUY_PUT = "BUY_PUT"
    SELL_PUT_SPREAD = "SELL_PUT_SPREAD"      # bullish, collects premium
    SELL_CALL_SPREAD = "SELL_CALL_SPREAD"    # bearish, collects premium
    NONE = "NONE"

    @property
    def is_credit(self) -> bool:
        return self in (Action.SELL_PUT_SPREAD, Action.SELL_CALL_SPREAD)

    @property
    def call_put(self) -> str:
        return "call" if self in (Action.BUY_CALL, Action.SELL_CALL_SPREAD) else "put"

    @property
    def is_bullish(self) -> bool:
        """True when the position gains as the underlying rises."""
        return self in (Action.BUY_CALL, Action.SELL_PUT_SPREAD)


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
