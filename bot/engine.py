"""The trading loop: clock -> reconcile orders -> exits -> entries -> sleep.

Depends only on the broker interface (duck-typed), never on alpaca directly,
so the whole cycle is testable with a fake broker.
"""

import logging
import time
from datetime import UTC, date, datetime, timedelta
from datetime import time as dtime

from bot import control, earnings, journal, risk, scanner, state, watchlist
from bot.config import Settings
from bot.options import pick_contract
from bot.strategy import Action, evaluate

log = logging.getLogger("bot.engine")


class Engine:
    def __init__(self, broker, cfg: Settings):
        self.broker = broker
        self.cfg = cfg
        self._session_open_cache: tuple[date, object] | None = None
        # Two-tier scan state: the shortlist the fast loop polls, and when it
        # was last re-ranked from the full universe.
        self._active: tuple[str, ...] = ()
        self._last_ranked: datetime | None = None
        # Per (symbol, action) time a signal last fired — the trade cooldown.
        self._last_signal: dict[tuple[str, str], datetime] = {}

    # --- public API ---

    def run_forever(self) -> None:
        log.info(
            "starting loop: universe=%d poll=%ss rank=%ss top=%d dry_run=%s",
            len(self.cfg.symbols), self.cfg.loop_interval_sec,
            self.cfg.scan_interval_sec, self.cfg.active_list_size, self.cfg.dry_run,
        )
        while True:
            try:
                self.run_cycle()
            except KeyboardInterrupt:
                log.info("shutdown requested — exiting cleanly")
                return
            except Exception:
                log.exception("cycle failed; continuing after sleep")
            time.sleep(self.cfg.loop_interval_sec)

    def run_cycle(self) -> None:
        clock = self.broker.get_clock()
        if not clock.is_open:
            log.info("market closed; next open %s", clock.next_open)
            return

        open_orders, fills = self.broker.get_todays_option_orders(_day_start(clock.now))
        added = journal.append_fills(self.cfg.journal_file, fills)
        if added:
            log.info("journaled %d new fill(s)", added)
        self._cancel_stale_orders(open_orders, clock.now)

        positions = self.broker.get_option_positions()
        self.manage_exits(positions, clock.now, open_orders)

        control_state = control.load_control(self.cfg.control_file)
        if control_state.paused:
            log.info("bot paused; skipping new entries")
            return

        if not self._in_entry_window(clock):
            return
        self._refresh_active_list(clock.now)
        self.scan_entries(clock, positions, open_orders, fills)

    # --- exits ---

    def manage_exits(self, positions, now: datetime, open_orders) -> None:
        exiting = {o.symbol for o in open_orders if o.side == "sell"}
        for p in positions:
            if p.symbol in exiting:
                log.info("%s: exit order already working", p.symbol)
                continue
            if p.current_price <= 0:
                log.warning("%s: no price; skipping exit check", p.symbol)
                continue
            held_days = None
            entered = journal.entry_time(self.cfg.journal_file, p.symbol)
            if entered is not None:
                held_days = (now - entered).total_seconds() / 86400
            reason = risk.exit_reason(p, self.cfg, held_days)
            if reason:
                log.info("EXIT %s (%s): %s", p.symbol, p.underlying, reason)
                self.broker.close_option(p.symbol, p.qty, p.current_price)
            else:
                log.info(
                    "HOLD %s: P&L %+.1f%% (on bid), %d DTE",
                    p.symbol, p.pnl_pct * 100, p.days_to_expiry,
                )

    # --- entries ---

    def scan_entries(self, clock, positions, open_orders, fills) -> None:
        today = clock.now.date()
        equity = self.broker.get_equity()
        anchor = self.broker.get_last_equity() or equity
        day = state.load_day_state(self.cfg.state_file, today, anchor)

        # Trades today per the broker's books: filled buys + working buy
        # orders. The state counter covers DRY_RUN (no real orders exist)
        # and same-cycle submissions; take the more conservative number.
        open_buys = [o for o in open_orders if o.side == "buy"]
        broker_count = sum(1 for f in fills if f.side == "buy") + len(open_buys)
        trades_today = max(day.trades_today, broker_count)

        # A working buy order occupies its underlying's slot.
        gate_positions = list(positions) + [
            risk.OpenPosition(
                symbol=o.symbol, underlying=o.underlying, qty=1,
                avg_entry_price=0.0, current_price=0.0, days_to_expiry=99,
            )
            for o in open_buys
        ]

        for symbol in self._active:
            closes = self.broker.get_closes(symbol)
            signal = evaluate(closes, self.cfg)
            log.info("%s: %s -> %s", symbol, signal.action.value, signal.reason)
            if signal.action == Action.NONE:
                continue

            # Cooldown: a signal that already fired within the window is
            # suppressed here — before the chain fetch — so the same setup
            # can't re-trigger (or re-spam) on every 30s poll. We record the
            # fire now, so ALL downstream outcomes (entered, gated, no
            # contract) share one cooldown, not just successful entries.
            if self._on_cooldown(symbol, signal.action, clock.now):
                log.info("SKIP %s: %s on cooldown", symbol, signal.action.value)
                continue
            self._mark_fired(symbol, signal.action, clock.now)

            gate = risk.entry_allowed(
                symbol, gate_positions, trades_today, equity, day.day_start_equity, self.cfg
            )
            if not gate.allowed:
                log.info("SKIP %s: %s", symbol, gate.reason)
                continue

            px = self.broker.get_underlying_price(symbol)
            want = "call" if signal.action == Action.BUY_CALL else "put"
            chain = self.broker.get_chain(symbol, want, today, px)
            contract, rejections = pick_contract(chain, signal.action, px, today, self.cfg)
            if contract is None:
                log.info(
                    "SKIP %s: no qualifying %s (%d contracts rejected, e.g. %s)",
                    symbol, want, len(rejections),
                    rejections[0].reason if rejections else "empty chain",
                )
                continue

            sizing = risk.size_allowed(contract.ask, equity, self.cfg)
            if not sizing.allowed:
                log.info("SKIP %s: %s", symbol, sizing.reason)
                continue

            log.info(
                "ENTER %s: BUY 1 x %s (strike %.2f, exp %s, bid %.2f/ask %.2f) — %s",
                symbol, contract.symbol, contract.strike, contract.expiry,
                contract.bid, contract.ask, signal.reason,
            )
            self.broker.buy_option(contract, qty=1)
            day.trades_today = trades_today = trades_today + 1
            state.save_day_state(self.cfg.state_file, day)
            # Occupy the slot immediately so this cycle can't double-enter.
            gate_positions.append(
                risk.OpenPosition(
                    symbol=contract.symbol,
                    underlying=symbol,
                    qty=1,
                    avg_entry_price=contract.ask,
                    current_price=contract.ask,
                    days_to_expiry=(contract.expiry - today).days,
                )
            )

    # --- ranking (slow tier) ---

    def _refresh_active_list(self, now: datetime) -> None:
        """Re-rank the full universe into the shortlist the fast loop polls —
        but only once per scan_interval_sec. Between refreshes this is a cheap
        timestamp check and returns immediately, so the fast loop stays fast."""
        due = (
            self._last_ranked is None
            or (now - self._last_ranked).total_seconds() >= self.cfg.scan_interval_sec
        )
        if not due:
            return

        closes_by_symbol = {sym: self.broker.get_closes(sym) for sym in self.cfg.symbols}
        ranked = scanner.rank_symbols(closes_by_symbol, self.cfg)

        # Drop names in earnings blackout so they never occupy a polling slot.
        dates = earnings.load_earnings_dates(self.cfg.earnings_file)
        eligible = [
            s for s in ranked
            if not earnings.in_blackout(
                s.symbol, now.date(), dates, self.cfg.earnings_blackout_days
            )
        ]
        top = eligible[: self.cfg.active_list_size]
        self._active = tuple(s.symbol for s in top)
        self._last_ranked = now

        # Publish for the dashboard process (empty list is a valid state:
        # everything eligible was blacked out).
        watchlist.save_active(
            self.cfg.active_file,
            now,
            [watchlist.ActiveEntry(s.symbol, s.score, s.detail) for s in top],
        )

        log.info("re-ranked %d symbols -> active shortlist:", len(ranked))
        for s in top:
            log.info("  %-6s score=%.6f  %s", s.symbol, s.score, s.detail)

    # --- cooldown ---

    def _on_cooldown(self, symbol: str, action: Action, now: datetime) -> bool:
        last = self._last_signal.get((symbol, action.value))
        if last is None:
            return False
        return (now - last).total_seconds() < self.cfg.signal_cooldown_sec

    def _mark_fired(self, symbol: str, action: Action, now: datetime) -> None:
        self._last_signal[(symbol, action.value)] = now

    # --- helpers ---

    def _cancel_stale_orders(self, open_orders, now: datetime) -> None:
        """A marketable limit should fill in seconds; one still open after
        the timeout is off-market and must not lurk as a resting order."""
        for o in open_orders:
            age = (now - o.submitted_at).total_seconds()
            if age >= self.cfg.stale_order_cancel_sec:
                log.info("canceling stale %s order %s (%.0fs old)", o.side, o.symbol, age)
                self.broker.cancel_order(o.id)

    def _in_entry_window(self, clock) -> bool:
        """No new entries in the first/last N minutes of the session."""
        today = clock.now.date()
        if self._session_open_cache is None or self._session_open_cache[0] != today:
            self._session_open_cache = (today, self.broker.session_open_time(today))
        session_open = self._session_open_cache[1]
        if session_open is None:
            log.warning("no session open time available; skipping entries")
            return False

        since_open = clock.now - session_open
        until_close = clock.next_close - clock.now
        if since_open < timedelta(minutes=self.cfg.skip_open_minutes):
            log.info("entry window closed: %d min since open", since_open.seconds // 60)
            return False
        if until_close < timedelta(minutes=self.cfg.skip_close_minutes):
            log.info("entry window closed: %d min to close", until_close.seconds // 60)
            return False
        return True


def _day_start(now: datetime) -> datetime:
    return datetime.combine(now.date(), dtime(0, 0), tzinfo=UTC)
