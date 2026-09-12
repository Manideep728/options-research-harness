# Operations: setup, run, dashboard, self-tuning

> Part of [options-research-harness](../README.md).

## Setup

```powershell
python -m venv .venv
.venv\Scripts\pip install -r requirements-dev.txt   # runtime + test/lint tooling
copy .env.example .env   # then edit .env
```

(`requirements.txt` alone is enough to *run* the bot; `requirements-dev.txt`
adds pytest, ruff, and mypy. Both are pinned so a clone reproduces CI.)

Get free paper API keys: sign up at https://alpaca.markets, open the dashboard,
switch to Paper Trading, and generate an API key pair. Put both values in `.env`.

## Run

```powershell
.venv\Scripts\python main.py
```

- `DRY_RUN=true` (default): full pipeline with real market data; orders are
  logged, not submitted. Start here.
- `DRY_RUN=false`: orders are submitted to your paper account.

Every cycle logs the shortlist's indicator readings and the decision reason
(enter / skip / hold / exit) to the console and `bot.log`, and each re-rank
logs the new top-5 with their scores. Daily trade counts
survive restarts via `state.json`; every fill is journaled to `trades.csv`
with FIFO-matched realized P&L.

## Dashboard

The repository now includes a local web dashboard in `web/` and a small
FastAPI backend in `dashboard_api.py`.

```powershell
cd web
npm run dev
```

`npm run dev` boots the FastAPI backend automatically (reusing it if it's
already running on `:8000`) alongside the Next.js frontend, so opening the
dashboard never needs a second terminal. Opening the dashboard does not start
the trading loop; `main.py` only runs once you explicitly start it.

Equivalently, `.venv\Scripts\python run_dashboard.py` still launches the API
and frontend together from the Python side, without npm.

The dashboard reads live bot state from the API and shows the current signal
and risk reasoning for the active shortlist (the top-ranked names the engine is
actually polling, read from `active.json`, so a refresh costs a handful of data
calls instead of one per universe symbol), plus two separate sets of controls:

- **Bot process** — Start/stop the trading loop (`main.py`) itself as an OS
  process. This is the on/off switch; nothing trades while it's stopped.
- **Entry gate** — Pause/resume new entries on an already-running bot without
  touching exits, plus canceling orders and closing positions.

## Backtesting & self-tuning

```powershell
.venv\Scripts\python backtest.py --days 365          # replay current params
.venv\Scripts\python backtest.py --days 365 --tune   # guarded self-tuning
```

The backtester replays the exact live signal rules over historical bars with
an explicit option-P&L model — Black-Scholes in `bot/pricing.py`, driven by the
implied volatility, days to expiry, and round-trip cost in `SimParams`.
`--tune` grid-searches the signal
and exit parameters under hard guidelines (`bot/tuner.py`):

1. Risk caps are not in the search space at all.
2. Every candidate value is clamped into `TUNABLE_BOUNDS` — twice.
3. ≥ 30 trades required on both the train and validation windows.
4. The winner is selected on training expectancy, then checked exactly once
   against validation: it must have positive validation expectancy AND
   beat the current parameters' validation expectancy by ≥ 10%. (Ranking
   candidates by validation score — and then reporting that same score as
   the evidence — is selection bias: across ~200 grid candidates the best
   validation score would be partly luck. Selecting on train and touching
   validation only once for the winner keeps it an honest out-of-sample
   check.)

Accepted changes are written to `tuned_params.json` with their evidence; the bot
applies them (re-clamped) on next start. If nothing qualifies, the current
parameters stand.

Note that `--tune` uses a single chronological train/validation split, not
walk-forward analysis. There are no rolling windows and no re-fit sequence.

**Honest numbers.** This repo had a `tuned_params.json` that carried evidence
of +15.65% validation expectancy on 46 trades. That evidence was stale twice
over: an earlier tuner selected the winner on the validation window itself,
and the numbers came from the P&L model that the [simulator
fix](postmortem.md#the-bug) replaced.
The live bot loaded the file on start.

The file is now deleted. These are the measurements that show why:

- Re-run `backtest.py --tune` under the corrected model. The tune is
  REJECTED; the winning candidate gives only 11 validation trades.
- On the validation window, the tuned parameters give −1.75% expectancy
  per trade. The file claimed +15.65%.
- Over 120 days and 30 symbols, the defaults give +3.63% on 522 trades.
  The tuned parameters give +3.56% on 462 trades.

The tuned parameters thus give no gain over the defaults, and their recorded
evidence is false. With no file present, `apply_tuned_params` returns the
defaults from `bot/config.py`. The tuner keeps its full function: if a future
`--tune` is ACCEPTED, it writes the file again with evidence that the
corrected model produced.
