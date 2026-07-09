"""Anti-reward-hacking checks a winner must pass before the holdout gate.

1. SimParams perturbation: the simulator prices options with four constants
   (delta, theta, spread cost). A search pointed at constants will find their
   seams — e.g. hold times that dodge a flat 5%/day theta. Re-running the
   winner with each constant halved and 1.5x'd exposes edges that only exist
   inside the model.

2. Daily-bar regime check: one year of 15-min bars is one market regime.
   Running the winner's signal on multi-year DAILY bars (a coarse proxy for
   the strategy, so we only ask for the SIGN of expectancy, not magnitude)
   checks the idea isn't an artifact of one regime. The daily cache was cut
   before the holdout window at fetch time, so this peeks at nothing.
"""

import itertools
from dataclasses import dataclass, replace as dc_replace
from datetime import datetime
from pathlib import Path

from bot.config import Settings
from bot.simulator import SimParams

from research import data
from research.families import Family
from research.search import run_family

_FACTORS = (0.5, 1.0, 1.5)
MIN_FOLD_TRADES = 10   # a yearly fold needs this many trades to count
MIN_FOLDS = 2          # and we need at least this many countable folds


@dataclass
class PerturbationRow:
    label: str
    trades: int
    expectancy: float


@dataclass
class RobustnessOutcome:
    perturbation_passed: bool
    perturbation_rows: list[PerturbationRow]
    regime_passed: bool
    regime_rows: list[PerturbationRow]   # label = year

    @property
    def passed(self) -> bool:
        return self.perturbation_passed and self.regime_passed


def perturbation_grid(base: SimParams) -> list[tuple[str, SimParams]]:
    out = []
    for f_delta, f_theta, f_cost in itertools.product(_FACTORS, repeat=3):
        label = f"delta x{f_delta} theta x{f_theta} cost x{f_cost}"
        out.append((label, dc_replace(
            base,
            delta=base.delta * f_delta,
            theta_daily=base.theta_daily * f_theta,
            roundtrip_cost=base.roundtrip_cost * f_cost,
        )))
    return out


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
                       data_dir: Path = data.DATA_DIR) -> tuple[bool, list[PerturbationRow]]:
    """Sign-consistency across year folds of the daily cache. Folds with too
    few trades are reported but don't count either way."""
    by_year: dict[int, dict] = {}
    for symbol in cfg.symbols:
        closes, times = data.load_bars("daily", symbol, data_dir)
        for year, group in _group_by_year(closes, times).items():
            by_year.setdefault(year, {})[symbol] = group

    rows = []
    evaluable = 0
    passed = True
    for year in sorted(by_year):
        result = run_family(by_year[year], cfg, sp, family, signal_params)
        rows.append(PerturbationRow(str(year), result.n, result.expectancy))
        if result.n >= MIN_FOLD_TRADES:
            evaluable += 1
            if result.expectancy <= 0:
                passed = False
    if evaluable < MIN_FOLDS:
        return False, rows  # not enough evidence is a fail, not a free pass
    return passed, rows


def _group_by_year(closes: list[float],
                   times: list[datetime]) -> dict[int, tuple[list[float], list[datetime]]]:
    out: dict[int, tuple[list[float], list[datetime]]] = {}
    for c, t in zip(closes, times):
        fold = out.setdefault(t.year, ([], []))
        fold[0].append(c)
        fold[1].append(t)
    return out
