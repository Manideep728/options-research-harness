"""Historical bar cache + dataset splits for the research loop.

Why a cache: the loop reruns backtests constantly, and backtest.py's
fetch-on-every-run pattern would hammer Alpaca and make every iteration
minutes slow. `python -m research fetch` downloads once; everything
downstream reads CSV from disk.

Why the holdout quarantine: the final out-of-sample gate is only honest if
the search process never saw that data. The most recent slice of history —
cut at the same calendar moment across BOTH the 15-min and daily datasets —
is written to data/holdout/, and only research/gate.py ever reads it.
"""

import csv
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

from alpaca.data.enums import DataFeed
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.models import BarSet
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

from bot.config import Settings

log = logging.getLogger("research.data")

DATA_DIR = Path(__file__).resolve().parent / "data"

RESEARCH_FRACTION = 0.80  # train+validation share; the rest is holdout
TRAIN_FRACTION = 0.75     # train share of the research slice (=60% overall)
# Bars dropped between adjacent segments. The slowest indicator needs
# ~31 bars of lookback (ema_slow bound is 30), so 60 leaves no way for a
# window computed at the start of one segment to overlap the previous one.
EMBARGO_BARS = 60

Bars = tuple[list[float], list[datetime]]


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


def _read_bars(path: Path) -> Bars:
    if not path.exists():
        return [], []
    closes: list[float] = []
    times: list[datetime] = []
    with path.open(newline="") as f:
        for row in csv.DictReader(f):
            times.append(datetime.fromisoformat(row["timestamp"]))
            closes.append(float(row["close"]))
    return closes, times


def load_bars(kind: str, symbol: str, data_dir: Path = DATA_DIR) -> Bars:
    """kind: 'intraday' | 'daily'. Missing file -> ([], []) so a symbol
    that failed to fetch degrades to 'no data', not a crash."""
    if kind not in ("intraday", "daily"):
        raise ValueError(f"unknown dataset kind: {kind!r}")
    return _read_bars(Path(data_dir) / kind / f"{symbol}.csv")


def load_holdout_bars(symbol: str, data_dir: Path = DATA_DIR) -> Bars:
    """ONLY research/gate.py may call this. Search/report code loads via
    load_bars(), which cannot reach the holdout directory."""
    return _read_bars(Path(data_dir) / "holdout" / f"{symbol}.csv")


# --- fetching (the only network code in the research package) ---

def fetch_all(cfg: Settings, intraday_days: int = 365, daily_years: int = 5,
              data_dir: Path = DATA_DIR) -> None:
    """Download intraday + daily history for every universe symbol, split
    off the holdout slice, and write the CSV cache."""
    client = StockHistoricalDataClient(cfg.api_key, cfg.secret_key)
    for symbol in cfg.symbols:
        intraday = _fetch(client, symbol,
                          TimeFrame(cfg.bar_timeframe_minutes, TimeFrameUnit.Minute),
                          days=intraday_days)
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
        )
    )).data.get(symbol, [])
    return [float(b.close) for b in bars], [b.timestamp for b in bars]
