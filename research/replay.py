"""Historical replay: drive the REAL trading engine over cached bars.

The gap this closes. bot/simulator.py reads 8 of the ~40 fields in Settings. It
knows about the signal and the exits, and nothing else — so `max_positions`,
`max_trades_per_day`, `max_positions_per_underlying`, `signal_cooldown_sec`,
`active_list_size`, `skip_open_minutes`/`skip_close_minutes`, the earnings
blackout, `min_open_interest` and `max_spread_pct_of_mid` were all absent from
every backtest ever run here. The simulator takes every signal on all 30
symbols with unlimited concurrency; the live bot takes at most 3 trades a day
from a 5-name shortlist. The README claimed the backtest was verified identical
to the live logic, which was true of strategy.evaluate() and false of the
engine.

No rewrite was needed to fix that, because bot/engine.py already depends only
on a duck-typed broker ("so the whole cycle is testable with a fake broker" —
its own docstring), and tests/test_engine.py's FakeBroker already proves 11
methods are enough to drive a full cycle. ReplayBroker is that same interface
backed by the CSV cache instead of Alpaca, so `Engine.run_cycle()` — the real
one, with the real bot/risk.py, bot/scanner.py and bot/options.py running
inside it — becomes the thing under test.

Two properties this file has to get right, or it is worse than useless:

  1. Causality. get_closes(symbol) returns bars up to and including the
     current replay index and never one bar further. The engine is entitled to
     assume it cannot see the future; a leak here would be invisible and would
     make every result meaningless.
  2. One P&L model. Open positions are repriced through
     bot.simulator.option_return, the identical function simulate() uses. A
     second pricing model living here is exactly the backtest/live drift this
     module exists to eliminate.

What it still does not model, stated rather than glossed: real option quotes
(the chain is synthesized from SimParams, so no IV, no smile, no term
structure); the limit-order lifecycle, because buys fill immediately at the ask
whereas the live engine posts a marketable limit and cancels it unfilled after
120s — so the stale-order path is NOT exercised here and non-fills, which are
adversely selected, are not modelled; partial fills; assignment; and
multi-contract sizing. See SimParams for the pricing model's own limitations.
"""

import logging
from bisect import bisect_right
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from bot import pricing
from bot.broker import ClockInfo, Fill, OpenOrder
from bot.config import Settings
from bot.options import Contract
from bot.risk import OpenPosition
from bot.simulator import SimParams, option_return, premium_of
from research.data import MARKET_TZ, SESSION_CLOSE, SESSION_OPEN, Bars, in_session

log = logging.getLogger("research.replay")

# How far OTM the synthesized contract sits, and how long it has to run, both
# come from SimParams — the same numbers simulate() prices against. Keeping a
# second copy here is how the replay and the backtest drift apart.
OPEN_INTEREST = 5_000
# Quoted spread as a fraction of mid. Must stay clear of the live gate's
# max_spread_pct_of_mid (0.10) with room for rounding to cents: quoting exactly
# at the limit rounds to 10.7% on a $0.56 mid and gets the contract rejected.
# 5% is also the more honest number — the limit is the worst case the gate
# tolerates, not what a liquid contract actually quotes.
SPREAD_PCT_OF_MID = 0.05


@dataclass
class _Holding:
    """An open option position plus the state needed to reprice it."""

    contract: Contract
    qty: int
    entry_premium: float
    entry_spot: float
    entry_time: datetime
    direction: float          # +1.0 for a call, -1.0 for a put


@dataclass
class ReplayStats:
    """What the replay observed. Deliberately thin — the trade journal the
    engine itself writes is the authoritative record."""

    cycles: int = 0
    buys: int = 0
    sells: int = 0
    cancels: int = 0
    blocked_entries: int = 0
    fills: list[Fill] = field(default_factory=list)

    @property
    def realized_returns(self) -> list[float]:
        """Per-trade return on premium, matched buy-to-sell in order."""
        out: list[float] = []
        opens: dict[str, Fill] = {}
        for f in self.fills:
            if f.side == "buy":
                opens[f.symbol] = f
            else:
                entry = opens.pop(f.symbol, None)
                if entry is not None and entry.price > 0:
                    out.append((f.price - entry.price) / entry.price)
        return out


class ReplayBroker:
    """The 11-method broker interface, backed by cached bars.

    `timeline` is the union of every symbol's bar timestamps, so one step of
    the replay is one moment in time for the whole universe — the engine polls
    all its symbols at a single `now`, and staggering them would let one symbol
    act on a later bar than another.
    """

    def __init__(self, bars: dict[str, Bars], cfg: Settings,
                 sp: SimParams = SimParams(), equity: float = 100_000.0):
        self.bars = {s: b for s, b in bars.items() if b[0]}
        self.cfg = cfg
        self.sp = sp
        self.start_equity = equity
        self.equity = equity
        self._day_open_equity = equity
        self.stats = ReplayStats()

        self._times: dict[str, list[datetime]] = {s: t for s, (_, t) in self.bars.items()}
        self._closes: dict[str, list[float]] = {s: c for s, (c, _) in self.bars.items()}
        self.timeline: list[datetime] = sorted({t for ts in self._times.values() for t in ts})
        self.index = 0

        self._holdings: dict[str, _Holding] = {}
        self._orders: list[OpenOrder] = []
        self._order_seq = 0
        self._today: date | None = None

    # --- replay driving ---

    @property
    def now(self) -> datetime:
        return self.timeline[min(self.index, len(self.timeline) - 1)]

    def step(self) -> bool:
        """Advance one bar. False when the timeline is exhausted."""
        self.index += 1
        if self.index >= len(self.timeline):
            return False
        if self._today != self.now.date():
            # New session: reset the circuit-breaker anchor the way a real
            # trading day does.
            self._today = self.now.date()
            self._day_open_equity = self.equity
        return True

    # --- clock ---

    def get_clock(self) -> ClockInfo:
        now = self.now
        return ClockInfo(
            is_open=in_session(now),
            now=now,
            next_open=_session_bound(now + timedelta(days=1), SESSION_OPEN),
            next_close=_session_bound(now, SESSION_CLOSE),
        )

    def session_open_time(self, day: date) -> datetime:
        return _session_bound(datetime.combine(day, time(12, 0), tzinfo=MARKET_TZ),
                              SESSION_OPEN)

    # --- market data (the causality contract lives here) ---

    def get_closes(self, symbol: str) -> list[float]:
        """Bars up to and INCLUDING now, never beyond. bisect_right on the
        symbol's own timestamps, because symbols do not share a bar grid."""
        times = self._times.get(symbol)
        if not times:
            return []
        cut = bisect_right(times, self.now)
        return self._closes[symbol][max(0, cut - self.cfg.bar_history_count):cut]

    def get_underlying_price(self, symbol: str) -> float:
        closes = self.get_closes(symbol)
        return closes[-1] if closes else 0.0

    def get_chain(self, underlying: str, want: str, today: date,
                  px: float) -> list[Contract]:
        """One synthesized contract, priced from SimParams. The quote is a real
        two-sided spread so pick_contract's liquidity filter runs on it rather
        than being handed a free pass."""
        if px <= 0:
            return []
        # Strike and mid both come from bot/pricing.py, so a quote here is the
        # same contract at the same price simulate() would have used.
        direction = 1.0 if want == "call" else -1.0
        strike = pricing.strike_for(px, self.sp.otm_pct, want)
        mid = premium_of(px, px, direction, 0.0, self.sp)
        if mid <= 0:
            return []
        half_spread = mid * SPREAD_PCT_OF_MID / 2.0
        expiry = today + timedelta(days=int(self.sp.dte_days))
        return [Contract(
            symbol=_occ_symbol(underlying, expiry, strike, want),
            underlying=underlying,
            expiry=expiry,
            strike=strike,
            call_put=want,
            bid=round(mid - half_spread, 2),
            ask=round(mid + half_spread, 2),
            open_interest=OPEN_INTEREST,
        )]

    # --- account ---

    def get_equity(self) -> float:
        return self.equity

    def get_last_equity(self) -> float:
        return self._day_open_equity

    def get_option_positions(self) -> list[OpenPosition]:
        """Reprice every holding through the SAME model simulate() uses."""
        out = []
        for holding in self._holdings.values():
            out.append(OpenPosition(
                symbol=holding.contract.symbol,
                underlying=holding.contract.underlying,
                qty=holding.qty,
                avg_entry_price=holding.entry_premium,
                current_price=self._premium_now(holding),
                days_to_expiry=(holding.contract.expiry - self.now.date()).days,
            ))
        return out

    def _premium_now(self, holding: _Holding) -> float:
        spot = self.get_underlying_price(holding.contract.underlying)
        if spot <= 0:
            return holding.entry_premium
        days = (self.now - holding.entry_time).total_seconds() / 86400
        ret = option_return(spot, holding.entry_spot, holding.direction, days,
                            self.sp)
        return max(0.0, holding.entry_premium * (1.0 + ret))

    # --- orders ---

    def get_todays_option_orders(self, day_start: datetime) -> tuple[list[OpenOrder],
                                                                    list[Fill]]:
        today = self.now.date()
        working = [o for o in self._orders if o.submitted_at.date() == today]
        filled = [f for f in self.stats.fills if f.filled_at.date() == today]
        return working, filled

    def cancel_order(self, order_id: str) -> None:
        self._orders = [o for o in self._orders if o.id != order_id]
        self.stats.cancels += 1

    def buy_option(self, contract: Contract, qty: int) -> str:
        """Fill immediately at the ask. Optimistic and stated as such: the live
        engine posts a marketable limit and cancels it unfilled after 120s, and
        the misses are adversely selected."""
        spot = self.get_underlying_price(contract.underlying)
        self._holdings[contract.symbol] = _Holding(
            contract=contract, qty=qty, entry_premium=contract.ask, entry_spot=spot,
            entry_time=self.now, direction=1.0 if contract.call_put == "call" else -1.0,
        )
        self.equity -= contract.ask * 100 * qty
        self.stats.buys += 1
        return self._record_fill(contract, qty, "buy", contract.ask)

    def close_option(self, occ_symbol: str, qty: int, bid: float) -> str:
        holding = self._holdings.pop(occ_symbol, None)
        if holding is None:
            return ""
        premium = self._premium_now(holding)
        self.equity += premium * 100 * qty
        self.stats.sells += 1
        return self._record_fill(holding.contract, qty, "sell", premium)

    def _record_fill(self, contract: Contract, qty: int, side: str,
                     price: float) -> str:
        self._order_seq += 1
        order_id = f"replay-{self._order_seq}"
        self.stats.fills.append(Fill(
            order_id=order_id, filled_at=self.now, symbol=contract.symbol,
            underlying=contract.underlying, side=side, qty=qty, price=round(price, 2),
        ))
        return order_id


# --- session helpers ---

def _session_bound(ts: datetime, at: time) -> datetime:
    """`at` on ts's calendar day, in exchange-local time then back to ts's tz —
    so DST is handled instead of assumed."""
    local = ts.astimezone(MARKET_TZ)
    return local.replace(hour=at.hour, minute=at.minute, second=0,
                         microsecond=0).astimezone(ts.tzinfo)


def _occ_symbol(underlying: str, expiry: date, strike: float, want: str) -> str:
    """OCC-shaped so anything parsing it downstream sees a realistic symbol."""
    return (f"{underlying}{expiry:%y%m%d}{'C' if want == 'call' else 'P'}"
            f"{round(strike * 1000):08d}")


def run_replay(bars: dict[str, Bars], cfg: Settings, sp: SimParams = SimParams(),
               equity: float = 100_000.0, max_cycles: int | None = None) -> ReplayStats:
    """Walk the timeline, running one real Engine cycle per bar.

    The engine writes state.json / trades.csv / active.json / control.json at
    the paths in `cfg`, so callers MUST point those at a scratch directory —
    replaying over the live bot's files would corrupt them.
    """
    from bot.engine import Engine

    broker = ReplayBroker(bars, cfg, sp, equity)
    engine = Engine(broker, cfg)
    while True:
        if broker.get_clock().is_open:
            engine.run_cycle()
            broker.stats.cycles += 1
        if max_cycles is not None and broker.stats.cycles >= max_cycles:
            break
        if not broker.step():
            break
    log.info("replay finished: %d cycles, %d buys, %d sells, %d cancels, equity %.2f -> %.2f",
             broker.stats.cycles, broker.stats.buys, broker.stats.sells,
             broker.stats.cancels, broker.start_equity, broker.equity)
    return broker.stats
