"""Trade journal: every fill appended to trades.csv, sells matched to buys
(FIFO) for realized P&L. This file is the bot's performance record — without
it, weeks of paper trading teach nothing measurable.
"""

import csv
from datetime import datetime
from pathlib import Path

from bot.broker import Fill

COLUMNS = ["order_id", "filled_at", "symbol", "underlying", "side", "qty", "price", "realized_pnl"]


def read_rows(path: str) -> list[dict]:
    file = Path(path)
    if not file.exists():
        return []
    with file.open(newline="") as f:
        return list(csv.DictReader(f))


def append_fills(path: str, fills: list[Fill]) -> int:
    """Append fills not yet journaled (by order id). Returns count added."""
    rows = read_rows(path)
    seen = {r["order_id"] for r in rows}
    new = [f for f in sorted(fills, key=lambda f: f.filled_at) if f.order_id not in seen]
    if not new:
        return 0

    file = Path(path)
    write_header = not file.exists()
    with file.open("a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS)
        if write_header:
            writer.writeheader()
        for fill in new:
            realized = ""
            if fill.side == "sell":
                buy_price = _match_buy_price(rows, fill.symbol)
                if buy_price is not None:
                    realized = f"{(fill.price - buy_price) * 100 * fill.qty:.2f}"
            row = {
                "order_id": fill.order_id,
                "filled_at": fill.filled_at.isoformat(),
                "symbol": fill.symbol,
                "underlying": fill.underlying,
                "side": fill.side,
                "qty": fill.qty,
                "price": f"{fill.price:.4f}",
                "realized_pnl": realized,
            }
            writer.writerow(row)
            rows.append(row)
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


def _match_buy_price(rows: list[dict], symbol: str) -> float | None:
    """FIFO: price of the oldest BUY of `symbol` not yet consumed by a sell."""
    buys = [float(r["price"]) for r in rows if r["symbol"] == symbol and r["side"] == "buy"]
    sells = sum(1 for r in rows if r["symbol"] == symbol and r["side"] == "sell")
    if sells < len(buys):
        return buys[sells]
    return None
