# Options Paper-Trading Bot

Automatic options trading bot for an **Alpaca paper account**. On a 60-second
loop during market hours it pulls 5-minute bars, computes EMA/RSI, and — when
the strict rules line up — buys a single call or put, then manages the exit.
Long options only: maximum possible loss on any trade is the premium paid.

**Paper trading only.** The broker client is hard-wired to Alpaca's paper
endpoint. Nothing here is financial advice; the strategy is a disciplined
skeleton, not a profit claim.

## Strategy (all thresholds in `bot/config.py`)

**Entry** — evaluated per symbol (`SPY, QQQ, AAPL, MSFT, NVDA`) on the latest
closed bar:

| Condition | Buy CALL | Buy PUT |
|---|---|---|
| Trend filter | EMA9 > EMA21 | EMA9 < EMA21 |
| Trigger | RSI(14) crosses **up** through 35 | RSI(14) crosses **down** through 65 |

Contract picked: nearest expiry within **7–14 DTE**, first strike OTM, and it
must pass liquidity gates (bid > 0, spread ≤ 10% of mid, open interest ≥ 100).

**Exits** (checked every loop): take profit **+50%**, stop loss **−25%**,
time stop at **≤ 2 DTE**.

**Risk gates** (all must pass): max 3 open positions, 1 per underlying,
premium ≤ 2% of equity, max 3 new trades/day, and a **daily circuit breaker**
that halts new entries if the account is down 4% on the day. No entries in the
first/last 15 minutes of the session.

## Setup

```powershell
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
copy .env.example .env   # then edit .env
```

Get free **paper** API keys: sign up at https://alpaca.markets, open the
dashboard, switch to **Paper Trading**, and generate an API key pair. Put both
values in `.env`. (Use paper keys only — the bot refuses nothing else, but the
client itself always targets `paper-api.alpaca.markets`.)

## Run

```powershell
.venv\Scripts\python main.py
```

- `DRY_RUN=true` (default): full pipeline with real market data; orders are
  **logged, not submitted**. Start here.
- `DRY_RUN=false`: orders are submitted to your **paper** account.

Every cycle logs each symbol's indicator readings and the decision reason
(enter / skip / hold / exit) to the console and `bot.log`. Daily trade counts
survive restarts via `state.json`.

## Tests

```powershell
.venv\Scripts\python -m pytest tests -q
```

48 tests cover the indicator math (including a known Wilder RSI value), signal
triggers, contract filters, every risk gate, and full engine cycles against a
fake broker (entry, exit, holds, limits, closed market).

## Layout

```
main.py            entry point (logging, config validation, loop start)
bot/config.py      every tunable: symbols, thresholds, limits
bot/indicators.py  EMA / Wilder RSI (pure math)
bot/strategy.py    signal logic (pure)
bot/options.py     contract selection + liquidity gates (pure)
bot/risk.py        entry gates, sizing, exit rules (pure)
bot/state.py       daily counters persisted to state.json
bot/broker.py      the ONLY module that talks to Alpaca; DRY_RUN lives here
bot/engine.py      the loop: clock -> exits -> entries -> sleep
```
