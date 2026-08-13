# The Backtester Was Lying, and I Built the Test That Caught It

[![CI](https://github.com/Manideep728/trading_bot_new/actions/workflows/ci.yml/badge.svg)](https://github.com/Manideep728/trading_bot_new/actions/workflows/ci.yml)

An options trading bot on an **Alpaca paper account**, plus the research harness
that evaluates it. The interesting part is not the bot. It is that the harness
searched 986 strategy configurations, reported a plausible number for every one
of them, and every one of those numbers was wrong — because of four lines in the
simulator's exit loop.

## The two findings

**1. The backtest manufactured its own edge.** `bot/simulator.py` detected a
barrier crossing and then booked the full return of the bar rather than the
barrier. Losses were floored at −100%; gains were not. Symmetric price noise
therefore produced expectancy with no forecast involved. The floor alone was
worth **31 percentage points** of phantom edge. Nothing in the repo had ever
checked whether a random signal earns zero, so `research/null.py` now does, and
it is wired in as a threshold rather than a report.

→ [The full postmortem](docs/postmortem.md) — the bug, the test that catches it,
four more defects the same audit turned up, and what it did to the headline
result.

**2. The premium is real; no timing rule beats collecting it.** With the ruler
corrected, one pre-registered thesis was tested: sell the variance risk premium.
`python -m research vrp` reports **+3.79 volatility points** on VIX/SPY against a
0.56 cost hurdle, positive on 84.3% of days. Three entry rules were then measured
against a structure-matched control, and all three were **REJECTED**. The control
itself earns +0.70% to +0.82% per trade. That is the finding: the premium is
payment for holding a risk, not a mispricing anyone times.

→ [The variance risk premium arc](docs/variance-risk-premium.md) — the kill
criterion, the three defects that made the ruler unusable for this, and the
verdict.

**Paper trading only.** The broker client is hard-wired to Alpaca's paper
endpoint. Nothing here is financial advice, and the live strategy has **no
demonstrated edge** — four completed round trips on the paper account, all four
losers, −$600 realized. The risk caps exist to contain the damage while evidence
accumulates. The burn-once holdout gate has never been run.

## The registry counts

The repo's pitch is numerical rigor, so its own headline count is stated once,
here, and every other page links to this section instead of restating it.
`research/trials.jsonl` is append-only and committed, because the number of
things you tried is the denominator of every honest claim.

| figure | value | what it means |
|---|---:|---|
| Unique trials when the bug was found | **986** | 987 rows, all before the first `dataset_change` marker. This is the number the postmortem is about. |
| Unique trials today | **1,160** | Across 1,164 rows: 1,162 trials and 2 `dataset_change` markers. |
| Unique trials on the current model | **174** | Logged after the most recent marker. Only these estimate the deflated Sharpe's variance. |

Nothing is ever deleted. Trials measured on a P&L model that was later replaced
stay counted in `N`, because those attempts were real chances to get lucky; a
`dataset_change` row marks them as non-comparable. Reproduce the table with:

```powershell
.venv\Scripts\python -c "from research import registry; print(registry.trial_count())"
```

## Quickstart

```powershell
python -m venv .venv
.venv\Scripts\pip install -r requirements-dev.txt   # runtime + test/lint tooling
copy .env.example .env                              # then add your paper keys
.venv\Scripts\python main.py                        # DRY_RUN=true by default
```

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
cp .env.example .env
.venv/bin/python main.py
```

Get free **paper** keys at https://alpaca.markets → Paper Trading → generate an
API key pair. `DRY_RUN=true` runs the full pipeline against real market data and
logs orders without submitting them; start there.

→ [Operations](docs/operations.md) — setup detail, the dashboard, and the
guarded self-tuner.

## Architecture

```mermaid
flowchart LR
    subgraph loop["Trading loop (main.py)"]
        engine["engine.py<br/>clock → reconcile → exits → entries"]
        scanner["scanner.py<br/>rank universe, keep top 5"]
        strategy["strategy.py<br/>EMA trend + RSI cross"]
        risk["risk.py<br/>entry gates · sizing · exit rules"]
        options["options.py<br/>contract selection + liquidity gates"]
        engine --> scanner
        engine --> strategy
        engine --> risk
        engine --> options
    end

    broker["broker.py<br/>the ONLY Alpaca client"]
    alpaca[("Alpaca paper API")]
    engine --> broker --> alpaca

    subgraph state["Shared state (files)"]
        journal[("trades.csv")]
        day[("state.json")]
        active[("active.json")]
        control[("control.json")]
        tuned[("tuned_params.json<br/>optional; absent by default")]
    end
    engine --> journal
    engine --> day
    engine --> active
    control --> engine
    tuned --> engine

    subgraph dash["Dashboard"]
        api["dashboard_api.py<br/>FastAPI"] --> web["web/<br/>Next.js UI"]
    end
    api --> active
    api --> control
    api -. "start / stop main.py" .-> engine

    subgraph research["Research loop (offline — never imported by the bot)"]
        vrpmod["vrp.py<br/>does the effect exist? (kill criterion)"]
        search["search.py<br/>train-only grid search"]
        nullmod["null.py<br/>coin-flip control"]
        replaymod["replay.py<br/>ReplayBroker"]
        gate["gate.py<br/>burn-once holdout verdict"]
        proposer["proposer.py<br/>LLM strategy specs (JSON, never code)"]
        proposer --> search --> gate
        nullmod --> search
        vrpmod -. "licenses the attempt" .-> search
    end
    replaymod -. "same 11-method interface as broker.py" .-> engine
    gate -. "evidence for manual review" .-> tuned
```

The decision worth pointing at: `bot/engine.py` depends only on a duck-typed
broker interface, 11 methods wide. So `research/replay.py` implements those same
11 methods against the CSV bar cache and drives **the real** `Engine.run_cycle()`
— with the real risk gates, scanner, and contract selection running inside it —
with zero edits to the engine.

→ [Architecture](docs/architecture.md) — the broker interface, the strategy
rules, and the file-by-file layout.

## Tests

```powershell
.venv\Scripts\python -m pytest tests -q   # 380 tests
.venv\Scripts\python -m ruff check .      # lint
.venv\Scripts\python -m mypy              # type check
cd web; npm run lint; npm run build       # frontend
```

CI runs all five on every push and pull request. The suite is near 1:1 with
modules, and the tests that pin the findings above are worth reading first —
`test_regime_guard_rejects_a_coin_flip` is the regression test for the whole
audit.

→ [Tests and quality gates](docs/tests.md) — the eleven tests that pin the
findings, and what the rest cover.

## How this was built

This repo was written with AI assistance. What is mine is the direction, the
review, and the decision to distrust the output — which is the part the repo
actually demonstrates.

`research/proposer.py` is that posture in running code. An LLM may propose
strategies only as **JSON specs drawn from a fixed block vocabulary with clamped
parameters — never as code**. Every proposal is validated against
`research/blocks.py`, logged to the committed registry, and counted in `N`, so a
model that proposes a hundred ideas raises the statistical bar its own winner
must clear. The guardrails exist because the generator is not trusted, including
when the generator is writing this system.

The same discipline caught the failure this README leads with: the tooling
produced 986 plausible results, and the control that was missing is what proved
them wrong.

## Documentation

| page | what is in it |
|---|---|
| [Postmortem](docs/postmortem.md) | The simulator bug, the coin-flip test, four more defects, and the headline result being withdrawn. |
| [Variance risk premium](docs/variance-risk-premium.md) | The pre-registered kill criterion, and why all three timing rules were rejected. |
| [Architecture](docs/architecture.md) | Broker interface, replay, strategy rules, file layout. |
| [Research loop](docs/research-loop.md) | The `fetch → search → null → replay → robustness → gate` cycle and its guardrails. |
| [Operations](docs/operations.md) | Setup, running the bot, the dashboard, the guarded self-tuner. |
| [Tests](docs/tests.md) | What the 380 tests cover, and which ones pin the findings. |

## License

MIT — see [LICENSE](LICENSE).
