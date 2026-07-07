from datetime import datetime, timedelta, timezone

from bot import journal
from bot.broker import Fill

T0 = datetime(2026, 7, 6, 15, 0, tzinfo=timezone.utc)


def fill(order_id, side, price, ts=T0, symbol="SPY260716C00120000"):
    return Fill(order_id=order_id, filled_at=ts, symbol=symbol,
                underlying="SPY", side=side, qty=1, price=price)


def test_buy_then_sell_realizes_pnl(tmp_path):
    path = str(tmp_path / "trades.csv")
    journal.append_fills(path, [fill("b1", "buy", 1.00)])
    journal.append_fills(path, [fill("s1", "sell", 1.50, ts=T0 + timedelta(hours=2))])
    rows = journal.read_rows(path)
    assert len(rows) == 2
    # (1.50 - 1.00) * 100 shares = $50
    assert rows[1]["realized_pnl"] == "50.00"


def test_duplicate_order_ids_not_rejournaled(tmp_path):
    path = str(tmp_path / "trades.csv")
    assert journal.append_fills(path, [fill("b1", "buy", 1.00)]) == 1
    assert journal.append_fills(path, [fill("b1", "buy", 1.00)]) == 0
    assert len(journal.read_rows(path)) == 1


def test_fifo_matching_two_round_trips(tmp_path):
    path = str(tmp_path / "trades.csv")
    journal.append_fills(path, [
        fill("b1", "buy", 1.00),
        fill("s1", "sell", 1.20, ts=T0 + timedelta(hours=1)),
        fill("b2", "buy", 2.00, ts=T0 + timedelta(hours=2)),
        fill("s2", "sell", 1.40, ts=T0 + timedelta(hours=3)),
    ])
    rows = journal.read_rows(path)
    assert rows[1]["realized_pnl"] == "20.00"    # 1.20 vs 1.00
    assert rows[3]["realized_pnl"] == "-60.00"   # 1.40 vs 2.00


def test_entry_time_open_position(tmp_path):
    path = str(tmp_path / "trades.csv")
    journal.append_fills(path, [fill("b1", "buy", 1.00)])
    assert journal.entry_time(path, "SPY260716C00120000") == T0


def test_entry_time_none_after_close(tmp_path):
    path = str(tmp_path / "trades.csv")
    journal.append_fills(path, [
        fill("b1", "buy", 1.00),
        fill("s1", "sell", 1.50, ts=T0 + timedelta(hours=1)),
    ])
    assert journal.entry_time(path, "SPY260716C00120000") is None


def test_entry_time_unknown_symbol(tmp_path):
    assert journal.entry_time(str(tmp_path / "trades.csv"), "XYZ") is None
