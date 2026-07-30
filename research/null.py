"""The zero-skill baseline: what does a RANDOM signal earn on this window?

Every other guard in this package asks "is this result better than chance?"
using a statistical model of chance — the deflated Sharpe's expected-max
Sharpe, the improvement margin over the incumbent. This module asks it
empirically instead: run a coin flip through the exact same P&L engine on the
exact same bars, a couple of hundred times, and look at where the candidate
lands in that distribution.

Why this earns its place. A random signal SHOULD score about zero minus costs.
When it doesn't, the measurement layer is broken and every number computed on
top of it is furniture. That is not hypothetical here: this module was written
after a coin flip scored +11.1% expectancy per trade on the daily cache, which
was enough to pass robustness.daily_regime_check — whose only criterion was
`expectancy > 0`. The bug was in bot/simulator.py's exit accounting (see its
module docstring); the reason it survived 986 logged trials is that nothing
ever asked what zero looked like.

So the null is wired in as a threshold, not a report: a fold must beat its own
null's 95th percentile, not merely beat zero.

The random signal is deliberately built as a real Family and run through
research.search.run_family, so it takes the identical code path a genuine
candidate takes. A null with its own private simulator would drift away from
the thing it is supposed to be measuring — which is exactly the class of bug
it exists to catch.
"""

import logging
import math
import random
from dataclasses import dataclass
from datetime import datetime

from bot.config import Settings
from bot.simulator import SimParams
from bot.strategy import Action
from research.data import Bars
from research.families import Family

log = logging.getLogger("research.null")

DEFAULT_SEEDS = 200
# A fold needs this many null trades before its distribution means anything.
MIN_NULL_TRADES = 10
# Threshold quantile a real candidate must clear. Not 0.5: beating the median
# coin flip is a coin flip.
NULL_QUANTILE = 0.95


@dataclass(frozen=True)
class NullSummary:
    """The null distribution for one window: one expectancy per seed."""

    expectancies: list[float]
    trades: list[int]
    fire_rate: float

    @property
    def seeds(self) -> int:
        return len(self.expectancies)

    @property
    def mean_trades(self) -> float:
        return sum(self.trades) / len(self.trades) if self.trades else 0.0

    @property
    def mean(self) -> float:
        return sum(self.expectancies) / self.seeds if self.seeds else 0.0

    @property
    def threshold(self) -> float:
        """The bar a real candidate must clear on this window."""
        return percentile(self.expectancies, NULL_QUANTILE)

    def beats(self, expectancy: float) -> bool:
        """True when `expectancy` clears the threshold. An empty or
        under-traded null cannot license a pass, so it returns False."""
        if self.seeds == 0 or self.mean_trades < MIN_NULL_TRADES:
            return False
        return expectancy > self.threshold

    def percentile_of(self, expectancy: float) -> float:
        """Fraction of the null distribution this expectancy beats, in [0, 1]."""
        if self.seeds == 0:
            return 0.0
        return sum(1 for e in self.expectancies if e < expectancy) / self.seeds


def percentile(values: list[float], q: float) -> float:
    """Nearest-rank percentile, q in [0, 1]. Empty input scores 0.0 so a
    missing null never reads as a low (i.e. easy) bar."""
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = math.ceil(q * len(ordered)) - 1
    return ordered[min(len(ordered) - 1, max(0, idx))]


def null_family(seed: int, fire_rate: float) -> Family:
    """A Family whose signal is a coin flip: `fire_rate` chance of entering on
    any bar, then call or put with equal probability.

    Causal by construction — it never reads `closes` at all, so it cannot peek
    at the future, which is the one property the P&L engine requires.
    """

    def signal(closes: list[float], times: "list[datetime] | None",
               params: dict) -> list[Action]:
        # Seed off the series identity, not just `seed`, so the 30 symbols get
        # independent draws. Sharing one draw across a correlated universe
        # would pile every null trade onto the same bars and inflate the
        # spread of the distribution, making the threshold meaninglessly wide.
        anchor = int(times[0].timestamp()) if times else 0
        rng = random.Random(f"{seed}:{anchor}:{len(closes)}")
        out: list[Action] = []
        for _ in closes:
            if rng.random() >= fire_rate:
                out.append(Action.NONE)
            else:
                out.append(Action.BUY_CALL if rng.random() < 0.5 else Action.BUY_PUT)
        return out

    return Family(
        name=f"null:{seed}",
        description=f"coin-flip control, fire_rate={fire_rate:.4f}",
        signal=signal,
        grid={},
        bounds={},
    )


def fire_rate_for(windows: dict[str, Bars], target_trades: int) -> float:
    """Fire rate that lands the null near `target_trades` on these windows, so
    the comparison is like-for-like on sample size.

    Approximate on purpose: simulate() holds one position at a time and skips
    signals fired while in a trade, so realised trades come in below signals
    fired. Callers report the null's actual trade count alongside its
    expectancy rather than pretending the match is exact.
    """
    bars = sum(len(closes) for closes, _ in windows.values())
    if bars == 0 or target_trades <= 0:
        return 0.0
    return min(1.0, target_trades / bars)


def null_distribution(windows: dict[str, Bars], cfg: Settings, sp: SimParams,
                      target_trades: int, seeds: int = DEFAULT_SEEDS) -> NullSummary:
    """Run `seeds` independent coin flips over `windows` and collect one
    expectancy each. Seeds that produce no trades are dropped rather than
    scored as 0.0, which would drag the threshold toward zero."""
    # Local import: research.search imports this module to gate acceptance, so
    # a module-level import here would close the cycle. Same pattern as
    # search.resolve_family's local `blocks` import.
    from research.search import run_family

    fire_rate = fire_rate_for(windows, target_trades)
    if fire_rate <= 0:
        return NullSummary(expectancies=[], trades=[], fire_rate=0.0)

    expectancies: list[float] = []
    trades: list[int] = []
    for seed in range(seeds):
        result = run_family(windows, cfg, sp, null_family(seed, fire_rate), {})
        if result.n == 0:
            continue
        expectancies.append(result.expectancy)
        trades.append(result.n)

    log.debug("null over %d seeds: fire_rate=%.5f mean trades=%.1f mean expectancy=%+.4f",
              len(expectancies), fire_rate,
              sum(trades) / len(trades) if trades else 0.0,
              sum(expectancies) / len(expectancies) if expectancies else 0.0)
    return NullSummary(expectancies=expectancies, trades=trades, fire_rate=fire_rate)
