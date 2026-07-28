"""Hard risk gates. Pure logic — all account/position data passed in."""

from dataclasses import dataclass

from bot.config import Settings


@dataclass(frozen=True)
class OpenPosition:
    """Broker-agnostic view of an open option position."""

    symbol: str            # OCC option symbol
    underlying: str
    qty: int
    avg_entry_price: float  # per-share premium paid
    current_price: float    # per-share current premium
    days_to_expiry: int

    @property
    def pnl_pct(self) -> float:
        if self.avg_entry_price == 0:
            return 0.0
        return (self.current_price - self.avg_entry_price) / self.avg_entry_price


@dataclass(frozen=True)
class GateResult:
    allowed: bool
    reason: str


def entry_allowed(
    underlying: str,
    positions: list[OpenPosition],
    trades_today: int,
    equity: float,
    day_start_equity: float,
    cfg: Settings,
) -> GateResult:
    """All gates must pass before we even look at the option chain."""
    if trades_today >= cfg.max_trades_per_day:
        return GateResult(
            False, f"daily trade limit reached ({trades_today}/{cfg.max_trades_per_day})"
        )

    if len(positions) >= cfg.max_positions:
        return GateResult(
            False, f"max concurrent positions reached ({len(positions)}/{cfg.max_positions})"
        )

    held = sum(1 for p in positions if p.underlying == underlying)
    if held >= cfg.max_positions_per_underlying:
        return GateResult(False, f"already holding {held} position(s) in {underlying}")

    if day_start_equity > 0:
        day_pnl_pct = (equity - day_start_equity) / day_start_equity
        if day_pnl_pct <= -cfg.daily_loss_limit_pct:
            return GateResult(
                False,
                f"circuit breaker: day P&L {day_pnl_pct:+.2%} <= -{cfg.daily_loss_limit_pct:.0%}",
            )

    return GateResult(True, "all entry gates passed")


def size_allowed(premium_per_share: float, equity: float, cfg: Settings) -> GateResult:
    """One contract = 100 shares of premium. Reject if it risks too much equity."""
    cost = premium_per_share * 100
    limit = equity * cfg.max_premium_pct_of_equity
    if cost > limit:
        return GateResult(
            False,
            f"premium ${cost:.2f} exceeds "
            f"{cfg.max_premium_pct_of_equity:.0%} of equity (${limit:.2f})",
        )
    return GateResult(True, f"premium ${cost:.2f} within ${limit:.2f} limit")


def exit_reason(
    position: OpenPosition, cfg: Settings, held_days: float | None = None
) -> str | None:
    """Return why the position must be closed, or None to keep holding.

    `held_days` comes from the trade journal; None means the entry time is
    unknown (pre-journal position), which skips only the max-hold check."""
    if position.pnl_pct >= cfg.take_profit_pct:
        return f"take profit: {position.pnl_pct:+.1%} >= +{cfg.take_profit_pct:.0%}"
    if position.pnl_pct <= -cfg.stop_loss_pct:
        return f"stop loss: {position.pnl_pct:+.1%} <= -{cfg.stop_loss_pct:.0%}"
    if position.days_to_expiry <= cfg.time_stop_dte:
        return f"time stop: {position.days_to_expiry} DTE <= {cfg.time_stop_dte}"
    if held_days is not None and held_days >= cfg.max_hold_days:
        return f"max hold: {held_days:.1f} days >= {cfg.max_hold_days:.1f}"
    return None
