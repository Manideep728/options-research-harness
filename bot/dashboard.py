"""Build live dashboard snapshots from the existing bot surfaces."""

from dataclasses import asdict, dataclass
from datetime import datetime, timezone

from bot import control, journal, risk, state, watchlist
from bot.broker import Fill
from bot.config import Settings
from bot.options import pick_contract
from bot.strategy import Action, evaluate


@dataclass(frozen=True)
class ContractSnapshot:
    symbol: str
    strike: float
    expiry: str
    bid: float
    ask: float
    open_interest: int


@dataclass(frozen=True)
class SymbolSnapshot:
    symbol: str
    action: str
    reason: str
    trend: str
    signal_ready: bool
    gate_allowed: bool
    gate_reason: str
    underlying_price: float | None = None
    contract: ContractSnapshot | None = None
    size_allowed: bool | None = None
    size_reason: str | None = None
    rejections: list[dict[str, str]] | None = None
    score: float | None = None          # rank score from the last scan
    rank_detail: str | None = None      # human-readable score breakdown


@dataclass(frozen=True)
class PositionSnapshot:
    symbol: str
    underlying: str
    qty: int
    avg_entry_price: float
    current_price: float
    pnl_pct: float
    days_to_expiry: int
    entry_time: str | None
    exit_reason: str | None


@dataclass(frozen=True)
class OrderSnapshot:
    id: str
    symbol: str
    underlying: str
    side: str
    submitted_at: str
    age_sec: float


@dataclass(frozen=True)
class FillSnapshot:
    order_id: str
    filled_at: str
    symbol: str
    underlying: str
    side: str
    qty: int
    price: float
    realized_pnl: str


@dataclass(frozen=True)
class DashboardSnapshot:
    generated_at: str
    market: dict
    bot: dict
    day: dict
    journal: dict
    control: dict
    symbols: list[SymbolSnapshot]
    positions: list[PositionSnapshot]
    open_orders: list[OrderSnapshot]
    fills: list[FillSnapshot]
    recent_activity: list[dict]


def build_dashboard_snapshot(broker, cfg: Settings) -> DashboardSnapshot:
    clock = broker.get_clock()
    open_orders, fills = broker.get_todays_option_orders(_day_start(clock.now))
    journal.append_fills(cfg.journal_file, fills)
    control_state = control.load_control(cfg.control_file)

    positions = broker.get_option_positions()
    equity = broker.get_equity()
    last_equity = broker.get_last_equity() or equity
    day_state = state.load_day_state(cfg.state_file, clock.now.date(), last_equity)
    open_buys = [o for o in open_orders if o.side == "buy"]
    broker_trade_count = sum(1 for fill in fills if fill.side == "buy") + len(open_buys)
    trades_today = max(day_state.trades_today, broker_trade_count)

    gate_positions = list(positions) + [
        risk.OpenPosition(
            symbol=o.symbol,
            underlying=o.underlying,
            qty=1,
            avg_entry_price=0.0,
            current_price=0.0,
            days_to_expiry=99,
        )
        for o in open_buys
    ]

    # Only snapshot the engine's active shortlist — not all 30 names — so a
    # dashboard refresh makes a handful of data calls, not one per symbol.
    shortlist = watchlist.load_active(cfg.active_file)

    symbol_snapshots: list[SymbolSnapshot] = []
    recent_activity: list[dict] = []
    for entry in shortlist.entries:
        symbol = entry.symbol
        closes = broker.get_closes(symbol)
        signal = evaluate(closes, cfg)
        trend = "up" if signal.action == Action.BUY_CALL else "down" if signal.action == Action.BUY_PUT else "flat"
        gate = risk.entry_allowed(symbol, gate_positions, trades_today, equity, day_state.day_start_equity or last_equity, cfg)
        underlying_price: float | None = None
        contract_snapshot: ContractSnapshot | None = None
        size_allowed: bool | None = None
        size_reason: str | None = None
        rejections: list[dict[str, str]] | None = None

        if signal.action != Action.NONE and gate.allowed:
            underlying_price = broker.get_underlying_price(symbol)
            want = "call" if signal.action == Action.BUY_CALL else "put"
            chain = broker.get_chain(symbol, want, clock.now.date(), underlying_price)
            contract, rejects = pick_contract(chain, signal.action, underlying_price, clock.now.date(), cfg)
            rejections = [asdict(r) for r in rejects[:5]]
            if contract is not None:
                contract_snapshot = ContractSnapshot(
                    symbol=contract.symbol,
                    strike=contract.strike,
                    expiry=contract.expiry.isoformat(),
                    bid=contract.bid,
                    ask=contract.ask,
                    open_interest=contract.open_interest,
                )
                size = risk.size_allowed(contract.ask, equity, cfg)
                size_allowed = size.allowed
                size_reason = size.reason

        symbol_snapshots.append(
            SymbolSnapshot(
                symbol=symbol,
                action=signal.action.value,
                reason=signal.reason,
                trend=trend,
                signal_ready=signal.action != Action.NONE,
                gate_allowed=gate.allowed,
                gate_reason=gate.reason,
                underlying_price=underlying_price,
                contract=contract_snapshot,
                size_allowed=size_allowed,
                size_reason=size_reason,
                rejections=rejections,
                score=entry.score,
                rank_detail=entry.detail,
            )
        )
        recent_activity.append(
            {
                "kind": "signal",
                "symbol": symbol,
                "title": signal.action.value,
                "detail": signal.reason,
            }
        )

    position_snapshots = _build_position_snapshots(positions, cfg)
    fill_snapshots = [
        FillSnapshot(
            order_id=fill.order_id,
            filled_at=fill.filled_at.isoformat(),
            symbol=fill.symbol,
            underlying=fill.underlying,
            side=fill.side,
            qty=fill.qty,
            price=fill.price,
            realized_pnl=_realized_pnl_for_fill(fill, cfg.journal_file),
        )
        for fill in fills
    ]
    order_snapshots = [
        OrderSnapshot(
            id=o.id,
            symbol=o.symbol,
            underlying=o.underlying,
            side=o.side,
            submitted_at=o.submitted_at.isoformat(),
            age_sec=max(0.0, (clock.now - o.submitted_at).total_seconds()),
        )
        for o in open_orders
    ]

    journal_rows = journal.read_rows(cfg.journal_file)
    realized_pnl = 0.0
    wins = 0
    losses = 0
    for row in journal_rows:
        value = row.get("realized_pnl", "")
        if not value:
            continue
        try:
            pnl = float(value)
        except ValueError:
            continue
        realized_pnl += pnl
        if pnl > 0:
            wins += 1
        elif pnl < 0:
            losses += 1

    recent_activity.extend(
        {
            "kind": "fill",
            "symbol": fill.symbol,
            "title": f"{fill.side.upper()} {fill.qty}",
            "detail": f"{fill.price:.2f} @ {fill.filled_at.isoformat()}",
        }
        for fill in fills[-5:]
    )

    return DashboardSnapshot(
        generated_at=datetime.now(timezone.utc).isoformat(),
        market={
            "is_open": clock.is_open,
            "now": clock.now.isoformat(),
            "next_open": clock.next_open.isoformat(),
            "next_close": clock.next_close.isoformat(),
        },
        bot={
            "dry_run": cfg.dry_run,
            "loop_interval_sec": cfg.loop_interval_sec,
            "symbols": list(cfg.symbols),          # full universe (metadata)
            "active_symbols": [e.symbol for e in shortlist.entries],
            "ranked_at": shortlist.ranked_at,      # when the shortlist was last built
            "paused": control_state.paused,
        },
        day={
            "day": day_state.day,
            "trades_today": trades_today,
            "day_start_equity": day_state.day_start_equity,
            "equity": equity,
            "day_pnl": equity - day_state.day_start_equity,
            "day_pnl_pct": _pct(equity - day_state.day_start_equity, day_state.day_start_equity),
        },
        journal={
            "fills": len(journal_rows),
            "realized_pnl": realized_pnl,
            "wins": wins,
            "losses": losses,
        },
        control={
            "paused": control_state.paused,
            "updated_at": control_state.updated_at,
        },
        symbols=symbol_snapshots,
        positions=position_snapshots,
        open_orders=order_snapshots,
        fills=fill_snapshots,
        recent_activity=recent_activity[-12:],
    )


def _build_position_snapshots(positions, cfg: Settings) -> list[PositionSnapshot]:
    out: list[PositionSnapshot] = []
    for position in positions:
        entry_time = journal.entry_time(cfg.journal_file, position.symbol)
        out.append(
            PositionSnapshot(
                symbol=position.symbol,
                underlying=position.underlying,
                qty=position.qty,
                avg_entry_price=position.avg_entry_price,
                current_price=position.current_price,
                pnl_pct=position.pnl_pct,
                days_to_expiry=position.days_to_expiry,
                entry_time=entry_time.isoformat() if entry_time else None,
                exit_reason=_exit_reason(position, cfg, entry_time),
            )
        )
    return out


def _exit_reason(position: risk.OpenPosition, cfg: Settings, entry_time: datetime | None) -> str | None:
    held_days = None
    if entry_time is not None:
        held_days = (datetime.now(timezone.utc) - entry_time).total_seconds() / 86400
    return risk.exit_reason(position, cfg, held_days)


def _realized_pnl_for_fill(fill: Fill, journal_path: str) -> str:
    rows = journal.read_rows(journal_path)
    for row in rows:
        if row.get("order_id") == fill.order_id:
            return row.get("realized_pnl", "")
    return ""


def _pct(delta: float, base: float) -> float:
    if base == 0:
        return 0.0
    return delta / base


def _day_start(now: datetime) -> datetime:
    return datetime.combine(now.date(), datetime.min.time(), tzinfo=timezone.utc)