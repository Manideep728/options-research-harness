"""The trading loop: clock -> reconcile orders -> exits -> entries -> sleep.

Depends only on the broker interface (duck-typed), never on alpaca directly,
so the whole cycle is testable with a fake broker.
"""

import logging
import time
from datetime import date, datetime, time as dtime, timedelta, timezone

from bot import journal, risk, state
from bot.config import Settings
from bot.options import pick_contract
from bot.strategy import Action, evaluate

log = logging.getLogger("bot.engine")


class Engine:
    def __init__(self, broker, cfg: Settings):
        self.broker = broker
        self.cfg = cfg
        self._session_open_cache: tuple[date, object] | None = None

    # --- public API ---

    def run_forever(self) -> None:
        log.info(
            "starting loop: symbols=%s interval=%ss dry_run=%s",
            ",".join(self.cfg.symbols), self.cfg.loop_interval_sec, self.cfg.dry_run,
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

        if not self._in_entry_window(clock):
            return
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

        for symbol in self.cfg.symbols:
            closes = self.broker.get_closes(symbol)
            signal = evaluate(closes, self.cfg)
            log.info("%s: %s -> %s", symbol, signal.action.value, signal.reason)
            if signal.action == Action.NONE:
                continue

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
    return datetime.combine(now.date(), dtime(0, 0), tzinfo=timezone.utc)
