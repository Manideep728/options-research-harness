---
name: test-strategy
description: Test a plain-English trading strategy idea through the guarded research pipeline. Use when the user says "test a strategy", "try a strategy where...", "what if we bought/sold when...", "backtest this idea", "does X work as a strategy", or describes any entry/exit rule they want evaluated.
---

# Test a Trading Strategy

Turn a plain-English strategy idea into a spec, run it through the research
pipeline (`python -m research ...`), and report the verdict honestly. The
pipeline's guardrails are the product — never route around them.

## Method

### 1. Translate the idea into a spec

The vocabulary lives in `research/blocks.py` — read `TRENDS`, `TRIGGERS`,
`FILTERS`, `REQUIRED_PARAMS`, and `PARAM_BOUNDS` from there (do not trust
memory; the vocabulary grows over time). A spec is:

```json
{
  "name": "kebab-case-name",
  "hypothesis": "one sentence: what edge this tests and why",
  "trend": "<one of TRENDS>",
  "trigger": "<one of TRIGGERS>",
  "filters": ["<zero or more of FILTERS>"],
  "grid": {"<param>": [2-3 values each]}
}
```

Rules for the translation:
- **Restate the mapping in one sentence** before running anything — "I'm
  testing this as: RSI cross up through 40/45 in an EMA uptrend, calls only"
  — so a mistranslation is visible to the user immediately.
- **Keep grids small** (2–3 values per param). Every candidate is logged to
  the trial registry and permanently raises the deflated-Sharpe bar for the
  whole research program. A big sweep is not thoroughness, it's multiplicity.
- Validate with `research.blocks.validate_spec()` before running.

### 2. If the idea doesn't fit the vocabulary

Say exactly which concept is missing (e.g. "volume filter — there is no
volume block"). Offer two options and let the user pick:

a. **Add a new block** to `research/blocks.py`: entry in `REQUIRED_PARAMS`
   and `PARAM_BOUNDS`, a branch in `spec_signal` (must stay causal —
   signals[i] may only use bars ≤ i), validation coverage, and tests in
   `tests/test_blocks.py` following the existing filter tests. Run the full
   suite after.
b. **Test the nearest expressible approximation**, clearly labeled as an
   approximation of what they asked.

Never silently approximate.

### 3. Ensure data exists

If `research/data/intraday/` is missing or empty, run
`python -m research fetch` first (needs Alpaca keys in `.env`; downloading
30 symbols takes a few minutes — tell the user).

### 4. Run the search

```powershell
# write the spec (reuse research.proposer.save_spec for naming), then:
.venv\Scripts\python -m research search --spec research/proposals/<name>.json
```

Report the verdict and numbers verbatim: train/validation trade counts,
expectancy, win rate, and the live-baseline comparison. **A rejection is the
normal, informative outcome** — most ideas have no edge, and the pipeline
saying so is it working. Report which guideline failed and what that means;
do not apologize or spin.

### 5. Follow-ups (consent-gated)

- If ACCEPTED: offer `python -m research report` (failure analysis) and
  `python -m research robustness` (simulator-perturbation + regime checks).
  Both are safe to run and repeat.
- **NEVER run `python -m research gate` unless the user explicitly asks for
  it in the current conversation.** The gate burns the one-shot out-of-sample
  holdout — pass or fail, that data can never judge again until months of
  new market data accrue. Always state this cost before the user decides.

## Honesty rails (non-negotiable)

- Always use the default registry (`research/trials.jsonl`) so every attempt
  is on the record. No scratch registries to "try things quietly".
- If a spec is rejected, do NOT nudge its parameters and re-run against the
  same validation data until something passes — that is the iterate-until-
  lucky trap. Instead run `report`, read the loss buckets, and form a
  structurally different hypothesis.
- Never lower `MIN_TRADES`, widen `PARAM_BOUNDS`, touch risk caps, or raise
  `MAX_CANDIDATES` to force an acceptance.
- An accepted-and-gated strategy still needs weeks of paper trading and a
  human decision before it goes near `tuned_params.json`.
