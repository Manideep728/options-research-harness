"""Self-improvement with hard guidelines.

The tuner may ONLY propose values for the signal/exit parameters listed in
config.TUNABLE_BOUNDS. Candidates are ranked and picked on the TRAINING
window only; the validation window is touched exactly once, to check the
single winner, so it stays genuinely unseen by the selection process
(ranking on validation, then reporting that same score as evidence, is
selection bias — across ~200 grid candidates the best validation score would
be partly luck). A proposal is only accepted when ALL of these hold:

  1. Risk caps (position limits, sizing, daily limits, circuit breaker) are
     untouchable — they are not in the search space at all.
  2. Every candidate value is clamped into TUNABLE_BOUNDS (twice: here and
     again when the bot loads tuned_params.json).
  3. >= MIN_TRADES trades on BOTH the training and validation windows —
     no conclusions from a handful of lucky trades.
  4. The winner's validation expectancy must be positive AND beat the
     current parameters' validation expectancy by IMPROVE_FACTOR (or by an
     absolute margin when the current expectancy is not positive).

If nothing qualifies, the current parameters stand. Every accepted change is
written to tuned_params.json WITH its evidence, so it can be audited later.
"""

import itertools
import json
import logging
from dataclasses import dataclass, replace
from datetime import datetime, timezone

from bot.atomic import atomic_write_text
from bot.config import Settings, clamp_tunables
from bot.simulator import SimParams, SimResult, simulate

log = logging.getLogger("bot.tuner")

# --- hard guidelines (not user-tunable via any config file) ---
MIN_TRADES = 30
IMPROVE_FACTOR = 1.10      # must beat current val expectancy by 10%...
IMPROVE_ABS_MARGIN = 0.01  # ...or by 1% of premium if current is <= 0
TRAIN_FRACTION = 0.70

# RSI levels near the midline are included deliberately: with a trend filter
# active, RSI(14) rarely reaches classic extremes (measured on real data —
# 35/65 fired ~zero times in 120 days of SPY/QQQ 15-min bars).
GRID: dict[str, list] = {
    "ema_pair": [(8, 18), (9, 21), (12, 26)],
    "rsi_bull_level": [35.0, 40.0, 45.0],
    "rsi_bear_level": [55.0, 60.0, 65.0],
    "take_profit_pct": [0.40, 0.50, 0.60],
    "stop_loss_pct": [0.20, 0.25, 0.30],
}


@dataclass
class Windows:
    """Per-symbol (closes, times) split into train/validation slices."""
    train: dict[str, tuple[list[float], list]]
    val: dict[str, tuple[list[float], list]]


@dataclass
class TuneOutcome:
    accepted: bool
    reason: str
    params: dict
    current_val: SimResult
    best_val: SimResult | None


def split_windows(bars_by_symbol: dict[str, tuple[list[float], list]]) -> Windows:
    train: dict = {}
    val: dict = {}
    for sym, (closes, times) in bars_by_symbol.items():
        cut = int(len(closes) * TRAIN_FRACTION)
        train[sym] = (closes[:cut], times[:cut])
        val[sym] = (closes[cut:], times[cut:])
    return Windows(train=train, val=val)


def run_all(windows: dict[str, tuple[list[float], list]], cfg: Settings,
            sp: SimParams) -> SimResult:
    combined = SimResult()
    for closes, times in windows.values():
        combined.trades.extend(simulate(closes, times, cfg, sp).trades)
    combined.trades.sort(key=lambda t: t.entry_time)
    return combined


def candidate_params() -> list[dict]:
    out = []
    for combo in itertools.product(*GRID.values()):
        raw = dict(zip(GRID.keys(), combo))
        ema_fast, ema_slow = raw.pop("ema_pair")
        raw["ema_fast"], raw["ema_slow"] = ema_fast, ema_slow
        clamped = clamp_tunables(raw)  # guideline 2
        if clamped:
            out.append(clamped)
    return out


def tune(bars_by_symbol: dict[str, tuple[list[float], list]], cfg: Settings,
         sp: SimParams = SimParams()) -> TuneOutcome:
    """Pick the winner on TRAIN expectancy only, then check that one winner
    against validation. Ranking on validation (like picking whichever
    candidate scores highest on val, then reporting that same score as
    evidence) is selection bias: across ~200 grid candidates the best
    validation score is partly luck, so it would overstate the edge by
    construction. Scoring on train and validating once keeps validation data
    genuinely unseen by the selection process."""
    w = split_windows(bars_by_symbol)
    current_val = run_all(w.val, cfg, sp)

    best_train: SimResult | None = None
    best_params: dict = {}
    for params in candidate_params():
        candidate_cfg = replace(cfg, **params)
        train_result = run_all(w.train, candidate_cfg, sp)
        if train_result.n < MIN_TRADES:  # guideline 3 (train side)
            continue
        if best_train is None or train_result.expectancy > best_train.expectancy:
            best_train, best_params = train_result, params

    if best_train is None:
        return TuneOutcome(False, "no candidate produced enough training trades",
                           {}, current_val, None)

    best_val = run_all(w.val, replace(cfg, **best_params), sp)
    if best_val.n < MIN_TRADES:  # guideline 3 (validation side)
        return TuneOutcome(False, "winning candidate lacked enough validation trades",
                           best_params, current_val, best_val)
    if best_val.expectancy <= 0:  # guideline 4
        return TuneOutcome(False, "best candidate still has non-positive expectancy",
                           best_params, current_val, best_val)

    if current_val.expectancy > 0:
        required = current_val.expectancy * IMPROVE_FACTOR
    else:
        required = current_val.expectancy + IMPROVE_ABS_MARGIN
    if best_val.expectancy < required:  # guideline 4
        return TuneOutcome(
            False,
            f"improvement too small (val expectancy {best_val.expectancy:.3f} "
            f"< required {required:.3f})",
            best_params, current_val, best_val,
        )

    return TuneOutcome(True, "passed all guidelines", best_params, current_val, best_val)


def write_tuned_params(path: str, outcome: TuneOutcome) -> None:
    payload = {
        "params": outcome.params,
        "evidence": {
            "validation_trades": outcome.best_val.n,
            "validation_expectancy": round(outcome.best_val.expectancy, 4),
            "validation_win_rate": round(outcome.best_val.win_rate, 4),
            "validation_profit_factor": round(outcome.best_val.profit_factor, 4),
            "previous_val_expectancy": round(outcome.current_val.expectancy, 4),
        },
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    atomic_write_text(path, json.dumps(payload, indent=2))
    log.info("wrote %s: %s", path, outcome.params)
