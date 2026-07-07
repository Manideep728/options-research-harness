"""Backtest simulator: replays the live signal rules over historical bars and
models option P&L with an explicit, documented approximation.

Option model (crude but directionally honest):
  option_return ≈ underlying_move% * delta / premium_pct   (gearing)
                  - theta_daily * days_held                (time decay)
                  - roundtrip_cost                         (spread + slippage)
capped below at -100% (long options cannot lose more than the premium).
Constants live in SimParams so the approximation is visible and testable.
"""

from dataclasses import dataclass, field
from datetime import datetime

from bot.config import Settings
from bot.indicators import ema, rsi
from bot.strategy import Action


@dataclass(frozen=True)
class SimParams:
    delta: float = 0.40              # first-OTM 7-14 DTE contract, roughly
    premium_pct_of_spot: float = 0.005
    theta_daily: float = 0.05        # premium decay per calendar day held
    roundtrip_cost: float = 0.03     # spread paid entering + exiting


@dataclass(frozen=True)
class SimTrade:
    entry_time: datetime
    exit_time: datetime
    direction: str      # "call" | "put"
    entry_px: float
    exit_px: float
    pnl_pct: float      # of premium, net of costs
    reason: str


@dataclass
class SimResult:
    trades: list[SimTrade] = field(default_factory=list)

    @property
    def n(self) -> int:
        return len(self.trades)

    @property
    def win_rate(self) -> float:
        return sum(1 for t in self.trades if t.pnl_pct > 0) / self.n if self.n else 0.0

    @property
    def expectancy(self) -> float:
        """Mean P&L per trade as a fraction of premium — the score."""
        return sum(t.pnl_pct for t in self.trades) / self.n if self.n else 0.0

    @property
    def profit_factor(self) -> float:
        gains = sum(t.pnl_pct for t in self.trades if t.pnl_pct > 0)
        losses = -sum(t.pnl_pct for t in self.trades if t.pnl_pct < 0)
        return gains / losses if losses > 0 else float("inf") if gains > 0 else 0.0

    @property
    def max_drawdown(self) -> float:
        """Worst peak-to-trough dip of cumulative P&L (premium units)."""
        peak = cum = 0.0
        worst = 0.0
        for t in self.trades:
            cum += t.pnl_pct
            peak = max(peak, cum)
            worst = max(worst, peak - cum)
        return worst


def signal_series(closes: list[float], cfg: Settings) -> list[Action]:
    """Per-bar signal, identical to strategy.evaluate() at every prefix but
    computed in O(n) total instead of O(n^2) (indicators are causal)."""
    n = len(closes)
    out = [Action.NONE] * n
    needed = max(cfg.ema_slow, cfg.rsi_period + 1) + 1
    if n < needed:
        return out

    ema_f = ema(closes, cfg.ema_fast)
    ema_s = ema(closes, cfg.ema_slow)
    rsi_s = rsi(closes, cfg.rsi_period)
    for i in range(needed - 1, n):
        uptrend = ema_f[i] > ema_s[i]
        rsi_prev, rsi_now = rsi_s[i - 1], rsi_s[i]
        if uptrend and rsi_prev < cfg.rsi_bull_level <= rsi_now:
            out[i] = Action.BUY_CALL
        elif not uptrend and rsi_prev > cfg.rsi_bear_level >= rsi_now:
            out[i] = Action.BUY_PUT
    return out


def simulate(
    closes: list[float],
    times: list[datetime],
    cfg: Settings,
    sp: SimParams = SimParams(),
) -> SimResult:
    """One position at a time (mirrors the live one-per-underlying cap)."""
    result = SimResult()
    signals = signal_series(closes, cfg)
    gearing = sp.delta / sp.premium_pct_of_spot

    i = 0
    n = len(closes)
    while i < n:
        if signals[i] == Action.NONE:
            i += 1
            continue

        direction = 1.0 if signals[i] == Action.BUY_CALL else -1.0
        entry_px, entry_t = closes[i], times[i]
        exit_reason = "end of data"
        exit_j = n - 1
        pnl = _option_ret(closes[n - 1], entry_px, direction,
                          (times[n - 1] - entry_t).total_seconds() / 86400, gearing, sp)

        for j in range(i + 1, n):
            days = (times[j] - entry_t).total_seconds() / 86400
            ret = _option_ret(closes[j], entry_px, direction, days, gearing, sp)
            if ret >= cfg.take_profit_pct:
                exit_reason, exit_j, pnl = "take profit", j, ret
                break
            if ret <= -cfg.stop_loss_pct:
                exit_reason, exit_j, pnl = "stop loss", j, ret
                break
            if days >= cfg.max_hold_days:
                exit_reason, exit_j, pnl = "max hold", j, ret
                break

        result.trades.append(
            SimTrade(
                entry_time=entry_t,
                exit_time=times[exit_j],
                direction="call" if direction > 0 else "put",
                entry_px=entry_px,
                exit_px=closes[exit_j],
                pnl_pct=pnl,
                reason=exit_reason,
            )
        )
        i = exit_j + 1  # flat again; scan for the next signal

    return result


def _option_ret(px, entry_px, direction, days, gearing, sp: SimParams) -> float:
    move = direction * (px - entry_px) / entry_px
    ret = move * gearing - sp.theta_daily * days - sp.roundtrip_cost
    return max(ret, -1.0)
