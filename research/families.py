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
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from bot.indicators import ema, rsi
from bot.strategy import Action

RSI_PERIOD = 14  # fixed, as in the live bot

# Signal contract: (closes, times, params) -> list[Action], causal.
# `times` (bar timestamps, parallel to closes) exists for blocks that need
# the clock (e.g. entry-hour filters); the built-in families ignore it.
SignalFn = Callable[[list[float], "list[datetime] | None", dict], "list[Action]"]


@dataclass(frozen=True)
class Family:
    name: str
    description: str
    signal: SignalFn
    grid: dict[str, list]                       # signal params only
    bounds: dict[str, tuple[float, float]]


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
            value = int(round(value))
        out[key] = value
    return out


def signal_candidates(family: Family) -> list[dict]:
    """Expand the family's grid into clamped candidate param dicts."""
    out = []
    for combo in itertools.product(*family.grid.values()):
        raw = dict(zip(family.grid.keys(), combo))
        if "ema_pair" in raw:
            raw["ema_fast"], raw["ema_slow"] = raw.pop("ema_pair")
        clamped = clamp_params(raw, family.bounds)
        if clamped:
            out.append(clamped)
    return out
