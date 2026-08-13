# The second attempt: sell the variance risk premium

> Part of [trading_bot_new](../README.md).

The audit in the [postmortem](postmortem.md) corrected the measurement. It did
not supply a strategy. So the
next question was whether the signal had any forecast power at all, independent
of the P&L model. Measure the direction-adjusted move of the UNDERLYING after
every signal, and no option pricing is involved:

| horizon after the signal | baseline | donchian | rsi_only |
|---|---:|---:|---:|
| 15 minutes | +0.003% | −0.005% | −0.004% |
| 1 hour | −0.020% | −0.017% | −0.007% |
| 1 day | −0.136% | −0.091% | −0.046% |
| 32 hours | −0.724% | −0.448% | −0.282% |

Zero or negative at every horizon, on 154,000 bars. Decomposing the P&L per
trade agrees: the directional term is **−0.02%**, theta is −1.77%, and the round
trip is −3.00%. No exit rule, leverage level, or holding period repairs a signal
that forecasts nothing. Tuning was therefore the wrong response.

The bot buys options. It is on the losing side of the best documented effect in
its own market: **implied volatility usually exceeds the volatility that then
occurs, and option sellers collect the difference.** The remaining work tests
that effect, with a kill criterion registered before any result existed.

### Step 1 — does the premium exist here?

No bot code was written for this step, because the previous failure was building
machinery before measuring the effect. `research/vrp.py` needs two public series
and prices nothing.

```powershell
.venv\Scripts\python -m research vrp
```
```
VIX/SPY: 2645 overlapping days (2016-01-04 .. 2026-07-13), 126 independent cycles
  mean implied          18.53
  mean VRP              +3.79   median +4.71   positive on 84.3% of days
  cost hurdle            0.56   (round trip 3% of premium)
  worst single day     -63.39   (2020-02-20: implied 15.6 vs realized 79.0)
  max drawdown          64.33   (cumulative vol points, independent cycles)
  -> CLEARS the cost hurdle
```

The premium is 6.8 times the cost of collecting it, and it survives the 6% round
trip that the live liquidity gate tolerates. It is also collected in calm
markets and lost in one event: the worst single observation is 17 times the
mean, and it starts from an implied volatility of 15.6, which is the calm third
of the sample. The tail is the whole risk.

Note the two measurements that are separate on purpose. Drawdown uses
**non-overlapping** cycles, because consecutive daily rows share 20 of their 21
forward days — one crash smeared across 21 rows flatters every drawdown, and a
short-volatility strategy is judged on exactly that.

### Step 2 — three defects that made the ruler unusable for this

A coin flip must lose money. On short premium it must instead **earn the
premium**, so the null changes meaning: a positive null is the effect, and the
candidate has to beat it. That only works if the pricing is real.

**Linear pricing.** The simulator approximated the option return from a fixed
delta and a fixed premium. `bot/pricing.py` now prices with Black-Scholes from
(spot, strike, time, volatility, rate). The real premium is 0.93% of spot, not
the 0.5% assumed, so the true gearing is **43×, not 80×**.

**Closes only.** `research/data.py` stored `close` and discarded open, high, and
low. A barrier exit was therefore inferred from a close, which is what produced
the overshoot. Bars now carry every column, and the adverse extreme is tested
first when one bar touches both barriers.

**No implied volatility.** Every option was priced at a flat 20%. VIX, VXN, and
RVX supply a real daily series for SPY, QQQ, and IWM.

Each input alone left the coin flip profitable. Both were required:

| window | closes only | full bars + real IV |
|---|---:|---:|
| SPY/QQQ/IWM daily | −9.83% | **−12.61%** |
| 30 symbols, daily | **+12.55%** | **−5.96%** |
| 30 symbols, intraday | **+1.86%** | **−1.99%** |

**Short positions also needed a denominator.** A seller's maximum loss is not
the premium. The model therefore trades **defined-risk credit spreads** only —
short one strike, long a further one — so the loss is bounded and known, and
every return is a fraction of capital at risk. Naked short options cannot be
represented. `research/robustness.py` gained a tail check on worst trade and
maximum drawdown, because expectancy cannot judge a short-volatility book.

### Step 3 — the verdict

Three entry rules were tested against the same control, on the same bars.

| rule | validation expectancy | percentile of its own null | verdict |
|---|---:|---:|---|
| sell on every bar (passive) | +1.54% | 81.0 | **REJECTED** |
| sell when IV rank is high | −1.01% | 7.0 | **REJECTED** |
| sell when IV exceeds trailing realized vol | +1.19% | 69.5 | **REJECTED** |

**The control is positive, and that is the finding.** A coin flip that sells
premium with random timing earns **+0.70% to +0.82% per trade**. The premium is
real, it survives costs, and passive collection captures it — but no timing rule
tested here is distinguishable from random timing. That is what theory predicts
for a risk premium rather than a mispricing: the seller is paid for holding a
risk, not for knowing something.

The first result of the three was **ACCEPTED** until the control was corrected.
The coin flip was still BUYING options, which loses 9.81% per trade, so any
seller cleared that bar without an edge. A control must trade the same structure
as the candidate; `null.null_family` now takes the action pair as an argument.
That change alone moved the passive family from ACCEPTED to REJECTED.

The IV-rank rule inverted, and the reason is instructive. It sells when implied
volatility is at the top of its own range, which is when realized volatility has
already overtaken it — it sells into a fall in progress. It was built on the
regime table above, where the stressed third pays +5.06 against the calm third's
+2.56. That table rests on one crash. It did not survive contact with a trading
rule.

The third rule was written to invert that error: compare implied against the
volatility the underlying is actually delivering, so the same crash that
maximizes IV rank makes this rule stand down. The search was free to demand a
rich premium and **selected the lowest threshold in the grid** (`vrp_min = 0.0`),
which is the setting closest to no filter at all. Every filter subtracted.

`research/candidate.json` held the wrongly-accepted passive candidate, stamped
with the old control's numbers. Running `gate` would have spent the burn-once
holdout on a candidate that is in fact rejected. The file was deleted, and the
holdout is still sealed.

