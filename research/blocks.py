"""Composable strategy blocks + the proposal spec format (Phase 2).

An LLM (or human) proposes a new strategy family as a JSON *spec* that picks
from this FIXED vocabulary: one trend block x one trigger block x optional
entry filters, plus a parameter grid. The proposer never writes code — a
spec can only combine blocks that exist here, every parameter is clamped
into PARAM_BOUNDS, and the grid size is capped. Those are the rails that
keep an automated proposer from smuggling in arbitrary logic, unbounded
parameters, or a multiplicity explosion.

Spec shape:
{
  "name": "low-vol-calls",
  "hypothesis": "high-volatility entries lose; trade calls in calm uptrends",
  "trend": "ema_cross" | "ema_slope" | "none",
  "trigger": "rsi_cross" | "donchian",
  "filters": ["calls_only" | "puts_only" | "max_entry_vol" | "min_entry_vol"
              | "entry_hours", ...],
  "grid": {"<param>": [values...], ...}   # one list per required param
}
"""

import itertools
import statistics
from datetime import datetime
from typing import Any

from bot.indicators import ema, rsi
from bot.strategy import Action
from research.families import RSI_PERIOD, Family, _none_series, _rsi_cross

TRENDS = ("ema_cross", "ema_slope", "none")
TRIGGERS = ("rsi_cross", "donchian")
FILTERS = ("calls_only", "puts_only", "max_entry_vol", "min_entry_vol",
           "entry_hours")

# Params each block needs a grid for. entry_hours values are LISTS of UTC
# hours (a grid over hour-sets), everything else is a list of numbers.
REQUIRED_PARAMS: dict[str, tuple[str, ...]] = {
    "ema_cross": ("ema_fast", "ema_slow"),
    "ema_slope": ("ema_slow", "slope_bars"),
    "none": (),
    "rsi_cross": ("rsi_bull_level", "rsi_bear_level"),
    "donchian": ("lookback",),
    "max_entry_vol": ("max_entry_vol",),
    "min_entry_vol": ("min_entry_vol",),
    "entry_hours": ("entry_hours",),
    "calls_only": (),
    "puts_only": (),
}

PARAM_BOUNDS: dict[str, tuple[float, float]] = {
    "ema_fast": (5, 15),
    "ema_slow": (18, 30),
    "slope_bars": (5, 40),
    "rsi_bull_level": (25.0, 50.0),
    "rsi_bear_level": (50.0, 75.0),
    "lookback": (10, 100),
    # per-bar return stdev on 15-min bars; ~0.001 is calm, ~0.01 is wild
    "max_entry_vol": (0.0005, 0.05),
    "min_entry_vol": (0.0005, 0.05),
}

MAX_CANDIDATES = 500  # signal-grid cap, before the shared exit grid
VOL_LOOKBACK = 14     # bars of returns for the entry-vol filters


# --- validation ---

def validate_spec(spec: Any) -> list[str]:
    """All problems with a spec, as human/LLM-readable strings. [] = valid.

    Typed `Any` deliberately: the input is untrusted JSON from an LLM, so the
    isinstance guards below are real runtime checks, not dead code.
    """
    errors: list[str] = []
    if not isinstance(spec, dict):
        return ["spec must be a JSON object"]
    if not str(spec.get("name", "")).strip():
        errors.append("missing 'name'")
    if spec.get("trend") not in TRENDS:
        errors.append(f"'trend' must be one of {TRENDS}")
    if spec.get("trigger") not in TRIGGERS:
        errors.append(f"'trigger' must be one of {TRIGGERS}")
    filters = spec.get("filters", [])
    if not isinstance(filters, list) or any(f not in FILTERS for f in filters):
        errors.append(f"'filters' must be a list drawn from {FILTERS}")
        filters = [f for f in filters if f in FILTERS] if isinstance(filters, list) else []
    if "calls_only" in filters and "puts_only" in filters:
        errors.append("'calls_only' and 'puts_only' are mutually exclusive")

    grid = spec.get("grid", {})
    if not isinstance(grid, dict):
        return [*errors, "'grid' must be an object of param -> list of values"]
    blocks = [spec.get("trend"), spec.get("trigger"), *filters]
    required = [p for b in blocks for p in REQUIRED_PARAMS.get(b, ())]
    for param in required:
        values = grid.get(param)
        if not isinstance(values, list) or not values:
            errors.append(f"grid must provide a non-empty list for '{param}'")
        elif param == "entry_hours":
            if not all(isinstance(v, list) and v
                       and all(isinstance(h, int) and 0 <= h <= 23 for h in v)
                       for v in values):
                errors.append("'entry_hours' values must be non-empty lists of UTC hours 0-23")
        elif not all(isinstance(v, (int, float)) for v in values):
            errors.append(f"grid values for '{param}' must be numbers")
    for param in grid:
        if param not in required:
            errors.append(f"grid param '{param}' is not used by the chosen blocks")

    if not errors:
        size = len(spec_candidates(spec))
        if size == 0:
            errors.append("grid expands to zero valid candidates "
                          "(check ema_slow - ema_fast > 3)")
        elif size > MAX_CANDIDATES:
            errors.append(f"grid expands to {size} candidates (max {MAX_CANDIDATES})")
    return errors


def spec_candidates(spec: dict) -> list[dict]:
    """Expand the spec's grid into clamped candidate param dicts."""
    grid: dict = spec.get("grid", {})
    if not grid:
        return [{}] if not any(
            REQUIRED_PARAMS.get(b, ())
            for b in (spec.get("trend"), spec.get("trigger"), *spec.get("filters", []))
        ) else []
    keys = list(grid.keys())
    out = []
    for combo in itertools.product(*(grid[k] for k in keys)):
        params = dict(zip(keys, combo, strict=True))
        clamped = _clamp(params)
        # a near-equal EMA pair makes the trend filter meaningless
        if (
            "ema_fast" in clamped
            and "ema_slow" in clamped
            and clamped["ema_slow"] - clamped["ema_fast"] <= 3
        ):
            continue
        out.append(clamped)
    return out


def _clamp(params: dict) -> dict:
    out: dict = {}
    for key, value in params.items():
        if key == "entry_hours":
            out[key] = sorted({int(h) for h in value if 0 <= int(h) <= 23})
            continue
        lo, hi = PARAM_BOUNDS[key]
        clamped = max(lo, min(hi, float(value)))
        out[key] = round(clamped) if isinstance(value, int) else clamped
    return out


# --- signal composition ---

def spec_signal(closes: list[float], times: list[datetime] | None,
                spec: dict, p: dict) -> list[Action]:
    """Causal composed signal: trigger gated by trend, then entry filters."""
    n = len(closes)
    trend, trigger = spec["trend"], spec["trigger"]
    filters = spec.get("filters", [])

    needed = 2
    if trigger == "rsi_cross":
        needed = max(needed, RSI_PERIOD + 2)
    else:  # donchian
        needed = max(needed, int(p["lookback"]) + 1)
    if trend == "ema_cross":
        needed = max(needed, int(p["ema_slow"]) + 1)
    elif trend == "ema_slope":
        needed = max(needed, int(p["ema_slow"]) + int(p["slope_bars"]) + 1)
    if n < needed:
        return _none_series(n)

    uptrend = _trend_series(closes, trend, p)          # bool | None per bar
    r = rsi(closes, RSI_PERIOD) if trigger == "rsi_cross" else None
    vols = (_vol_series(closes)
            if ("max_entry_vol" in filters or "min_entry_vol" in filters) else None)

    out = _none_series(n)
    for i in range(needed - 1, n):
        # `r` is non-None exactly when the trigger is rsi_cross; testing it
        # directly (rather than re-testing `trigger`) lets the type checker
        # see that too.
        if r is not None:
            action = _rsi_cross(r[i - 1], r[i], p["rsi_bull_level"],
                                p["rsi_bear_level"], uptrend[i])
        else:
            action = _donchian_at(closes, i, int(p["lookback"]), uptrend[i])
        if action == Action.NONE:
            continue
        if "calls_only" in filters and action == Action.BUY_PUT:
            continue
        if "puts_only" in filters and action == Action.BUY_CALL:
            continue
        if vols is not None:
            if "max_entry_vol" in filters and vols[i] > p["max_entry_vol"]:
                continue
            if "min_entry_vol" in filters and vols[i] < p["min_entry_vol"]:
                continue
        if "entry_hours" in filters and (
            times is None or times[i].hour not in p["entry_hours"]
        ):
            continue
        out[i] = action
    return out


def _trend_series(closes: list[float], trend: str, p: dict) -> list:
    n = len(closes)
    if trend == "none":
        return [None] * n
    ema_s = ema(closes, int(p["ema_slow"]))
    if trend == "ema_cross":
        ema_f = ema(closes, int(p["ema_fast"]))
        return [ema_f[i] > ema_s[i] for i in range(n)]
    k = int(p["slope_bars"])
    return [ema_s[i] > ema_s[i - k] if i >= k else False for i in range(n)]


def _donchian_at(closes: list[float], i: int, lookback: int,
                 uptrend) -> Action:
    window = closes[i - lookback:i]
    if uptrend in (None, True) and closes[i] > max(window):
        return Action.BUY_CALL
    if uptrend in (None, False) and closes[i] < min(window):
        return Action.BUY_PUT
    return Action.NONE


def _vol_series(closes: list[float]) -> list[float]:
    """Rolling stdev of the last VOL_LOOKBACK simple returns, per bar
    (same measure the scanner and failure report use)."""
    n = len(closes)
    out = [0.0] * n
    returns = [0.0] * n
    for i in range(1, n):
        returns[i] = (closes[i] - closes[i - 1]) / closes[i - 1] if closes[i - 1] else 0.0
    for i in range(2, n):
        window = returns[max(1, i - VOL_LOOKBACK + 1):i + 1]
        if len(window) >= 2:
            out[i] = statistics.pstdev(window)
    return out


def spec_to_family(spec: dict) -> Family:
    """Wrap a spec as a Family so it runs through the identical search,
    robustness, and gate pipeline as the built-in families."""
    return Family(
        name=f"spec:{spec['name']}",
        description=spec.get("hypothesis", ""),
        signal=lambda closes, times, p: spec_signal(closes, times, spec, p),
        grid={},      # candidates come from spec_candidates(), not this
        bounds=PARAM_BOUNDS,
    )
