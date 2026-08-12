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
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from typing import cast
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from alpaca.data.enums import Adjustment, DataFeed
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.models import BarSet
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

from bot.config import Settings

log = logging.getLogger("research.data")

DATA_DIR = Path(__file__).resolve().parent / "data"

# CBOE serves the index CSVs only to a browser-shaped request.
USER_AGENT = "Mozilla/5.0 (compatible; trading-bot-research/1.0)"

# One-bar move beyond which a daily price series is almost certainly carrying
# an unadjusted split rather than a real move. See suspicious_moves().
SPLIT_MOVE_THRESHOLD = 0.35

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


@dataclass(frozen=True)
class OhlcBars:
    """Full bars, for the two things closes alone cannot answer: where price
    went WITHIN a bar, and how wide the bar was.

    `has_intrabar` is False when the row came from a close-only cache, where
    the high and low are copies of the close. Callers that resolve barrier
    order or measure range must check it. Without the flag a stale cache would
    silently degrade intra-bar logic back to close-only guessing, which is the
    exact defect this field exists to expose.
    """

    times: list[datetime]
    opens: list[float]
    highs: list[float]
    lows: list[float]
    closes: list[float]
    volumes: list[float]
    has_intrabar: bool

    def __len__(self) -> int:
        return len(self.closes)

    @property
    def bars(self) -> Bars:
        """The (closes, times) pair every existing caller expects."""
        return self.closes, self.times

    def take(self, indices: list[int]) -> "OhlcBars":
        """Keep `indices`, in order, across every column at once. Splitting and
        filtering pick rows, so they select indices and apply them here rather
        than each re-zipping a different subset of the columns."""
        return OhlcBars(
            times=[self.times[i] for i in indices],
            opens=[self.opens[i] for i in indices],
            highs=[self.highs[i] for i in indices],
            lows=[self.lows[i] for i in indices],
            closes=[self.closes[i] for i in indices],
            volumes=[self.volumes[i] for i in indices],
            has_intrabar=self.has_intrabar,
        )


def ohlc_from_closes(closes: list[float], times: list[datetime]) -> OhlcBars:
    """Wrap a close-only series, marked as carrying no intra-bar detail."""
    return OhlcBars(times=list(times), opens=list(closes), highs=list(closes),
                    lows=list(closes), closes=list(closes),
                    volumes=[0.0] * len(closes), has_intrabar=False)


# --- session filtering (pure, fully unit-testable) ---

def in_session(ts: datetime) -> bool:
    """True when a bar STARTS inside the regular session. Bar timestamps are
    start-of-bar, so a 15-min bar at 15:45 ET is the session's last bar and a
    bar at 16:00 ET is already after-hours."""
    local = ts.astimezone(MARKET_TZ).time()
    return SESSION_OPEN <= local < SESSION_CLOSE


def session_indices(times: list[datetime]) -> list[int]:
    """Positions of the bars inside the regular session."""
    return [i for i, ts in enumerate(times) if in_session(ts)]


def filter_to_session(closes: list[float], times: list[datetime]) -> Bars:
    """Drop pre- and post-market bars. Intraday only — see the module
    docstring for why this must never be applied to daily bars."""
    keep = session_indices(times)
    return [closes[i] for i in keep], [times[i] for i in keep]


# --- pure split logic (no I/O, fully unit-testable) ---

def union_cutoff(times_by_symbol: dict[str, list[datetime]],
                 fraction: float) -> datetime | None:
    """The single timestamp with `fraction` of ALL symbols' bars pooled before it.

    Splits used to cut each symbol at a fraction of ITS OWN bar count. Bar counts
    range from 5,043 (COST) to 6,738 (QQQ), so the train/validation cut landed on
    9 different dates spanning 2026-02-19 to 2026-03-05, and holdout starts on 8
    dates. For 30 near-100%-beta names that means the same market move sat in one
    symbol's training window and another's validation window — the embargo could
    not help, because it only ever guarded WITHIN a symbol.
    """
    pooled = sorted(t for times in times_by_symbol.values() for t in times)
    if not pooled:
        return None
    return pooled[min(len(pooled) - 1, max(0, int(len(pooled) * fraction)))]


def split_at(closes: list[float], times: list[datetime], cutoff: datetime,
             embargo: int = EMBARGO_BARS) -> tuple[Bars, Bars]:
    """(before, after) at a SHARED calendar cutoff.

    The cutoff is calendar-shared so one market moment lands on the same side of
    the split for every symbol. The embargo stays counted in BARS, per symbol,
    because its job is to stop an indicator lookback straddling the cut and the
    slowest indicator needs ~31 bars whatever wall-clock span that covers.
    """
    before, after = split_indices(times, cutoff, embargo)
    return (([closes[i] for i in before], [times[i] for i in before]),
            ([closes[i] for i in after], [times[i] for i in after]))


def split_indices(times: list[datetime], cutoff: datetime,
                  embargo: int = EMBARGO_BARS) -> tuple[list[int], list[int]]:
    """Positions before and after `cutoff`, with the embargo already removed
    from the second half. Splitting selects rows, so the choice is made once
    here and applied to whichever columns the caller holds."""
    before = [i for i, ts in enumerate(times) if ts < cutoff]
    after = [i for i, ts in enumerate(times) if ts >= cutoff]
    return before, after[embargo:]


def split_all(bars_by_symbol: dict[str, Bars],
              fraction: float, embargo: int = EMBARGO_BARS
              ) -> tuple[dict[str, Bars], dict[str, Bars]]:
    """Apply one shared cutoff across every symbol."""
    cutoff = union_cutoff({s: times for s, (_, times) in bars_by_symbol.items()},
                          fraction)
    if cutoff is None:
        return {}, {}
    before: dict[str, Bars] = {}
    after: dict[str, Bars] = {}
    for symbol, (closes, times) in bars_by_symbol.items():
        before[symbol], after[symbol] = split_at(closes, times, cutoff, embargo)
    return before, after


def filter_ohlc_to_session(bars: OhlcBars) -> OhlcBars:
    """Session filter across every column. Intraday only, for the reason in the
    module docstring."""
    return bars.take(session_indices(bars.times))


def ohlc_before(bars: OhlcBars, cutoff: datetime) -> OhlcBars:
    return bars.take([i for i, ts in enumerate(bars.times) if ts < cutoff])


def ohlc_before_date(bars: OhlcBars, cutoff: datetime) -> OhlcBars:
    """Keep bars from calendar days strictly before `cutoff`'s day.

    Quarantining the dailies by TIMESTAMP let one holdout day through. Alpaca
    stamps a daily bar at 00:00 ET of the session it summarizes, and the
    holdout starts partway through a trading day, so the daily bar for that day
    compared as earlier than the cutoff while containing the whole session's
    price action — including the part inside the holdout.
    """
    day = cutoff.astimezone(MARKET_TZ).date()
    return bars.take([i for i, ts in enumerate(bars.times)
                      if ts.astimezone(MARKET_TZ).date() < day])


def split_all_ohlc(bars_by_symbol: dict[str, OhlcBars], fraction: float,
                   embargo: int = EMBARGO_BARS
                   ) -> tuple[dict[str, OhlcBars], dict[str, OhlcBars]]:
    """split_all, keeping every column. One shared cutoff, as ever."""
    cutoff = union_cutoff({s: b.times for s, b in bars_by_symbol.items()}, fraction)
    if cutoff is None:
        return {}, {}
    before: dict[str, OhlcBars] = {}
    after: dict[str, OhlcBars] = {}
    for symbol, bars in bars_by_symbol.items():
        i_before, i_after = split_indices(bars.times, cutoff, embargo)
        before[symbol], after[symbol] = bars.take(i_before), bars.take(i_after)
    return before, after


def implied_vol_series(symbol: str, times: list[datetime],
                       data_dir: Path = DATA_DIR) -> list[float] | None:
    """Implied volatility for `symbol` on each of `times`, as a fraction.

    None when the symbol has no volatility index of its own. That is not a
    detail to paper over: pricing an option at a volatility the market never
    quoted is what made a coin flip earn 10% per trade, so a caller with no
    real series must know it is falling back to a constant.

    The index is a daily close, so a value is carried forward to every bar of
    the same day and to days the index did not publish. Carrying FORWARD only —
    an intraday bar is priced with the volatility already known at the previous
    close, never with one from later.
    """
    index = next((name for name, sym in VOL_INDEX_UNDERLYING.items()
                  if sym == symbol), None)
    if index is None:
        return None
    closes, index_times = load_vol_index(index, data_dir)
    if not closes:
        return None
    by_day = {t.astimezone(MARKET_TZ).date(): c / 100.0
              for t, c in zip(index_times, closes, strict=True)}
    ordered = sorted(by_day)
    out: list[float] = []
    last: float | None = None
    cursor = 0
    for stamp in times:
        day = stamp.astimezone(MARKET_TZ).date()
        while cursor < len(ordered) and ordered[cursor] <= day:
            last = by_day[ordered[cursor]]
            cursor += 1
        if last is None:
            return None          # the series starts after these bars do
        out.append(last)
    return out


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


OHLC_COLUMNS = ("timestamp", "open", "high", "low", "close", "volume")


def save_ohlc(path: Path, bars: OhlcBars) -> None:
    """Write full bars. The close-only writer stays for series that genuinely
    have no other columns, such as the CBOE volatility indices."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(OHLC_COLUMNS)
        for i in range(len(bars)):
            writer.writerow([
                bars.times[i].isoformat(),
                f"{bars.opens[i]:.6f}", f"{bars.highs[i]:.6f}",
                f"{bars.lows[i]:.6f}", f"{bars.closes[i]:.6f}",
                f"{bars.volumes[i]:.2f}",
            ])


def _read_ohlc(path: Path, session_only: bool = False) -> OhlcBars:
    """Read either schema. A file written before the open/high/low/volume
    columns existed still loads, with high and low set to the close and
    has_intrabar False — so the 9 MB cache on disk keeps working and no caller
    can mistake a copied close for a real intra-bar range.
    """
    empty = OhlcBars([], [], [], [], [], [], has_intrabar=False)
    if not path.exists():
        return empty
    times: list[datetime] = []
    cols: dict[str, list[float]] = {c: [] for c in OHLC_COLUMNS[1:]}
    full = True
    with path.open(newline="") as f:
        for row in csv.DictReader(f):
            ts = datetime.fromisoformat(row["timestamp"])
            if session_only and not in_session(ts):
                continue
            close = float(row["close"])
            times.append(ts)
            for column in OHLC_COLUMNS[1:]:
                raw = row.get(column)
                if raw is None or raw == "":
                    full = False
                    cols[column].append(0.0 if column == "volume" else close)
                else:
                    cols[column].append(float(raw))
    return OhlcBars(times=times, opens=cols["open"], highs=cols["high"],
                    lows=cols["low"], closes=cols["close"],
                    volumes=cols["volume"],
                    has_intrabar=full and bool(times))


def _read_bars(path: Path, session_only: bool = False) -> Bars:
    return _read_ohlc(path, session_only).bars


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


def load_vol_index(name: str, data_dir: Path = DATA_DIR) -> Bars:
    """Cached CBOE volatility index history. Its own directory and loader so
    load_bars' kind guard — the thing that keeps the holdout unreachable —
    stays untouched."""
    return _read_bars(Path(data_dir) / "volidx" / f"{name}.csv")


def suspicious_moves(closes: list[float], times: list[datetime],
                     threshold: float = SPLIT_MOVE_THRESHOLD
                     ) -> list[tuple[datetime, float]]:
    """Single-bar moves too large to be a real price change.

    A 20:1 split reads as -95% in one bar when the request forgot
    `Adjustment.ALL`. The fetch sets it now, but a cache written before that
    keeps the artifact forever: unlike the session filter, a split cannot be
    repaired on read, because the adjustment factor is not in the file. The
    only fix is to fetch again, so the least this can do is refuse to stay
    quiet about it.

    A heuristic, deliberately loose. Real single-day moves reach roughly 25-30%
    on an earnings gap, while split artifacts sit near 90%. Anything flagged
    here is reported, never dropped — deciding a real crash was a data defect
    would be its own kind of lie.
    """
    out = []
    for i in range(1, len(closes)):
        prev, now = closes[i - 1], closes[i]
        if prev <= 0 or now <= 0:
            continue
        move = now / prev - 1.0
        if abs(move) >= threshold:
            out.append((times[i], move))
    return out


def warn_on_suspicious_moves(label: str, bars: OhlcBars) -> None:
    flagged = suspicious_moves(bars.closes, bars.times)
    if not flagged:
        return
    worst = min(flagged, key=lambda row: row[1])
    log.warning(
        "%s: %d bar(s) move more than %.0f%% in one step, worst %+.1f%% on %s. "
        "This is what an unadjusted split looks like. Re-fetch before trusting "
        "any number computed on these bars.",
        label, len(flagged), SPLIT_MOVE_THRESHOLD * 100, worst[1] * 100,
        worst[0].date(),
    )


def load_ohlc(kind: str, symbol: str, data_dir: Path = DATA_DIR) -> OhlcBars:
    """Full bars for a cached symbol. Same kind guard as load_bars, so this
    cannot reach the holdout directory either."""
    if kind not in ("intraday", "daily"):
        raise ValueError(f"unknown dataset kind: {kind!r}")
    bars = _read_ohlc(Path(data_dir) / kind / f"{symbol}.csv",
                      session_only=(kind == "intraday"))
    warn_on_suspicious_moves(f"{kind}/{symbol}", bars)
    return bars


def load_vrp_bars(symbol: str, data_dir: Path = DATA_DIR) -> Bars:
    """Daily bars of a VRP underlying. Deliberately NOT the `daily/` cache:
    that one is quarantined to end before the holdout window, while these run
    to the present and must never be reachable from the regime check."""
    return _read_bars(Path(data_dir) / "vrp" / f"{symbol}.csv")


def load_vrp_ohlc(symbol: str, data_dir: Path = DATA_DIR) -> OhlcBars:
    """The same bars with every column, for the barrier test."""
    return _read_ohlc(Path(data_dir) / "vrp" / f"{symbol}.csv")


# --- fetching (the only network code in the research package) ---

def fetch_all(cfg: Settings, intraday_days: int = 365, daily_years: int = 5,
              data_dir: Path = DATA_DIR) -> None:
    """Download intraday + daily history for every universe symbol, split off
    the holdout slice at ONE shared calendar cutoff, and write the CSV cache.

    Two passes on purpose: the research/holdout cutoff cannot be known until
    every symbol's bars are in hand, because it is a quantile of the pooled
    timeline rather than of any one symbol's bar count.
    """
    client = StockHistoricalDataClient(cfg.api_key, cfg.secret_key)

    # Pass 1: download. Session-filter before splitting so the fractions are
    # computed over tradeable bars only.
    intraday_by_symbol: dict[str, OhlcBars] = {}
    daily_by_symbol: dict[str, OhlcBars] = {}
    for symbol in cfg.symbols:
        intraday = filter_ohlc_to_session(_fetch_ohlc(
            client, symbol,
            TimeFrame(cfg.bar_timeframe_minutes, TimeFrameUnit.Minute),
            days=intraday_days,
        ))
        if not len(intraday):
            log.warning("%s: no intraday bars returned; skipping symbol", symbol)
            continue
        intraday_by_symbol[symbol] = intraday
        daily_by_symbol[symbol] = _fetch_ohlc(client, symbol, TimeFrame.Day,
                                              days=daily_years * 365)

    if not intraday_by_symbol:
        log.warning("no symbols returned intraday bars; nothing cached")
        return

    # Pass 2: one cutoff for the whole universe, then write. The cutoff is a
    # quantile of the pooled timeline, so it cannot be known until every
    # symbol's bars are in hand.
    cutoff = union_cutoff({s: b.times for s, b in intraday_by_symbol.items()},
                          RESEARCH_FRACTION)
    if cutoff is None:
        log.warning("no timestamps to split on; nothing cached")
        return

    splits = {s: split_indices(b.times, cutoff)
              for s, b in intraday_by_symbol.items()}
    holdout_start = min(
        (intraday_by_symbol[s].times[after[0]] for s, (_, after) in splits.items() if after),
        default=None)
    log.info("shared research/holdout cutoff -> holdout starts %s", holdout_start)

    for symbol, bars in intraday_by_symbol.items():
        before, after = splits[symbol]
        research, holdout = bars.take(before), bars.take(after)
        save_ohlc(Path(data_dir) / "intraday" / f"{symbol}.csv", research)
        save_ohlc(Path(data_dir) / "holdout" / f"{symbol}.csv", holdout)

        daily = daily_by_symbol[symbol]
        if holdout_start is not None:
            # Quarantine the same calendar window in the dailies, using the
            # SHARED holdout start so no symbol's dailies reach past it. Cut on
            # the DAY, not the timestamp: a daily bar stamped 00:00 ET compares
            # as earlier than a holdout that opens mid-session, while covering
            # that whole session.
            daily = ohlc_before_date(daily, cutoff=holdout_start)
        save_ohlc(Path(data_dir) / "daily" / f"{symbol}.csv", daily)

        log.info("%s: cached %d intraday research bars, %d holdout, %d daily",
                 symbol, len(research), len(holdout), len(daily))


def _fetch_ohlc(client: StockHistoricalDataClient, symbol: str,
                timeframe: TimeFrame, days: int,
                feed: DataFeed = DataFeed.IEX) -> OhlcBars:
    start = datetime.now(UTC) - timedelta(days=days)
    bars = cast(BarSet, client.get_stock_bars(
        StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=timeframe,
            start=start,
            feed=feed,
            # Without this, a 20:1 split reads as a -95% single-bar "move".
            adjustment=Adjustment.ALL,
        )
    )).data.get(symbol, [])
    return OhlcBars(
        times=[b.timestamp for b in bars],
        opens=[float(b.open) for b in bars],
        highs=[float(b.high) for b in bars],
        lows=[float(b.low) for b in bars],
        closes=[float(b.close) for b in bars],
        volumes=[float(b.volume or 0.0) for b in bars],
        has_intrabar=True,
    )


def _fetch(client: StockHistoricalDataClient, symbol: str,
           timeframe: TimeFrame, days: int,
           feed: DataFeed = DataFeed.IEX) -> Bars:
    return _fetch_ohlc(client, symbol, timeframe, days, feed).bars


# --- CBOE volatility indices: the implied-vol side of the VRP test ---
#
# The variance risk premium IS implied minus subsequently realized volatility,
# so it cannot be measured without real implied vol. bot/simulator.py has none
# (premium is a flat 0.005 of spot), and synthesising it would assume the
# answer. CBOE publishes each index's full daily history as a free CSV; the
# value is annualized volatility in percentage points.
#
# Each index is mapped to the tradeable ETF whose realized vol it should be
# compared against. VIX measures SPX, not SPY: the two differ by dividends,
# which are small and smooth relative to volatility, so SPY realized vol is the
# standard stand-in. That approximation is stated rather than hidden.
VOL_INDEX_UNDERLYING: dict[str, str] = {"VIX": "SPY", "VXN": "QQQ", "RVX": "IWM"}
CBOE_URL = "https://cdn.cboe.com/api/global/us_indices/daily_prices/{name}_History.csv"


def parse_cboe_csv(text: str) -> Bars:
    """Parse a CBOE daily-price CSV into (closes, times). Pure, so the parsing
    rules are testable without touching the network.

    Rows whose close is missing, unparseable, or non-positive are dropped: the
    early history of some indices carries placeholder rows, and a 0.0 implied
    vol would read as a huge variance risk premium rather than as missing data.
    """
    closes: list[float] = []
    times: list[datetime] = []
    for row in csv.DictReader(text.splitlines()):
        raw_date, raw_close = (row.get("DATE") or "").strip(), (row.get("CLOSE") or "").strip()
        if not raw_date or not raw_close:
            continue
        try:
            stamp = datetime.strptime(raw_date, "%m/%d/%Y").replace(tzinfo=UTC)
            close = float(raw_close)
        except ValueError:
            continue
        if close <= 0:
            continue
        times.append(stamp)
        closes.append(close)
    return closes, times


def fetch_vol_index(name: str) -> Bars:
    """Download one CBOE volatility index's full daily history."""
    request = Request(CBOE_URL.format(name=name), headers={"User-Agent": USER_AGENT})
    with urlopen(request, timeout=60) as response:
        text = response.read().decode("utf-8", "replace")
    return parse_cboe_csv(text)


def fetch_vrp_inputs(cfg: Settings, years: int = 12,
                     data_dir: Path = DATA_DIR) -> dict[str, str]:
    """Cache the implied/realized pair the VRP test needs: each CBOE index plus
    the daily bars of the ETF it is compared against.

    Uses the SIP feed rather than the IEX default. IEX daily history starts in
    November 2018, which excludes the February 2018 volatility event; SIP starts
    in January 2016 and covers it. A short-vol thesis is judged on exactly those
    events, so the earlier coverage is the point. Over the range both feeds
    cover, they agree bar for bar.

    The underlying bars go to their own `vrp/` directory, NOT to `daily/`.
    `daily/` was quarantined at fetch time to end before the holdout window, and
    robustness.daily_regime_check reads it — overwriting it with history running
    to today would push holdout prices into that check.

    Returns {index name: underlying symbol} for the pairs that cached cleanly.
    """
    client = StockHistoricalDataClient(cfg.api_key, cfg.secret_key)
    cached: dict[str, str] = {}
    for name, symbol in VOL_INDEX_UNDERLYING.items():
        implied = fetch_vol_index(name)
        if not implied[0]:
            log.warning("%s: no implied-vol history returned; skipping pair", name)
            continue
        underlying = _fetch_ohlc(client, symbol, TimeFrame.Day, days=years * 365,
                                 feed=DataFeed.SIP)
        if not len(underlying):
            log.warning("%s: no daily bars for %s; skipping pair", name, symbol)
            continue
        save_bars(Path(data_dir) / "volidx" / f"{name}.csv", *implied)
        # Full bars: without a high and a low the barrier test cannot tell where
        # price went inside a daily bar, and every exit books the whole
        # close-to-close move.
        save_ohlc(Path(data_dir) / "vrp" / f"{symbol}.csv", underlying)
        cached[name] = symbol
        log.info("%s/%s: %d implied (%s..%s), %d daily (%s..%s)",
                 name, symbol,
                 len(implied[0]), implied[1][0].date(), implied[1][-1].date(),
                 len(underlying), underlying.times[0].date(),
                 underlying.times[-1].date())
    return cached
