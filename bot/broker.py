"""Alpaca paper-account wrapper. The ONLY module that talks to the network.

Everything returned to the engine is a broker-agnostic type (Contract,
OpenPosition, ClockInfo, OpenOrder, Fill), so the engine can run against a
fake in tests. DRY_RUN mode reads real data but logs orders instead of
submitting them.
"""

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from alpaca.data.enums import DataFeed
from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.requests import (
    OptionLatestQuoteRequest,
    StockBarsRequest,
    StockLatestTradeRequest,
)
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import (
    AssetStatus,
    ContractType,
    OrderSide,
    QueryOrderStatus,
    TimeInForce,
)
from alpaca.trading.requests import (
    GetCalendarRequest,
    GetOptionContractsRequest,
    GetOrdersRequest,
    LimitOrderRequest,
)

from bot.config import Settings
from bot.options import Contract
from bot.risk import OpenPosition

log = logging.getLogger("bot.broker")

# OCC option symbol: ROOT + YYMMDD + C/P + strike*1000 zero-padded to 8 digits
_OCC_TAIL = 15


@dataclass(frozen=True)
class ClockInfo:
    is_open: bool
    now: datetime
    next_open: datetime
    next_close: datetime


@dataclass(frozen=True)
class OpenOrder:
    id: str
    symbol: str          # OCC option symbol
    underlying: str
    side: str            # "buy" | "sell"
    submitted_at: datetime


@dataclass(frozen=True)
class Fill:
    order_id: str
    filled_at: datetime
    symbol: str          # OCC option symbol
    underlying: str
    side: str            # "buy" | "sell"
    qty: int
    price: float         # per-share fill price


def occ_underlying(occ_symbol: str) -> str:
    return occ_symbol[:-_OCC_TAIL].strip()


def occ_expiry(occ_symbol: str) -> date:
    raw = occ_symbol[-_OCC_TAIL:][:6]
    return datetime.strptime(raw, "%y%m%d").date()


def is_occ_symbol(symbol: str) -> bool:
    return len(symbol) > _OCC_TAIL


def round_tick(price: float) -> float:
    """Snap to a valid option tick: pennies under $3, nickels above."""
    if price < 3.0:
        return round(price, 2)
    return round(round(price / 0.05) * 0.05, 2)


class AlpacaBroker:
    def __init__(self, cfg: Settings):
        self.cfg = cfg
        self.trading = TradingClient(cfg.api_key, cfg.secret_key, paper=True)
        self.stock_data = StockHistoricalDataClient(cfg.api_key, cfg.secret_key)
        self.option_data = OptionHistoricalDataClient(cfg.api_key, cfg.secret_key)

    # --- account / clock ---

    def get_clock(self) -> ClockInfo:
        c = self.trading.get_clock()
        return ClockInfo(
            is_open=c.is_open,
            now=c.timestamp,
            next_open=c.next_open,
            next_close=c.next_close,
        )

    def session_open_time(self, day: date) -> datetime | None:
        """Today's official session open (needed for the skip-open window).

        Alpaca's calendar returns NAIVE datetimes in US/Eastern wall time,
        so the zone must be attached explicitly before converting."""
        cal = self.trading.get_calendar(GetCalendarRequest(start=day, end=day))
        if not cal:
            return None
        eastern = cal[0].open.replace(tzinfo=ZoneInfo("America/New_York"))
        return eastern.astimezone(timezone.utc)

    def get_equity(self) -> float:
        return float(self.trading.get_account().equity)

    def get_last_equity(self) -> float:
        """Equity at yesterday's close — the honest circuit-breaker anchor."""
        return float(self.trading.get_account().last_equity or 0.0)

    # --- positions ---

    def get_option_positions(self) -> list[OpenPosition]:
        """Open option positions, priced at the BID (the sellable price),
        falling back to Alpaca's mark when there is no live bid."""
        raw = [
            p for p in self.trading.get_all_positions()
            if getattr(p.asset_class, "value", str(p.asset_class)) == "us_option"
        ]
        if not raw:
            return []

        bids: dict[str, float] = {}
        try:
            quotes = self.option_data.get_option_latest_quote(
                OptionLatestQuoteRequest(symbol_or_symbols=[p.symbol for p in raw])
            )
            bids = {s: float(q.bid_price or 0.0) for s, q in quotes.items()}
        except Exception:
            log.warning("quote fetch for positions failed; using marks", exc_info=True)

        today = datetime.now(timezone.utc).date()
        out: list[OpenPosition] = []
        for p in raw:
            mark = float(p.current_price or 0.0)
            bid = bids.get(p.symbol, 0.0)
            out.append(
                OpenPosition(
                    symbol=p.symbol,
                    underlying=occ_underlying(p.symbol),
                    qty=int(float(p.qty)),
                    avg_entry_price=float(p.avg_entry_price),
                    current_price=bid if bid > 0 else mark,
                    days_to_expiry=(occ_expiry(p.symbol) - today).days,
                )
            )
        return out

    # --- orders ---

    def get_todays_option_orders(self, day_start: datetime) -> tuple[list[OpenOrder], list[Fill]]:
        """(still-open option orders, option fills) since day_start."""
        open_orders: list[OpenOrder] = []
        for o in self.trading.get_orders(
            GetOrdersRequest(status=QueryOrderStatus.OPEN, after=day_start)
        ):
            if not is_occ_symbol(o.symbol):
                continue
            open_orders.append(
                OpenOrder(
                    id=str(o.id),
                    symbol=o.symbol,
                    underlying=occ_underlying(o.symbol),
                    side=o.side.value,
                    submitted_at=o.submitted_at,
                )
            )

        fills: list[Fill] = []
        for o in self.trading.get_orders(
            GetOrdersRequest(status=QueryOrderStatus.CLOSED, after=day_start)
        ):
            if not is_occ_symbol(o.symbol) or not o.filled_at or float(o.filled_qty or 0) == 0:
                continue
            fills.append(
                Fill(
                    order_id=str(o.id),
                    filled_at=o.filled_at,
                    symbol=o.symbol,
                    underlying=occ_underlying(o.symbol),
                    side=o.side.value,
                    qty=int(float(o.filled_qty)),
                    price=float(o.filled_avg_price or 0.0),
                )
            )
        return open_orders, fills

    def cancel_order(self, order_id: str) -> None:
        if self.cfg.dry_run:
            log.info("DRY_RUN: would CANCEL order %s", order_id)
            return
        self.trading.cancel_order_by_id(order_id)
        log.info("canceled order %s", order_id)

    def buy_option(self, contract: Contract, qty: int) -> str:
        """Marketable limit: mid plus a small buffer, never above the ask.
        Beats a market order by refusing to pay the full spread."""
        limit = round_tick(min(contract.ask, contract.mid * (1 + self.cfg.limit_buffer_pct)))
        limit = min(limit, contract.ask)
        if self.cfg.dry_run:
            log.info("DRY_RUN: would BUY %d x %s limit %.2f (ask %.2f)",
                     qty, contract.symbol, limit, contract.ask)
            return "dry-run"
        order = self.trading.submit_order(
            LimitOrderRequest(
                symbol=contract.symbol,
                qty=qty,
                side=OrderSide.BUY,
                time_in_force=TimeInForce.DAY,
                limit_price=limit,
                client_order_id=self._client_order_id("buy", contract.symbol),
            )
        )
        log.info("submitted BUY %d x %s limit %.2f (order %s)",
                 qty, contract.symbol, limit, order.id)
        return str(order.id)

    def close_option(self, occ_symbol: str, qty: int, bid: float) -> None:
        """Sell with a limit at the bid (marketable). If there is no live bid,
        fall back to a market close rather than leave the position unmanaged."""
        if self.cfg.dry_run:
            log.info("DRY_RUN: would SELL %d x %s limit %.2f", qty, occ_symbol, bid)
            return
        if bid <= 0:
            log.warning("%s: no bid — closing at market", occ_symbol)
            self.trading.close_position(occ_symbol)
            return
        order = self.trading.submit_order(
            LimitOrderRequest(
                symbol=occ_symbol,
                qty=qty,
                side=OrderSide.SELL,
                time_in_force=TimeInForce.DAY,
                limit_price=round_tick(bid),
                client_order_id=self._client_order_id("sell", occ_symbol),
            )
        )
        log.info("submitted SELL %d x %s limit %.2f (order %s)",
                 qty, occ_symbol, round_tick(bid), order.id)

    @staticmethod
    def _client_order_id(side: str, occ_symbol: str) -> str:
        """Deterministic per (side, contract, day): a genuine accidental
        resubmit of the same intent (e.g. a retry after a timed-out response
        whose order actually went through) is rejected by Alpaca's
        client_order_id uniqueness constraint instead of silently opening a
        second position."""
        today = datetime.now(timezone.utc).date().isoformat()
        return f"{side}-{occ_symbol}-{today}"[:128]

    # --- market data ---

    def get_closes(self, symbol: str) -> list[float]:
        """Last `bar_history_count` CLOSED bars (drops the in-progress bar)."""
        tf_min = self.cfg.bar_timeframe_minutes
        start = datetime.now(timezone.utc) - timedelta(days=10)
        bars = self.stock_data.get_stock_bars(
            StockBarsRequest(
                symbol_or_symbols=symbol,
                timeframe=TimeFrame(tf_min, TimeFrameUnit.Minute),
                start=start,
                feed=DataFeed.IEX,
            )
        )
        series = bars.data.get(symbol, [])
        now = datetime.now(timezone.utc)
        closed = [
            b for b in series
            if b.timestamp + timedelta(minutes=tf_min) <= now
        ]
        return [float(b.close) for b in closed[-self.cfg.bar_history_count:]]

    def get_underlying_price(self, symbol: str) -> float:
        trades = self.stock_data.get_stock_latest_trade(
            StockLatestTradeRequest(symbol_or_symbols=symbol, feed=DataFeed.IEX)
        )
        return float(trades[symbol].price)

    def get_chain(self, underlying: str, want: str, today: date, px: float) -> list[Contract]:
        """Contracts of one type within the DTE window, strikes within +/-5%
        of spot (the bot only ever buys the first OTM strike), joined with
        their latest quotes."""
        req = GetOptionContractsRequest(
            underlying_symbols=[underlying],
            status=AssetStatus.ACTIVE,
            type=ContractType.CALL if want == "call" else ContractType.PUT,
            expiration_date_gte=today + timedelta(days=self.cfg.min_dte),
            expiration_date_lte=today + timedelta(days=self.cfg.max_dte),
            strike_price_gte=str(round(px * 0.95, 2)),
            strike_price_lte=str(round(px * 1.05, 2)),
            limit=500,
        )
        contracts = []
        page = self.trading.get_option_contracts(req)
        contracts.extend(page.option_contracts or [])
        while page.next_page_token:
            req.page_token = page.next_page_token
            page = self.trading.get_option_contracts(req)
            contracts.extend(page.option_contracts or [])

        symbols = [c.symbol for c in contracts if c.tradable]
        if not symbols:
            return []

        # The latest-quote endpoint rejects requests with >100 symbols.
        quotes: dict = {}
        for i in range(0, len(symbols), 100):
            quotes.update(
                self.option_data.get_option_latest_quote(
                    OptionLatestQuoteRequest(symbol_or_symbols=symbols[i : i + 100])
                )
            )
        out: list[Contract] = []
        for c in contracts:
            q = quotes.get(c.symbol)
            if q is None:
                continue
            out.append(
                Contract(
                    symbol=c.symbol,
                    underlying=underlying,
                    expiry=c.expiration_date,
                    strike=float(c.strike_price),
                    call_put=want,
                    bid=float(q.bid_price or 0.0),
                    ask=float(q.ask_price or 0.0),
                    open_interest=int(float(c.open_interest or 0)),
                )
            )
        return out
