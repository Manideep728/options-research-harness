"""Hard risk gates. Pure logic — all account/position data passed in."""

from dataclasses import dataclass

from bot.config import Settings


@dataclass(frozen=True)
class OpenPosition:
    """Broker-agnostic view of an open option position.

    `is_credit` marks a position that was SOLD to open. Its profit and loss run
    the opposite way — the position gains when the price falls — and
    `max_loss_per_share` is then the width of the spread less the credit rather
    than the premium. Both default to the long case, so every existing caller
    keeps its current meaning.
    """

    symbol: str            # OCC option symbol
    underlying: str
    qty: int
    avg_entry_price: float  # per-share premium paid, or credit received
    current_price: float    # per-share current value of the position
    days_to_expiry: int
    is_credit: bool = False
    max_loss_per_share: float | None = None

    @property
    def capital_at_risk(self) -> float:
        """Per share. For a long option the premium is all you can lose. A
        credit position must state its own, because the credit received is not
        the risk and using it would understate the position by several times.
        """
        if not self.is_credit:
            return self.avg_entry_price
        return self.max_loss_per_share or 0.0

    @property
    def pnl_pct(self) -> float:
        """Profit as a fraction of the capital at risk."""
        risk = self.capital_at_risk
        if risk <= 0:
            return 0.0
        moved = (self.avg_entry_price - self.current_price if self.is_credit
                 else self.current_price - self.avg_entry_price)
        return moved / risk


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


def size_allowed(premium_per_share: float, equity: float, cfg: Settings,
                 max_loss_per_share: float | None = None) -> GateResult:
    """One contract = 100 shares. Reject if it risks too much equity.

    The gate is on the MAXIMUM LOSS, not on the cash paid. For a long option
    the two are the same number, so nothing changes for existing callers. For a
    position sold to open they are not: a spread might collect 50 dollars and
    still lose 450, and sizing on the credit would let the bot open nine times
    the intended risk while every check reported it as within limits.
    """
    risk_per_share = (premium_per_share if max_loss_per_share is None
                      else max_loss_per_share)
    risk = risk_per_share * 100
    limit = equity * cfg.max_premium_pct_of_equity
    if risk > limit:
        return GateResult(
            False,
            f"max loss ${risk:.2f} exceeds "
            f"{cfg.max_premium_pct_of_equity:.0%} of equity (${limit:.2f})",
        )
    return GateResult(True, f"max loss ${risk:.2f} within ${limit:.2f} limit")


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
