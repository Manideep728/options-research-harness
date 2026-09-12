# Research loop

> Part of [options-research-harness](../README.md).

A guarded self-improvement pipeline, separate from the live bot (the engine
never imports it). The cycle:

```powershell
.venv\Scripts\python -m research fetch            # cache bars once; splits off a quarantined holdout
.venv\Scripts\python -m research vrp              # does the effect exist? the pre-registered kill criterion
.venv\Scripts\python -m research search --family baseline   # select on TRAIN, judge on validation
.venv\Scripts\python -m research report           # failure analysis of train trades (you read this)
.venv\Scripts\python -m research null             # what a coin flip earns on the same bars
.venv\Scripts\python -m research replay --compare # the REAL engine over cached bars
.venv\Scripts\python -m research robustness       # perturbation + daily regime check vs the null
.venv\Scripts\python -m research gate             # burn-once out-of-sample verdict
```

What enforces the honesty:

| Guard | The failure it prevents |
|---|---|
| Coin-flip null | A guard that only asks `expectancy > 0` passes a random signal whenever the P&L model is broken. This is the one that caught everything in the [postmortem](postmortem.md). |
| Structure-matched control | A control that BUYS options while the candidate SELLS them measures the cost of buying, not the skill of the candidate. This moved a short-premium family from ACCEPTED to REJECTED. |
| Pre-registered kill criterion | The threshold for "the effect exists" is written down before the measurement runs, so it cannot be adjusted to fit the result. |
| Tail check | Expectancy cannot judge a short-volatility book: it wins small and often and loses large and rarely, so a sound book and a doomed one look identical on the average. Worst trade and maximum drawdown are separate pass/fail criteria. |
| Committed trial registry | A local-only log resets `N` to 0 on a fresh clone, and every result silently looks better than it is. |
| Dataset-keyed trial identity | A score keyed on the string `"train"` survives `fetch` moving the split boundaries, so the searcher reuses a number computed on different bars. |
| Deflated Sharpe ratio | Scores a result against *the best of N tries*, so the luckiest of N stops reading as skill. |
| Burn-once holdout | The out-of-sample window is consumed after a single gate attempt — pass or fail — so it can't be retried until it flatters. |
| Select on train, touch validation once | Ranking candidates by validation score and then reporting that score is selection bias. |
| Quarantined + embargoed holdout | `load_bars()` raises on `kind="holdout"`; exactly one function can reach that directory. |
| LLM proposes specs, never code | The proposer picks from a fixed block vocabulary with clamped params and a capped grid — it cannot smuggle in arbitrary logic. |
| No auto-deploy, ever | A gate pass prints evidence for human review. Risk caps are in no search space. |

Families: `baseline` (live EMA+RSI), `ema_slope`, `donchian`, `rsi_only`
(control), and three short-premium families — `short_put_passive` (the
benchmark), `short_put_iv_rank`, and `short_put_vrp_spread`.

Every candidate ever scored is logged to an append-only `research/trials.jsonl`,
committed to git, because that trial count is the denominator of every honest
claim the loop makes. A local-only registry would reset N to 0 on a fresh clone
and make every result look better than it is, and the burn-once gate would stop
being enforceable across machines. The acceptance score is a deflated Sharpe
(your Sharpe vs. the best of N logged tries); the holdout window is burned after
one gate attempt, pass or fail, and a repeat is refused until new data accrues.
A gate pass prints evidence for *manual* review; nothing auto-deploys, and risk
caps are not in any search space. The `search` step also skips candidates
already recorded on the same data window, so re-running a family neither repeats
work nor inflates N with duplicates.

The data cache under `research/data/` is *not* committed (large, and
re-fetchable from Alpaca); a fresh clone runs `fetch` once before searching.

A worked example (30-name universe, 365d of 15-min bars). Searching the
hand-written families produced ~841 logged trials and zero accepted winners: the
selective RSI configs that survived the train filter fired too few times to
clear the ≥30-trade validation floor, while the two families that traded often
enough were negative after costs (`donchian` over 3,143 trades; `rsi_only`, the
deliberate control, over 2,230). Note the shape of that failure. The configs
that *looked* best were the ones with almost no evidence behind them, which is
what the trade floor exists to catch.

Phase 2 appeared to change the outcome. Given the failure report, the LLM
proposer observed that uptrend entries scored far better than downtrend ones and
calm-volatility entries far better than high-volatility ones, then composed those
into `calm-uptrend-calls` (`calls_only` + `max_entry_vol`, spec in
`research/proposals/`). Its winner was the first candidate ever to clear the
search stage, at 986 unique trials.

It did not survive the audit. Re-measured with the exit accounting fixed and
un-tradeable bars removed, it loses to a coin flip on the window it was selected
on; [the postmortem](postmortem.md#what-it-did-to-the-headline-result) carries
the measured table. The specific numbers once quoted here — 32 validation trades
at +35.4% against the live strategy's +5.2% — came from the broken model and are
not reproducible.

The registry still counts those 986 attempts, marked non-comparable rather than
deleted, because that count is the honest denominator for anything claimed about
the old data. See [the registry counts](../README.md#the-registry-counts) for
how 986 relates to the 1,160 unique trials logged today.

The burn-once holdout gate has never been run, and should not be run on this
candidate. Burning the one-shot out-of-sample window on a result the null already
rejects would spend the only irreversible resource in the repo on a foregone
conclusion.

**Phase 2 — the LLM proposer.** `python -m research propose` sends the
failure report + trial history to Claude, which proposes the next family as
a JSON spec: a combination of fixed building blocks (`research/blocks.py`:
trend x trigger x entry filters such as `max_entry_vol`, `entry_hours`,
`calls_only`), never code. Specs are validated, clamped into bounds, capped
in grid size, and searched via `search --spec research/proposals/<name>.json`
through the identical pipeline with full registry logging. Requires
`ANTHROPIC_API_KEY` in `.env`; without one, `propose --offline` writes the
exact prompt to `research/proposal_prompt.txt` for any LLM (or you) to answer.
The gate stays human-invoked and burn-once regardless of who proposed.
