"""Trade journal: every fill appended to trades.csv, sells matched to buys
(FIFO) for realized P&L. This file is the bot's performance record — without
it, weeks of paper trading teach nothing measurable.
"""

import csv
import io
from datetime import datetime
from pathlib import Path

from bot.atomic import atomic_write_text
from bot.broker import Fill

COLUMNS = ["order_id", "filled_at", "symbol", "underlying", "side", "qty", "price", "realized_pnl"]


def read_rows(path: str) -> list[dict]:
    file = Path(path)
    if not file.exists():
        return []
    with file.open(newline="") as f:
        return list(csv.DictReader(f))


def append_fills(path: str, fills: list[Fill]) -> int:
    """Append fills not yet journaled (by order id). Returns count added.

    Rewrites the whole file atomically (temp + os.replace) rather than
    opening in append mode: this is the only writer-safe option when more
    than one process can call append_fills (engine + dashboard), since a
    plain append can interleave with another process's read/write and an
    in-place append is never atomic across processes."""
    rows = read_rows(path)
    seen = {r["order_id"] for r in rows}
    new = [f for f in sorted(fills, key=lambda f: f.filled_at) if f.order_id not in seen]
    if not new:
        return 0

    for fill in new:
        realized = ""
        if fill.side == "sell":
            realized = _match_realized_pnl(rows, fill.symbol, fill.qty, fill.price)
        rows.append({
            "order_id": fill.order_id,
            "filled_at": fill.filled_at.isoformat(),
            "symbol": fill.symbol,
            "underlying": fill.underlying,
            "side": fill.side,
            "qty": fill.qty,
            "price": f"{fill.price:.4f}",
            "realized_pnl": realized,
        })

    buf = io.StringIO(newline="")
    writer = csv.DictWriter(buf, fieldnames=COLUMNS)
    writer.writeheader()
    writer.writerows(rows)
    atomic_write_text(path, buf.getvalue())
    return len(new)


def entry_time(path: str, symbol: str) -> datetime | None:
    """When the currently-open position in `symbol` was bought: the latest
    BUY fill with no SELL after it. None if unknown (e.g. pre-journal)."""
    last_buy: datetime | None = None
    for r in read_rows(path):
        if r["symbol"] != symbol:
            continue
        ts = datetime.fromisoformat(r["filled_at"])
        if r["side"] == "buy":
            last_buy = ts
        elif last_buy is not None and ts >= last_buy:
            last_buy = None  # position was closed after that buy
    return last_buy


def _open_buy_lots(rows: list[dict], symbol: str) -> list[list[float]]:
    """FIFO queue of [qty_remaining, price] for `symbol`'s buys not yet fully
    consumed by sells, oldest first. Quantity-aware so a partial sell only
    consumes part of a lot instead of retiring the whole row."""
    lots: list[list[float]] = []
    for r in rows:
        if r["symbol"] != symbol:
            continue
        qty = float(r["qty"])
        if r["side"] == "buy":
            lots.append([qty, float(r["price"])])
        elif r["side"] == "sell":
            remaining = qty
            while remaining > 1e-9 and lots:
                lot = lots[0]
                consumed = min(lot[0], remaining)
                lot[0] -= consumed
                remaining -= consumed
                if lot[0] <= 1e-9:
                    lots.pop(0)
    return lots


def _match_realized_pnl(rows: list[dict], symbol: str, qty: int, price: float) -> str:
    """Realized P&L for a sell of `qty` at `price`, FIFO-matched against open
    buy lots. Spans multiple lots (and their different entry prices) if the
    sell quantity exceeds the oldest lot's remaining quantity."""
    lots = _open_buy_lots(rows, symbol)
    remaining = float(qty)
    total = 0.0
    matched = False
    for lot_qty, lot_price in lots:
        if remaining <= 1e-9:
            break
        consumed = min(lot_qty, remaining)
        total += (price - lot_price) * 100 * consumed
        remaining -= consumed
        matched = True
    return f"{total:.2f}" if matched else ""
