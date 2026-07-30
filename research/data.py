"""Historical bar cache + dataset splits for the research loop.

Why a cache: the loop reruns backtests constantly, and backtest.py's
fetch-on-every-run pattern would hammer Alpaca and make every iteration
minutes slow. `python -m research fetch` downloads once; everything
downstream reads CSV from disk.

Why the holdout quarantine: the final out-of-sample gate is only honest if
the search process never saw that data. The most recent slice of history —
cut at the same calendar moment across BOTH the 15-min and daily datasets —
is written to data/holdout/, and only research/gate.py ever reads it.

Two data-hygiene rules that are not optional, both of which were violated:

1. Intraday bars are filtered to the regular session (09:30-16:00 ET). Alpaca
   returns pre- and post-market bars, and US listed OPTIONS do not trade in
   those sessions — so an "exit" priced off an 08:00 ET bar is a fill that
   could never have happened. 6.3% of the cached bars were outside the
   session, and they carried three of the four trades that made up 89.5% of
   the pending candidate's validation edge.

2. Bars are split/dividend adjusted. Unadjusted history put a -95.1% single
   "day" in GOOGL (its 20:1 split), -94.9% in AMZN, -90.1% in NFLX and -89.9%
   in NVDA. At the simulator's 80x gearing a put "earns" +7,600% on that.

Note that the session filter applies to INTRADAY ONLY. Alpaca stamps daily
bars at 00:00 ET (04:00/05:00 UTC), so filtering them by time-of-day would
delete the entire daily cache.
"""

import csv
import logging
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from typing import cast
from zoneinfo import ZoneInfo

from alpaca.data.enums import Adjustment, DataFeed
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.models import BarSet
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

from bot.config import Settings

log = logging.getLogger("research.data")

DATA_DIR = Path(__file__).resolve().parent / "data"

# Regular US equity session, in exchange-local time so DST is handled for us.
MARKET_TZ = ZoneInfo("America/New_York")
SESSION_OPEN = time(9, 30)
SESSION_CLOSE = time(16, 0)

RESEARCH_FRACTION = 0.80  # train+validation share; the rest is holdout
TRAIN_FRACTION = 0.75     # train share of the research slice (=60% overall)
# Bars dropped between adjacent segments. The slowest indicator needs
# ~31 bars of lookback (ema_slow bound is 30), so 60 leaves no way for a
# window computed at the start of one segment to overlap the previous one.
EMBARGO_BARS = 60

Bars = tuple[list[float], list[datetime]]


# --- session filtering (pure, fully unit-testable) ---

def in_session(ts: datetime) -> bool:
    """True when a bar STARTS inside the regular session. Bar timestamps are
    start-of-bar, so a 15-min bar at 15:45 ET is the session's last bar and a
    bar at 16:00 ET is already after-hours."""
    local = ts.astimezone(MARKET_TZ).time()
    return SESSION_OPEN <= local < SESSION_CLOSE


def filter_to_session(closes: list[float], times: list[datetime]) -> Bars:
    """Drop pre- and post-market bars. Intraday only — see the module
    docstring for why this must never be applied to daily bars."""
    kept = [(c, t) for c, t in zip(closes, times, strict=True) if in_session(t)]
    if not kept:
        return [], []
    kept_closes, kept_times = zip(*kept, strict=True)
    return list(kept_closes), list(kept_times)


# --- pure split logic (no I/O, fully unit-testable) ---

def split_history(closes: list[float], times: list[datetime],
                  research_fraction: float = RESEARCH_FRACTION,
                  embargo: int = EMBARGO_BARS) -> tuple[Bars, Bars]:
    """(research, holdout): chronological cut with `embargo` bars dropped
    between the segments so an indicator lookback can't straddle the cut."""
    cut = int(len(closes) * research_fraction)
    research = (closes[:cut], times[:cut])
    holdout = (closes[cut + embargo:], times[cut + embargo:])
    return research, holdout


def split_train_val(closes: list[float], times: list[datetime],
                    train_fraction: float = TRAIN_FRACTION,
                    embargo: int = EMBARGO_BARS) -> tuple[Bars, Bars]:
    """(train, validation) split of the research slice, same embargo rule."""
    cut = int(len(closes) * train_fraction)
    return (closes[:cut], times[:cut]), (closes[cut + embargo:], times[cut + embargo:])


def bars_before(closes: list[float], times: list[datetime],
                cutoff: datetime) -> Bars:
    """Bars strictly before `cutoff` — used to quarantine the holdout
    calendar window in the daily dataset too, so a daily-bar robustness
    check can't peek at the period the gate will judge on."""
    kept = [(c, t) for c, t in zip(closes, times, strict=True) if t < cutoff]
    if not kept:
        return [], []
    kept_closes, kept_times = zip(*kept, strict=True)
    return list(kept_closes), list(kept_times)


# --- CSV persistence ---

def save_bars(path: Path, closes: list[float], times: list[datetime]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["timestamp", "close"])
        for t, c in zip(times, closes, strict=True):
            writer.writerow([t.isoformat(), f"{c:.6f}"])


def _read_bars(path: Path, session_only: bool = False) -> Bars:
    if not path.exists():
        return [], []
    closes: list[float] = []
    times: list[datetime] = []
    with path.open(newline="") as f:
        for row in csv.DictReader(f):
            ts = datetime.fromisoformat(row["timestamp"])
            if session_only and not in_session(ts):
                continue
            times.append(ts)
            closes.append(float(row["close"]))
    return closes, times


def load_bars(kind: str, symbol: str, data_dir: Path = DATA_DIR) -> Bars:
    """kind: 'intraday' | 'daily'. Missing file -> ([], []) so a symbol
    that failed to fetch degrades to 'no data', not a crash.

    Intraday bars are session-filtered HERE as well as at fetch time, so a
    cache written before that rule existed is corrected on read rather than
    silently feeding un-tradeable bars into every backtest."""
    if kind not in ("intraday", "daily"):
        raise ValueError(f"unknown dataset kind: {kind!r}")
    return _read_bars(Path(data_dir) / kind / f"{symbol}.csv",
                      session_only=(kind == "intraday"))


def load_holdout_bars(symbol: str, data_dir: Path = DATA_DIR) -> Bars:
    """ONLY research/gate.py may call this. Search/report code loads via
    load_bars(), which cannot reach the holdout directory. The holdout is
    intraday, so it is session-filtered like the rest."""
    return _read_bars(Path(data_dir) / "holdout" / f"{symbol}.csv", session_only=True)


# --- fetching (the only network code in the research package) ---

def fetch_all(cfg: Settings, intraday_days: int = 365, daily_years: int = 5,
              data_dir: Path = DATA_DIR) -> None:
    """Download intraday + daily history for every universe symbol, split
    off the holdout slice, and write the CSV cache."""
    client = StockHistoricalDataClient(cfg.api_key, cfg.secret_key)
    for symbol in cfg.symbols:
        # Session-filter BEFORE splitting, so the train/val/holdout fractions
        # are computed over tradeable bars only.
        intraday = filter_to_session(*_fetch(
            client, symbol,
            TimeFrame(cfg.bar_timeframe_minutes, TimeFrameUnit.Minute),
            days=intraday_days,
        ))
        if not intraday[0]:
            log.warning("%s: no intraday bars returned; skipping symbol", symbol)
            continue
        research, holdout = split_history(*intraday)
        save_bars(Path(data_dir) / "intraday" / f"{symbol}.csv", *research)
        save_bars(Path(data_dir) / "holdout" / f"{symbol}.csv", *holdout)

        daily = _fetch(client, symbol, TimeFrame.Day, days=daily_years * 365)
        if holdout[1]:  # quarantine the same calendar window in the dailies
            daily = bars_before(*daily, cutoff=holdout[1][0])
        save_bars(Path(data_dir) / "daily" / f"{symbol}.csv", *daily)

        log.info("%s: cached %d intraday research bars, %d holdout, %d daily",
                 symbol, len(research[0]), len(holdout[0]), len(daily[0]))


def _fetch(client: StockHistoricalDataClient, symbol: str,
           timeframe: TimeFrame, days: int) -> Bars:
    start = datetime.now(UTC) - timedelta(days=days)
    bars = cast(BarSet, client.get_stock_bars(
        StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=timeframe,
            start=start,
            feed=DataFeed.IEX,
            # Without this, a 20:1 split reads as a -95% single-bar "move".
            adjustment=Adjustment.ALL,
        )
    )).data.get(symbol, [])
    return [float(b.close) for b in bars], [b.timestamp for b in bars]
