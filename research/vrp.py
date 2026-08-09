"""Does the variance risk premium exist in this data? The kill criterion.

The thesis under test: implied volatility systematically exceeds the volatility
that subsequently shows up, so option SELLERS collect the difference. The bot is
currently a structural option buyer, i.e. on the other side of it.

Why this module runs before any strategy code exists. The repo already spent 987
trials searching for edge inside a measurement layer nobody had validated, and
the whole finding was that the edge belonged to the simulator rather than the
market. The correction is not to search more carefully — it is to ask whether
the effect is present at all before building machinery to harvest it. That
question is answerable from two public series and no bot code, so it is asked
here first, and a pre-registered threshold decides whether the rest happens.

What is deliberately NOT modelled here: option prices, strikes, spreads,
assignment, or dollars. This measures the effect in VOLATILITY POINTS — the
shape and sign of the premium, not the P&L of any particular trade. Turning vol
points into dollars needs the Black-Scholes work of the next session, and doing
it by eye here would be exactly the "assume the answer" move this module exists
to avoid.

Two approximations, both stated rather than hidden:

1. VIX prices a risk-neutral variance strip; what is compared against it is
   close-to-close realized volatility. That is the standard VRP measurement, but
   the two are not the same estimator, and the gap between them is part of what
   the premium is usually explained by.
2. VIX measures SPX. Realized vol is computed on SPY, which differs by
   dividends — small and smooth next to volatility, and the conventional
   stand-in.
"""

import math
import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime

TRADING_DAYS = 252
# VIX looks 30 calendar days ahead; 21 trading days is that span in bars, and
# the realized side must cover the same window or the two are not comparable.
HORIZON_BARS = 21
# Round-trip cost on the option, as a fraction of its premium. 0.03 is what
# bot/simulator.py's SimParams assumes; see cost_hurdle for why the honest
# number is probably higher.
DEFAULT_ROUNDTRIP = 0.03


@dataclass(frozen=True)
class VrpRow:
    """One observation: implied vol today, realized vol over the NEXT window."""

    day: date
    implied: float      # annualized vol in percentage points, e.g. 15.3
    realized: float     # annualized realized vol over the following horizon

    @property
    def vrp(self) -> float:
        return self.implied - self.realized


def log_returns(closes: Sequence[float]) -> list[float]:
    """Consecutive log returns. Non-positive prices are a data defect, not a
    condition to model, so they raise rather than silently producing nan."""
    out: list[float] = []
    for i in range(1, len(closes)):
        prev, now = closes[i - 1], closes[i]
        if prev <= 0 or now <= 0:
            raise ValueError(f"non-positive close at index {i}: {prev} -> {now}")
        out.append(math.log(now / prev))
    return out


def realized_vol(closes: Sequence[float]) -> float:
    """Annualized realized volatility in percentage points, zero-mean.

    Zero-mean (root of the mean SQUARE, not the variance about the sample mean)
    because that is the quantity a variance swap pays on, and VIX is priced off
    the variance strip. Over a 21-bar window the drift is negligible and
    estimating it would only add noise.
    """
    returns = log_returns(closes)
    if not returns:
        return 0.0
    mean_square = sum(r * r for r in returns) / len(returns)
    return math.sqrt(mean_square * TRADING_DAYS) * 100.0


def forward_realized_vol(closes: Sequence[float],
                         horizon: int = HORIZON_BARS) -> list[float | None]:
    """Realized vol over the `horizon` bars AFTER each index.

    Element i uses closes[i .. i+horizon], i.e. returns dated strictly after i,
    so an implied reading at i is compared only against vol it could not have
    observed. Indices without a full forward window are None — dropped, never
    truncated, because a short window reads as unnaturally low volatility and
    would inflate the measured premium exactly at the end of the sample.
    """
    out: list[float | None] = []
    for i in range(len(closes)):
        if i + horizon >= len(closes):
            out.append(None)
            continue
        out.append(realized_vol(closes[i:i + horizon + 1]))
    return out


def build_rows(implied_closes: Sequence[float], implied_times: Sequence[datetime],
               under_closes: Sequence[float], under_times: Sequence[datetime],
               horizon: int = HORIZON_BARS) -> list[VrpRow]:
    """Align the two series by calendar date and pair each implied reading with
    the realized vol that followed it.

    The join is on date because the index and the ETF are separate feeds with
    their own holiday handling; pairing by position would silently offset the
    two series after a single mismatched day.
    """
    forward = forward_realized_vol(under_closes, horizon)
    realized_by_day = {
        t.date(): value
        for t, value in zip(under_times, forward, strict=True)
        if value is not None
    }
    rows: list[VrpRow] = []
    for stamp, implied in zip(implied_times, implied_closes, strict=True):
        realized = realized_by_day.get(stamp.date())
        if realized is None:
            continue
        rows.append(VrpRow(day=stamp.date(), implied=implied, realized=realized))
    rows.sort(key=lambda r: r.day)
    return rows


def cycles(rows: Sequence[VrpRow], horizon: int = HORIZON_BARS) -> list[VrpRow]:
    """Non-overlapping observations — one per horizon.

    Every consecutive row shares 20 of its 21 forward days with its neighbour,
    so the daily series is one sample repeated, not many samples. It is fine for
    estimating the MEAN but useless for judging risk: overlapping windows smear
    a single crash across 21 rows and flatter every drawdown. A short-vol
    strategy is judged on exactly that tail, so drawdown is measured here, on
    independent sell-and-hold-to-expiry cycles.
    """
    return list(rows[::horizon]) if horizon > 0 else list(rows)


def max_drawdown(values: Sequence[float]) -> float:
    """Worst peak-to-trough fall of the cumulative sum. Zero for empty input."""
    peak = running = worst = 0.0
    for value in values:
        running += value
        peak = max(peak, running)
        worst = max(worst, peak - running)
    return worst


def cost_hurdle(implied: float, roundtrip: float = DEFAULT_ROUNDTRIP) -> float:
    """The premium a seller must clear just to pay the spread, in vol points.

    For a near-ATM option, premium ~= 0.4 * sigma * sqrt(T) * S and vega (per
    vol point) ~= 0.4 * sqrt(T) * S, so paying `roundtrip` of the premium costs
    premium * roundtrip / vega = sigma * roundtrip vol points. The maturity and
    spot cancel, which is why this is expressible without either.

    0.03 is what SimParams assumes. The live liquidity gate permits a spread up
    to 10% of mid, so the worst tolerated round trip is nearer 6% — pass that in
    to see the pessimistic hurdle.
    """
    return implied * roundtrip


@dataclass(frozen=True)
class VrpSummary:
    """The verdict on one index/underlying pair."""

    name: str
    symbol: str
    rows: list[VrpRow]                     # overlapping: for the mean
    independent: list[VrpRow]              # non-overlapping: for the tail

    @property
    def n(self) -> int:
        return len(self.rows)

    @property
    def mean(self) -> float:
        return statistics.fmean([r.vrp for r in self.rows]) if self.rows else 0.0

    @property
    def median(self) -> float:
        return statistics.median([r.vrp for r in self.rows]) if self.rows else 0.0

    @property
    def mean_implied(self) -> float:
        return statistics.fmean([r.implied for r in self.rows]) if self.rows else 0.0

    @property
    def share_positive(self) -> float:
        if not self.rows:
            return 0.0
        return sum(1 for r in self.rows if r.vrp > 0) / len(self.rows)

    @property
    def worst(self) -> VrpRow | None:
        return min(self.rows, key=lambda r: r.vrp) if self.rows else None

    @property
    def drawdown(self) -> float:
        """Worst cumulative loss a passive seller would have ridden, in vol
        points, measured on independent cycles."""
        return max_drawdown([r.vrp for r in self.independent])

    def hurdle(self, roundtrip: float = DEFAULT_ROUNDTRIP) -> float:
        return cost_hurdle(self.mean_implied, roundtrip)

    def clears(self, roundtrip: float = DEFAULT_ROUNDTRIP) -> bool:
        """The pre-registered kill criterion: the premium must be positive AND
        larger than what it costs to go and collect it."""
        return self.n > 0 and self.mean > self.hurdle(roundtrip)

    def by_regime(self) -> list[tuple[str, int, float, float]]:
        """(label, n, mean VRP, worst VRP) split by the implied vol AT ENTRY.

        The worst column is not decoration. Ranking regimes by mean alone reads
        as "sell when implied is already high", which is backwards about the
        risk: the premium is collected in calm markets and the catastrophes
        START there, because a seller is short the transition. Feb 2020 entered
        at an implied of 15.6 — the calm bucket — and realized 79. A report that
        showed only the means would hide that.
        """
        if len(self.rows) < 3:
            return []
        ordered = sorted(r.implied for r in self.rows)
        low = ordered[len(ordered) // 3]
        high = ordered[2 * len(ordered) // 3]
        buckets: dict[str, list[float]] = {"calm": [], "normal": [], "stressed": []}
        for row in self.rows:
            label = ("calm" if row.implied <= low
                     else "stressed" if row.implied > high else "normal")
            buckets[label].append(row.vrp)
        return [(label, len(v), statistics.fmean(v), min(v))
                for label, v in buckets.items() if v]


def summarize(name: str, symbol: str,
              implied_closes: Sequence[float], implied_times: Sequence[datetime],
              under_closes: Sequence[float], under_times: Sequence[datetime],
              horizon: int = HORIZON_BARS) -> VrpSummary:
    rows = build_rows(implied_closes, implied_times, under_closes, under_times,
                      horizon)
    return VrpSummary(name=name, symbol=symbol, rows=rows,
                      independent=cycles(rows, horizon))
