"""End-to-end cycle tests against a fake broker (no network)."""

import json
from datetime import date, datetime, timedelta, timezone

from bot import journal, watchlist
from bot.broker import ClockInfo, Fill, OpenOrder
from bot.config import Settings
from bot.engine import Engine
from bot.options import Contract
from bot.risk import OpenPosition

TODAY = date(2026, 7, 6)
NOW = datetime(2026, 7, 6, 15, 0, tzinfo=timezone.utc)  # 11:00 ET, mid-session
OPEN_T = datetime(2026, 7, 6, 13, 30, tzinfo=timezone.utc)
CLOSE_T = datetime(2026, 7, 6, 20, 0, tzinfo=timezone.utc)


def bounce_closes() -> list[float]:
    """Uptrend + pullback + recovery bar -> BUY_CALL (same as strategy tests)."""
    closes = [100.0 + 0.3 * (i + 1) for i in range(60)]
    px = closes[-1]
    closes += [px - 1.0 * (i + 1) for i in range(6)]
    closes += [closes[-1] + 3.0]
    return closes


def flat_closes() -> list[float]:
    return [100.0] * 80


class FakeBroker:
    def __init__(self, closes_by_symbol, positions=(), is_open=True, chain=None,
                 open_orders=(), fills=()):
        self.closes_by_symbol = closes_by_symbol
        self.positions = list(positions)
        self.is_open = is_open
        self.open_orders = list(open_orders)
        self.fills = list(fills)
        self.chain = chain if chain is not None else [
            Contract(
                symbol="SPY260716C00120000", underlying="SPY",
                expiry=TODAY + timedelta(days=10), strike=120.0, call_put="call",
                bid=1.00, ask=1.05, open_interest=5000,
            )
        ]
        self.bought: list[str] = []
        self.closed: list[str] = []
        self.canceled: list[str] = []

    def get_clock(self):
        return ClockInfo(is_open=self.is_open, now=NOW, next_open=NOW, next_close=CLOSE_T)

    def session_open_time(self, day):
        return OPEN_T

    def get_equity(self):
        return 100_000.0

    def get_last_equity(self):
        return 100_000.0

    def get_option_positions(self):
        return list(self.positions)

    def get_todays_option_orders(self, day_start):
        return list(self.open_orders), list(self.fills)

    def cancel_order(self, order_id):
        self.canceled.append(order_id)

    def get_closes(self, symbol):
        return self.closes_by_symbol.get(symbol, flat_closes())

    def get_underlying_price(self, symbol):
        return self.closes_by_symbol.get(symbol, flat_closes())[-1]

    def get_chain(self, underlying, want, today, px):
        return self.chain

    def buy_option(self, contract, qty):
        self.bought.append(contract.symbol)
        return "fake-order"

    def close_option(self, occ_symbol, qty, bid):
        self.closed.append(occ_symbol)


def cfg_for(tmp_path, **overrides) -> Settings:
    return Settings(
        api_key="k", secret_key="s",
        symbols=overrides.pop("symbols", ("SPY",)),
        state_file=str(tmp_path / "state.json"),
        journal_file=str(tmp_path / "trades.csv"),
        control_file=str(tmp_path / "control.json"),
        active_file=str(tmp_path / "active.json"),
        # Point at a (by default absent) tmp file so tests never pick up the
        # repo's earnings.json and accidentally black a symbol out.
        earnings_file=overrides.pop("earnings_file", str(tmp_path / "earnings.json")),
        **overrides,
    )


def spy_position(entry=1.00, current=1.10, dte=10):
    return OpenPosition(
        symbol="SPY260716C00120000", underlying="SPY", qty=1,
        avg_entry_price=entry, current_price=current, days_to_expiry=dte,
    )


def test_signal_fires_and_order_is_placed(tmp_path):
    broker = FakeBroker({"SPY": bounce_closes()})
    Engine(broker, cfg_for(tmp_path)).run_cycle()
    assert broker.bought == ["SPY260716C00120000"]
    day = json.loads((tmp_path / "state.json").read_text())
    assert day["trades_today"] == 1


def test_no_signal_no_order(tmp_path):
    broker = FakeBroker({"SPY": flat_closes()})
    Engine(broker, cfg_for(tmp_path)).run_cycle()
    assert broker.bought == []


def test_market_closed_does_nothing(tmp_path):
    broker = FakeBroker({"SPY": bounce_closes()}, is_open=False)
    Engine(broker, cfg_for(tmp_path)).run_cycle()
    assert broker.bought == []
    assert broker.closed == []


def test_take_profit_exit_closes_position(tmp_path):
    broker = FakeBroker({"SPY": flat_closes()}, positions=[spy_position(current=1.60)])
    Engine(broker, cfg_for(tmp_path)).run_cycle()
    assert broker.closed == ["SPY260716C00120000"]


def test_healthy_position_is_held(tmp_path):
    broker = FakeBroker({"SPY": flat_closes()}, positions=[spy_position()])
    Engine(broker, cfg_for(tmp_path)).run_cycle()
    assert broker.closed == []


def test_existing_position_blocks_reentry_same_underlying(tmp_path):
    broker = FakeBroker({"SPY": bounce_closes()}, positions=[spy_position()])
    Engine(broker, cfg_for(tmp_path)).run_cycle()
    assert broker.bought == []


def test_daily_trade_limit_blocks_entry(tmp_path):
    cfg = cfg_for(tmp_path)
    (tmp_path / "state.json").write_text(json.dumps({
        "day": TODAY.isoformat(), "trades_today": 3, "day_start_equity": 100_000.0,
    }))
    broker = FakeBroker({"SPY": bounce_closes()})
    Engine(broker, cfg).run_cycle()
    assert broker.bought == []


def test_no_qualifying_contract_no_order(tmp_path):
    broker = FakeBroker({"SPY": bounce_closes()}, chain=[])
    Engine(broker, cfg_for(tmp_path)).run_cycle()
    assert broker.bought == []


def test_two_symbols_same_cycle_both_can_enter(tmp_path):
    broker = FakeBroker({"SPY": bounce_closes(), "QQQ": bounce_closes()})
    Engine(broker, cfg_for(tmp_path, symbols=("SPY", "QQQ"))).run_cycle()
    assert len(broker.bought) == 2


# --- reconciliation behaviors ---

def test_stale_open_order_gets_canceled(tmp_path):
    stale = OpenOrder(id="o1", symbol="SPY260716C00120000", underlying="SPY",
                      side="buy", submitted_at=NOW - timedelta(seconds=300))
    fresh = OpenOrder(id="o2", symbol="SPY260716C00121000", underlying="SPY",
                      side="buy", submitted_at=NOW - timedelta(seconds=30))
    broker = FakeBroker({"SPY": flat_closes()}, open_orders=[stale, fresh])
    Engine(broker, cfg_for(tmp_path)).run_cycle()
    assert broker.canceled == ["o1"]


def test_fills_are_journaled(tmp_path):
    fill = Fill(order_id="f1", filled_at=NOW, symbol="SPY260716C00120000",
                underlying="SPY", side="buy", qty=1, price=1.02)
    broker = FakeBroker({"SPY": flat_closes()}, fills=[fill])
    Engine(broker, cfg_for(tmp_path)).run_cycle()
    rows = journal.read_rows(str(tmp_path / "trades.csv"))
    assert [r["order_id"] for r in rows] == ["f1"]


def test_open_buy_order_blocks_entry_same_underlying(tmp_path):
    working = OpenOrder(id="o1", symbol="SPY260716C00121000", underlying="SPY",
                        side="buy", submitted_at=NOW - timedelta(seconds=10))
    broker = FakeBroker({"SPY": bounce_closes()}, open_orders=[working])
    Engine(broker, cfg_for(tmp_path)).run_cycle()
    assert broker.bought == []


def test_exit_skipped_when_sell_order_already_working(tmp_path):
    selling = OpenOrder(id="o1", symbol="SPY260716C00120000", underlying="SPY",
                        side="sell", submitted_at=NOW - timedelta(seconds=10))
    broker = FakeBroker(
        {"SPY": flat_closes()},
        positions=[spy_position(current=1.60)],  # would otherwise take profit
        open_orders=[selling],
    )
    Engine(broker, cfg_for(tmp_path)).run_cycle()
    assert broker.closed == []


def test_max_hold_exit_uses_journal_entry_time(tmp_path):
    cfg = cfg_for(tmp_path)
    old_buy = Fill(order_id="b1", filled_at=NOW - timedelta(days=3),
                   symbol="SPY260716C00120000", underlying="SPY",
                   side="buy", qty=1, price=1.00)
    journal.append_fills(cfg.journal_file, [old_buy])
    broker = FakeBroker({"SPY": flat_closes()}, positions=[spy_position()])
    Engine(broker, cfg).run_cycle()
    assert broker.closed == ["SPY260716C00120000"]


# --- two-tier scan: ranking, cooldown, blackout ---

def test_only_top_ranked_symbols_are_polled(tmp_path):
    # Universe of two; only one is set up. With room for one active name,
    # the flat one must not make the shortlist and must not be evaluated.
    broker = FakeBroker({"SPY": bounce_closes(), "QQQ": flat_closes()})
    engine = Engine(broker, cfg_for(tmp_path, symbols=("SPY", "QQQ"), active_list_size=1))
    engine.run_cycle()
    assert engine._active == ("SPY",)
    assert broker.bought == ["SPY260716C00120000"]


def test_cooldown_suppresses_repeat_signal_same_bar(tmp_path):
    # Same clock time on both cycles: the second identical signal is on
    # cooldown and must not place a second order.
    broker = FakeBroker({"SPY": bounce_closes()})
    engine = Engine(broker, cfg_for(tmp_path))
    engine.run_cycle()
    engine.run_cycle()
    assert broker.bought == ["SPY260716C00120000"]  # exactly one, not two


def test_signal_refires_after_cooldown_expires(tmp_path):
    broker = FakeBroker({"SPY": bounce_closes()})
    cfg = cfg_for(tmp_path, signal_cooldown_sec=300)
    engine = Engine(broker, cfg)
    engine.run_cycle()
    # Push the recorded fire time back beyond the cooldown window.
    engine._last_signal[("SPY", "BUY_CALL")] = NOW - timedelta(seconds=301)
    engine.run_cycle()
    assert broker.bought == ["SPY260716C00120000", "SPY260716C00120000"]


def test_active_list_reranks_only_when_due(tmp_path):
    broker = FakeBroker({"SPY": bounce_closes(), "QQQ": flat_closes()})
    cfg = cfg_for(tmp_path, symbols=("SPY", "QQQ"), active_list_size=1,
                  scan_interval_sec=1800)
    engine = Engine(broker, cfg)
    engine.run_cycle()
    assert engine._active == ("SPY",)

    # Flip which symbol is set up, then run again inside the interval: the
    # shortlist must be unchanged because we haven't re-ranked yet.
    broker.closes_by_symbol = {"SPY": flat_closes(), "QQQ": bounce_closes()}
    engine.run_cycle()
    assert engine._active == ("SPY",)

    # Age the last-ranked stamp past the interval; now it re-ranks to QQQ.
    engine._last_ranked = NOW - timedelta(seconds=1801)
    engine.run_cycle()
    assert engine._active == ("QQQ",)


def test_earnings_blackout_keeps_symbol_off_shortlist(tmp_path):
    earnings_file = tmp_path / "earnings.json"
    earnings_file.write_text(json.dumps({"AAPL": NOW.date().isoformat()}))
    broker = FakeBroker(
        {"AAPL": bounce_closes()},
        chain=[Contract(
            symbol="AAPL260716C00120000", underlying="AAPL",
            expiry=TODAY + timedelta(days=10), strike=120.0, call_put="call",
            bid=1.00, ask=1.05, open_interest=5000,
        )],
    )
    engine = Engine(broker, cfg_for(
        tmp_path, symbols=("AAPL",), earnings_file=str(earnings_file)
    ))
    engine.run_cycle()
    assert engine._active == ()          # blacked out -> no polling slot
    assert broker.bought == []           # and therefore no trade


def test_refresh_publishes_shortlist_to_active_file(tmp_path):
    # The dashboard is a separate process, so the shortlist must land on disk.
    broker = FakeBroker({"SPY": bounce_closes(), "QQQ": flat_closes()})
    cfg = cfg_for(tmp_path, symbols=("SPY", "QQQ"), active_list_size=1)
    Engine(broker, cfg).run_cycle()
    shortlist = watchlist.load_active(cfg.active_file)
    assert [e.symbol for e in shortlist.entries] == ["SPY"]
    assert shortlist.ranked_at is not None
