"""Strategy families: the widened (but bounded) rule space the search walks.

A Family is a named signal function plus a bounded parameter grid. Signal
functions are CAUSAL series functions — signals[i] may only use closes[:i+1]
— with the same contract as simulator.signal_series, so every family runs
through the same verified P&L engine via simulate(signal_fn=...).

The space is small on purpose: four structurally different families, a few
hundred candidates each. Every candidate is clamped into per-family bounds
(the TUNABLE_BOUNDS pattern from bot/config.py) so no hand-edited grid or
future auto-proposer can escape the rails.
"""

import itertools
from collections.abc import Callable
from dataclasses import dataclass

from bot.indicators import ema, rsi
from bot.strategy import Action

RSI_PERIOD = 14  # fixed, as in the live bot

# Signal contract: (closes, times, params) -> list[Action], causal.
# `times` (bar timestamps, parallel to closes) exists for blocks that need
# the clock (e.g. entry-hour filters); the built-in families ignore it.
#
# A family that sets needs_iv is called with an extra `ivs=` keyword carrying
# the implied volatility series. Typed loosely because the two shapes differ;
# the alternative was a second contract, and one signature the searcher can
# always call is worth more than exact arity here.
SignalFn = Callable[..., "list[Action]"]


@dataclass(frozen=True)
class Family:
    name: str
    description: str
    signal: SignalFn
    grid: dict[str, list]                       # signal params only
    bounds: dict[str, tuple[float, float]]
    # Exits are part of a strategy, not a setting shared across all of them. A
    # breakout wants a wide stop; a credit spread cannot even reach the live
    # bot's +50% target, because its best possible outcome is about +19% of the
    # capital it risks. A family that sets these is judged on its own exits and
    # clamped against its own bounds instead of bot.config.TUNABLE_BOUNDS.
    #
    # A family with its own exit_bounds cannot be promoted to the live bot
    # without a code change — already true of every non-baseline family.
    exit_grid: dict[str, list] | None = None
    exit_bounds: dict[str, tuple[float, float]] | None = None
    # True when the signal needs the implied volatility series as well as the
    # prices. run_family then calls signal(closes, times, params, ivs=...).
    needs_iv: bool = False
    # Which cached dataset the family is searched on. "intraday" is the 30-name
    # 15-minute universe. "vrp" is SPY/QQQ/IWM daily, the only symbols with a
    # real implied volatility history (VIX/VXN/RVX), and therefore the only
    # ones a short-premium strategy can be priced on honestly.
    dataset: str = "intraday"
    # The incumbent this family must beat, by name. None means the live EMA and
    # RSI strategy. A short-premium family names the passive harvest instead:
    # the question is never "does the premium exist" — Session 1 settled that —
    # but "does this timing rule beat collecting it blindly".
    baseline_family: str | None = None
    # True when the family SELLS premium. The coin-flip control must trade
    # the same structure, or it measures the cost of buying options rather
    # than the skill of the candidate.
    credit: bool = False


# --- signal functions ---

def _none_series(n: int) -> list[Action]:
    return [Action.NONE] * n


def _rsi_cross(rsi_prev: float, rsi_now: float, bull: float, bear: float,
               uptrend: bool | None) -> Action:
    """uptrend=True allows calls only, False puts only, None both."""
    if uptrend in (None, True) and rsi_prev < bull <= rsi_now:
        return Action.BUY_CALL
    if uptrend in (None, False) and rsi_prev > bear >= rsi_now:
        return Action.BUY_PUT
    return Action.NONE


def ema_cross_rsi(closes: list[float], times, p: dict) -> list[Action]:
    """The live strategy's structure: EMA fast/slow trend filter + RSI cross."""
    fast, slow = int(p["ema_fast"]), int(p["ema_slow"])
    needed = max(slow, RSI_PERIOD + 1) + 1
    if len(closes) < needed:
        return _none_series(len(closes))
    ema_f, ema_s = ema(closes, fast), ema(closes, slow)
    r = rsi(closes, RSI_PERIOD)
    out = _none_series(len(closes))
    for i in range(needed - 1, len(closes)):
        out[i] = _rsi_cross(r[i - 1], r[i], p["rsi_bull_level"],
                            p["rsi_bear_level"], ema_f[i] > ema_s[i])
    return out


def ema_slope_rsi(closes: list[float], times, p: dict) -> list[Action]:
    """Trend = slope of the slow EMA over `slope_bars` (rising/falling),
    instead of the fast/slow cross. Same RSI trigger."""
    slow, k = int(p["ema_slow"]), int(p["slope_bars"])
    needed = max(slow + k, RSI_PERIOD + 1) + 1
    if len(closes) < needed:
        return _none_series(len(closes))
    ema_s = ema(closes, slow)
    r = rsi(closes, RSI_PERIOD)
    out = _none_series(len(closes))
    for i in range(needed - 1, len(closes)):
        out[i] = _rsi_cross(r[i - 1], r[i], p["rsi_bull_level"],
                            p["rsi_bear_level"], ema_s[i] > ema_s[i - k])
    return out


def rsi_no_trend(closes: list[float], times, p: dict) -> list[Action]:
    """Control family: RSI cross with NO trend filter. If this scores as
    well as the filtered families, the trend filter is doing nothing."""
    needed = RSI_PERIOD + 2
    if len(closes) < needed:
        return _none_series(len(closes))
    r = rsi(closes, RSI_PERIOD)
    out = _none_series(len(closes))
    for i in range(needed - 1, len(closes)):
        out[i] = _rsi_cross(r[i - 1], r[i], p["rsi_bull_level"],
                            p["rsi_bear_level"], None)
    return out


def donchian_breakout(closes: list[float], times, p: dict) -> list[Action]:
    """Momentum family: close breaks above the prior `lookback`-bar high ->
    call; below the prior low -> put. Structurally unlike the RSI families."""
    n = int(p["lookback"])
    out = _none_series(len(closes))
    for i in range(n, len(closes)):
        window = closes[i - n:i]
        if closes[i] > max(window):
            out[i] = Action.BUY_CALL
        elif closes[i] < min(window):
            out[i] = Action.BUY_PUT
    return out


# --- short premium: the variance risk premium families ---

def iv_rank(ivs: list[float], i: int, lookback: int) -> float | None:
    """Where today's implied volatility sits in its own trailing range, 0..1.

    Relative, not absolute, because 20% is calm for NVDA and alarming for TLT.
    None until a full lookback exists, and None when the range is flat — with
    no spread there is no rank, and returning 0.5 would invent one.
    """
    if i < lookback:
        return None
    window = ivs[i - lookback:i + 1]
    low, high = min(window), max(window)
    if high <= low:
        return None
    return (ivs[i] - low) / (high - low)


def always_short_put_spread(closes: list[float], times, p: dict,
                            ivs: list[float] | None = None) -> list[Action]:
    """Sell a put spread on every bar — the PASSIVE harvest, and the benchmark.

    The simulator holds one position at a time, so this enters, runs to its
    exit, and re-enters. It is deliberately signal-free: Session 1 measured a
    premium of about 3.8 volatility points on SPY, and the open question is not
    whether that premium exists but whether any timing rule beats collecting it
    blindly. This family IS that comparison.
    """
    return [Action.SELL_PUT_SPREAD] * len(closes)


def iv_rank_short_put_spread(closes: list[float], times, p: dict,
                             ivs: list[float] | None = None) -> list[Action]:
    """Sell a put spread only when implied volatility is high for this symbol.

    The hypothesis worth testing, from the Session 1 regime table: mean premium
    rose from +2.56 volatility points in the calm third to +5.06 in the
    stressed third, while the worst outcome was about the same in every third
    (-63.26 against -58.76). More reward for a tail that barely moved.

    Read that finding carefully. It rests on ONE crash, so it says the reward
    varies and says almost nothing trustworthy about the risk. The defined-risk
    structure, not this rule, is what bounds the loss.
    """
    n = len(closes)
    if ivs is None or len(ivs) != n:
        # No implied volatility means no rank. Firing anyway would silently
        # turn this into the passive family and report it under this name.
        return _none_series(n)
    lookback, floor = int(p["iv_lookback"]), float(p["iv_rank_min"])
    out = _none_series(n)
    for i in range(n):
        rank = iv_rank(ivs, i, lookback)
        if rank is not None and rank >= floor:
            out[i] = Action.SELL_PUT_SPREAD
    return out


# Exits for a credit spread, as fractions of CAPITAL AT RISK. The best case is
# the credit divided by the risk — about +19% on a 5%-wide spread — so the live
# bot's +50% target is unreachable and its bounds do not apply. Taking roughly
# half of the maximum profit is the conventional management of a short spread.
CREDIT_EXIT_GRID: dict[str, list] = {
    "take_profit_pct": [0.05, 0.08, 0.12],
    "stop_loss_pct": [0.30, 0.50, 0.70],
}
CREDIT_EXIT_BOUNDS: dict[str, tuple[float, float]] = {
    "take_profit_pct": (0.02, 0.18),
    "stop_loss_pct": (0.15, 1.00),
}


# --- the registry of families ---

FAMILIES: dict[str, Family] = {
    "baseline": Family(
        name="baseline",
        description="live EMA-cross trend + RSI trigger (current bot logic)",
        signal=ema_cross_rsi,
        grid={
            "ema_pair": [(8, 18), (9, 21), (12, 26)],
            "rsi_bull_level": [35.0, 40.0, 45.0],
            "rsi_bear_level": [55.0, 60.0, 65.0],
        },
        bounds={"ema_fast": (5, 15), "ema_slow": (18, 30),
                "rsi_bull_level": (25.0, 50.0), "rsi_bear_level": (50.0, 75.0)},
    ),
    "ema_slope": Family(
        name="ema_slope",
        description="slow-EMA slope trend + RSI trigger",
        signal=ema_slope_rsi,
        grid={
            "ema_slow": [18, 21, 26],
            "slope_bars": [10, 20],
            "rsi_bull_level": [35.0, 40.0, 45.0],
            "rsi_bear_level": [55.0, 60.0, 65.0],
        },
        bounds={"ema_slow": (18, 30), "slope_bars": (5, 40),
                "rsi_bull_level": (25.0, 50.0), "rsi_bear_level": (50.0, 75.0)},
    ),
    "rsi_only": Family(
        name="rsi_only",
        description="RSI cross, no trend filter (control)",
        signal=rsi_no_trend,
        grid={
            "rsi_bull_level": [30.0, 35.0, 40.0],
            "rsi_bear_level": [60.0, 65.0, 70.0],
        },
        bounds={"rsi_bull_level": (20.0, 50.0), "rsi_bear_level": (50.0, 80.0)},
    ),
    "donchian": Family(
        name="donchian",
        description="N-bar breakout momentum",
        signal=donchian_breakout,
        grid={"lookback": [20, 40, 55]},
        bounds={"lookback": (10, 100)},
    ),
    "short_put_passive": Family(
        name="short_put_passive",
        description="sell a put spread on every bar (passive premium harvest)",
        signal=always_short_put_spread,
        grid={},
        bounds={},
        exit_grid=CREDIT_EXIT_GRID,
        exit_bounds=CREDIT_EXIT_BOUNDS,
        dataset="vrp",
        credit=True,
    ),
    "short_put_iv_rank": Family(
        name="short_put_iv_rank",
        description="sell a put spread only when implied vol is high for this name",
        signal=iv_rank_short_put_spread,
        grid={
            "iv_lookback": [60, 120, 252],
            "iv_rank_min": [0.4, 0.6, 0.8],
        },
        bounds={"iv_lookback": (20, 504), "iv_rank_min": (0.0, 0.95)},
        exit_grid=CREDIT_EXIT_GRID,
        exit_bounds=CREDIT_EXIT_BOUNDS,
        needs_iv=True,
        dataset="vrp",
        baseline_family="short_put_passive",
        credit=True,
    ),
}

# Exit grid shared by every family; clamped by bot.config.clamp_tunables in
# the search, so exits can never leave the live TUNABLE_BOUNDS rails.
EXIT_GRID: dict[str, list] = {
    "take_profit_pct": [0.40, 0.50, 0.60],
    "stop_loss_pct": [0.20, 0.25, 0.30],
}


def clamp_params(params: dict, bounds: dict[str, tuple[float, float]]) -> dict:
    """Clamp known params into bounds, preserving int-ness. Unknown keys are
    dropped — a param without declared bounds has no business in a candidate."""
    out: dict = {}
    for key, (lo, hi) in bounds.items():
        if key not in params:
            continue
        value = max(lo, min(hi, float(params[key])))
        if isinstance(params[key], int):
            value = round(value)
        out[key] = value
    return out


def signal_candidates(family: Family) -> list[dict]:
    """Expand the family's grid into clamped candidate param dicts.

    A family with no grid has exactly ONE candidate: the empty parameter set.
    Returning nothing instead would silently skip such a family in the search,
    and the passive short-premium family — the benchmark every timing rule is
    measured against — is precisely a family with no parameters.
    """
    if not family.grid:
        return [{}]
    out = []
    for combo in itertools.product(*family.grid.values()):
        raw = dict(zip(family.grid.keys(), combo, strict=True))
        if "ema_pair" in raw:
            raw["ema_fast"], raw["ema_slow"] = raw.pop("ema_pair")
        clamped = clamp_params(raw, family.bounds)
        if clamped:
            out.append(clamped)
    return out
