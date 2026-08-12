"""Anti-reward-hacking checks a winner must pass before the holdout gate.

1. SimParams perturbation: the simulator prices options with four constants
   (delta, theta, spread cost). A search pointed at constants will find their
   seams — e.g. hold times that dodge a flat 5%/day theta. Re-running the
   winner with each constant halved and 1.5x'd exposes edges that only exist
   inside the model.

2. Daily-bar regime check: one year of 15-min bars is one market regime.
   Running the winner's signal on multi-year DAILY bars (a coarse proxy for
   the strategy, so we only ask whether it beats chance, not by how much)
   checks the idea isn't an artifact of one regime. The daily cache was cut
   before the holdout window at fetch time, so this peeks at nothing.

   This check used to pass a fold on `expectancy > 0`, which a coin flip
   cleared at +11.1% per trade — see research/null.py. A fold now has to beat
   its OWN null distribution's 95th percentile, measured on the same bars with
   the same P&L model, so a bar-resolution artifact lifts the bar it has to
   clear instead of handing out a free pass.

   Crossing from 15-minute to daily bars also changes what per-bar parameters
   MEAN. An entry-volatility threshold of 0.002 is "calm" for a 15-min bar and
   unreachable for a daily one — the pending `calm-uptrend-calls` spec matched
   zero daily bars across all six year folds, so the check reported FAIL for a
   unit mismatch rather than an economic reason, and no spec using that filter
   could ever have passed. Bar-relative params are now rescaled (see
   blocks.rescale_to_daily) and params with no daily equivalent at all are
   reported as NOT EVALUABLE, which is distinct from failing.
"""

import itertools
import statistics
from dataclasses import dataclass, field
from dataclasses import replace as dc_replace
from datetime import datetime
from pathlib import Path

from bot.config import Settings
from bot.simulator import SimParams
from research import blocks, data, null
from research.families import Family
from research.search import run_family

_FACTORS = (0.5, 1.0, 1.5)
MIN_FOLD_TRADES = 10   # a yearly fold needs this many trades to count
MIN_FOLDS = 2          # and we need at least this many countable folds
# Seeds per fold. Fewer than research.null.DEFAULT_SEEDS because the regime
# check runs one null per year fold; 100 still resolves a 95th percentile.
REGIME_NULL_SEEDS = 100
# Regular US session length, for converting intraday bars/day.
SESSION_MINUTES = 390  # 09:30-16:00 ET
# Tail limits, as fractions of capital at risk. A single trade may not lose
# more than the whole of the capital the structure puts at risk, and the
# cumulative give-back may not exceed MAX_DRAWDOWN of one position's risk.
# These judge the LOSS side, which expectancy cannot see.
MAX_WORST_TRADE = 1.0
MAX_DRAWDOWN = 8.0


def bars_per_day(cfg: Settings) -> float:
    return SESSION_MINUTES / max(1, cfg.bar_timeframe_minutes)


@dataclass
class PerturbationRow:
    label: str
    trades: int
    expectancy: float
    # Populated for regime folds only: the null bar this fold had to clear.
    null_threshold: float | None = field(default=None)
    null_trades: float | None = field(default=None)


def perturbation_grid(base: SimParams) -> list[tuple[str, SimParams]]:
    """The three constants the pricing model cannot derive from the bars.

    These replaced delta and theta, which are now outputs of bot/pricing.py
    rather than inputs. Implied volatility sets the premium and therefore the
    gearing; days to expiry sets how fast the premium decays; the round trip is
    the spread. A candidate that only works at one implied volatility has found
    a seam in the pricing model, not an edge in the market.
    """
    out = []
    for f_iv, f_dte, f_cost in itertools.product(_FACTORS, repeat=3):
        label = f"iv x{f_iv} dte x{f_dte} cost x{f_cost}"
        out.append((label, dc_replace(
            base,
            iv=base.iv * f_iv,
            dte_days=base.dte_days * f_dte,
            roundtrip_cost=base.roundtrip_cost * f_cost,
        )))
    return out


@dataclass
class TailRow:
    """What a strategy loses when it is wrong, not what it makes on average."""

    trades: int
    expectancy: float
    worst_trade: float
    max_drawdown: float
    loss_ratio: float          # worst single loss / mean gain


def check_tail(result, max_worst: float = MAX_WORST_TRADE,
               max_dd: float = MAX_DRAWDOWN) -> tuple[bool, TailRow]:
    """A separate pass/fail on the LOSS side.

    Expectancy cannot judge a short-premium strategy. Selling options wins
    small and often and loses large and rarely, so a book that is about to
    destroy itself and one that is sound look identical on the average — right
    up until the day they do not. Session 1 measured the shape on real data:
    the mean variance risk premium is about +3.8 volatility points and the
    worst single cycle is -63, a ratio near 17 to 1.

    The two limits are on capital at risk, which is what the structure bounds.
    A defined-risk spread cannot lose more than -1 on one trade, so a worst
    trade at the floor is not automatically a failure — the drawdown is what
    says whether those losses arrive together.
    """
    returns = [t.pnl_pct for t in result.trades]
    if not returns:
        return False, TailRow(0, 0.0, 0.0, 0.0, 0.0)
    worst = min(returns)
    gains = [r for r in returns if r > 0]
    mean_gain = statistics.fmean(gains) if gains else 0.0
    row = TailRow(
        trades=len(returns),
        expectancy=statistics.fmean(returns),
        worst_trade=worst,
        max_drawdown=result.max_drawdown,
        loss_ratio=abs(worst) / mean_gain if mean_gain > 0 else float("inf"),
    )
    passed = worst >= -abs(max_worst) and row.max_drawdown <= max_dd
    return passed, row


def check_perturbations(windows: dict, cfg: Settings, family: Family,
                        signal_params: dict,
                        base: SimParams = SimParams()) -> tuple[bool, list[PerturbationRow]]:
    """Winner must keep positive expectancy under every perturbed SimParams.
    (Entries don't move — only the P&L model does — so a genuine edge in the
    underlying's direction survives; a modeling artifact doesn't.)"""
    rows = []
    passed = True
    for label, sp in perturbation_grid(base):
        result = run_family(windows, cfg, sp, family, signal_params)
        rows.append(PerturbationRow(label, result.n, result.expectancy))
        if result.n > 0 and result.expectancy <= 0:
            passed = False
    return passed, rows


def daily_regime_check(cfg: Settings, family: Family, signal_params: dict,
                       sp: SimParams = SimParams(),
                       data_dir: Path = data.DATA_DIR,
                       null_seeds: int = REGIME_NULL_SEEDS
                       ) -> tuple[bool, list[PerturbationRow], str]:
    """Every countable year fold must beat its own coin-flip null. Folds with
    too few trades are reported but don't count either way.

    Returns (passed, rows, note). `note` is non-empty when the check could not
    be run as specified — either because a param has no daily equivalent, or to
    record that bar-relative params were rescaled.
    """
    blocked = [p for p in blocks.INTRADAY_ONLY_PARAMS if p in signal_params]
    if blocked:
        return False, [], (
            f"NOT EVALUABLE on daily bars: {', '.join(blocked)} is defined in "
            "intraday terms and has no daily equivalent. This is not evidence "
            "against the candidate — it means this check cannot judge it."
        )

    daily_params = blocks.rescale_to_daily(signal_params, bars_per_day(cfg))
    rescaled = {k: (signal_params[k], daily_params[k])
                for k in blocks.BAR_RELATIVE_PARAMS if k in signal_params}
    note = ""
    if rescaled:
        note = "rescaled per-bar params for daily bars: " + ", ".join(
            f"{k} {before:g} -> {after:.4g}" for k, (before, after) in rescaled.items()
        )

    by_year: dict[int, dict] = {}
    for symbol in cfg.symbols:
        closes, times = data.load_bars("daily", symbol, data_dir)
        for year, group in _group_by_year(closes, times).items():
            by_year.setdefault(year, {})[symbol] = group

    rows = []
    evaluable = 0
    passed = True
    for year in sorted(by_year):
        result = run_family(by_year[year], cfg, sp, family, daily_params)
        if result.n < MIN_FOLD_TRADES:
            rows.append(PerturbationRow(str(year), result.n, result.expectancy))
            continue
        # Size the null to this fold's trade count so the comparison is
        # like-for-like on sample size, not just on mean.
        summary = null.null_distribution(by_year[year], cfg, sp, result.n, seeds=null_seeds)
        rows.append(PerturbationRow(str(year), result.n, result.expectancy,
                                    null_threshold=summary.threshold,
                                    null_trades=summary.mean_trades))
        evaluable += 1
        if not summary.beats(result.expectancy):
            passed = False
    if evaluable < MIN_FOLDS:
        return False, rows, (note + ("; " if note else "")
                             + f"only {evaluable} fold(s) had >= {MIN_FOLD_TRADES} "
                               f"trades; {MIN_FOLDS} are required")
    return passed, rows, note


def _group_by_year(closes: list[float],
                   times: list[datetime]) -> dict[int, tuple[list[float], list[datetime]]]:
    out: dict[int, tuple[list[float], list[datetime]]] = {}
    for c, t in zip(closes, times, strict=True):
        fold = out.setdefault(t.year, ([], []))
        fold[0].append(c)
        fold[1].append(t)
    return out
