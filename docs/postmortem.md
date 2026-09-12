# Postmortem: the backtester was lying

> Part of [options-research-harness](../README.md).

## The bug

`bot/simulator.py` has no option prices. It approximates:

```
option_return = underlying_move × gearing − theta × days − roundtrip_cost
```

with `gearing = delta / premium_pct_of_spot = 0.40 / 0.005 = 80×`.

At 80×, the exits trigger on almost no movement: `take_profit_pct = 0.50` needs
a +0.625% move in the underlying, and `stop_loss_pct = 0.25` needs −0.31%. A
normal daily bar moves ±2%, i.e. 3–6× past *both* barriers at once. The exit
loop detected the crossing and then recorded the bar's full return, not the
barrier — and that return was floored at −100% on the loss side (correct for an
option premium) while the gain side was unbounded:

| next daily bar | raw model return | what the code booked |
|---|---|---|
| stock +2% | +152% | +152% — though it claimed to exit at +50% |
| stock −2% | −168% | −100% — though it claimed to exit at −25% |

Same size move, opposite sign. The win books +152%, the loss books −100%.
Symmetric price noise, asymmetric P&L: volatility alone produced expectancy,
with no forecast involved. The repo's own `research/report.txt` had been showing
the symptom all along — stop-losses averaging −44.4% against a −30% stop,
take-profits +51.8% against a +40% target — and I never read it as a bug.

Isolated by varying that one function, same coin-flip signal, same bars:

| how barrier exits are booked | daily bars | 15-min bars |
|---|---|---|
| as the code did it (overshoot, floored at −100%) | +11.1% | −3.1% |
| book the barrier level it claimed to exit at | +5.1% | −0.3% |
| overshoot with no −100% floor (symmetric) | −19.8% | −5.2% |

The floor alone was worth 31 percentage points of phantom edge.

## The test that catches it

A random signal should earn about zero, minus costs. Nothing in this repo had
ever checked. `research/null.py` checks, and it is wired in as a *threshold*,
not a report:

```powershell
.venv\Scripts\python -m research null --window daily --trades 1200
```
```
null: 60 seeds, mean trades=1163
  mean expectancy      +14.74%
  p95 threshold        +25.70%

WARNING: a coin flip EARNS +14.74% per trade here. A signal-free strategy should
pay the spread, not collect it, so the P&L model is not valid on these bars —
treat every number computed on them as unusable, not merely optimistic.
```

That +14.74% was enough to pass `robustness.daily_regime_check`, whose only
criterion was `expectancy > 0`. The repo's sole multi-regime guard could not
fail a zero-skill signal. It now requires a fold to beat its own null's 95th
percentile; `donchian` passed all six folds under the old rule and fails all six
under this one.

The random signal is deliberately built as a real `Family` and run through
`search.run_family`, the identical code path a genuine candidate takes. A null
with its own private simulator would drift away from the thing it is supposed to
be measuring, which is the class of bug it exists to catch.

## Four more defects the same audit turned up

**Un-tradeable bars.** 10,485 of 166,473 cached bars (6.30%) fell outside
09:30–16:00 ET. US listed options don't trade then, so an "exit" priced off an
08:00 ET print is a fill that could never have happened. Filtering now happens
at fetch time *and* on read, so the existing cache is corrected without waiting
for someone to remember to refetch.

**Unadjusted prices.** `adjustment` was never set on the Alpaca request, so the
daily cache contained a −95.1% single "day" in GOOGL (its 20:1 split), −94.9%
in AMZN, −90.1% in NFLX, −89.9% in NVDA. At 80× gearing a put "earns" +7,600%
on that, inside the multi-year regime check.

**Splits cut per symbol, not per calendar.** Each symbol was cut at a fraction
of *its own* bar count. Bar counts range 5,043 (COST) to 6,738 (QQQ), so the
train/validation boundary landed on 9 different dates spanning 2026-02-19 to
2026-03-05. On a universe of near-100%-beta names that is a leak: the same
market move sat in one symbol's training window and another's validation window,
and `EMBARGO_BARS` couldn't help because it only ever guarded *within* a symbol.
Now one cutoff is computed as a quantile of the pooled timeline. Nine dates
collapse to one, and the latest training bar anywhere precedes the earliest
validation bar anywhere. The embargo stays counted in *bars*, because its job is
stopping an indicator lookback straddling the cut.

**A guard that could never pass.** `max_entry_vol` is documented as per-bar
return stdev *on 15-minute bars*; `daily_regime_check` ran that same number
against daily bars, where stdev is ~√26 larger. The pending spec matched zero
daily bars in all six year folds, so the check reported FAIL for a unit mismatch
rather than an economic reason, and no spec using a volatility filter could ever
have passed it. Bar-relative params are now rescaled by √(bars per day); bar
*counts* are left alone. `entry_hours`, which has no daily equivalent at all,
now reports NOT EVALUABLE, which is distinct from failing.

I had first written both of those off as "real, but not what caused the false
edge." They were still wrong, so they're fixed.

## What it did to the headline result

The previous version of the README led with a candidate at "+35.4% validation
expectancy over 32 trades, against the live strategy's +5.2%." Re-measured after
all of the above:

| window | candidate | its own coin-flip null | verdict |
|---|---|---|---|
| **train** (what it was selected on) | −3.28% over 148 trades | mean −1.36%, p95 +12.27% | 46.7th percentile — a coin flip does better |
| **validation** | +39.91% over 24 trades | mean −0.18%, p95 +18.40% | beats the null, but 24 < the 30-trade floor |

It cannot beat a coin flip on the data it was selected on. Clustering its trades
by entry bar drops its Sharpe from +0.4166 to +0.2641 and its deflated Sharpe
from 0.6471 to 0.3226 against a 0.95 bar. It was already failing, and now it
fails by the margin the evidence actually supports.

The observation it was built on inverted. The LLM proposal's stated hypothesis
was *"low-vol entries +52.5% vs −2.2% in high vol — trade only when entry
volatility is calm."* Re-running the same failure report over corrected bars:

| entry volatility | before | after |
|---|---|---|
| low | +52.5% | −13.3% (n=50, 74% losers) |
| high | −2.2% | +3.9% (n=49) |

The calm-volatility edge was the artifact. Low-vol entries are now the worst
bucket, which is what you would expect once volatility stops being a source of
free expectancy: the bug paid out in proportion to how far a bar overshot its
barrier, so the strategy that looked best was the one selecting for the bars
where that mattered most. A spec built to exploit that observation was always
going to be a spec built to exploit the bug.

It would no longer be accepted by `search`, and the burn-once holdout gate has
still never been run. Every one of the 986 logged trials remains committed and
counted in `N`, because those attempts genuinely happened; they are marked
non-comparable by a `dataset_change` row rather than deleted. See [the registry
counts](../README.md#the-registry-counts) for how 986 relates to the 1,160
unique trials logged today.

The deflated Sharpe reads its two inputs from two different places, for this
reason. `N` counts every attempt, including the 986, because each one was a
chance to get lucky. The spread of trial Sharpes comes only from trials logged
after the most recent `dataset_change` row, because that spread describes the
measuring instrument and must come from one instrument. If fewer than two
trials follow the marker, the spread falls back to the full history: a stale
estimate is wrong, but no estimate at all sets the benchmark to zero and makes
the gate easier.
