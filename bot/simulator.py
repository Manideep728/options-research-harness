"""Backtest simulator: replays the live signal rules over historical bars and
models option P&L with an explicit, documented approximation.

Option model (crude but directionally honest):
  option_return ≈ underlying_move% * delta / premium_pct   (gearing)
                  - theta_daily * days_held                (time decay)
                  - roundtrip_cost                         (spread + slippage)
capped below at -100% (long options cannot lose more than the premium).
Constants live in SimParams so the approximation is visible and testable.

Exit accounting — read this before trusting any number out of here. The
gearing is delta/premium_pct = 80x, so take_profit_pct=0.50 is reached by a
+0.625% move in the underlying and stop_loss_pct=0.25 by -0.31%. Bars are
much bigger than that, so a bar that trips a barrier has almost always blown
straight past it, and the size of that overshoot is a property of the bar
size, not of the strategy. Booking the overshoot is therefore wrong twice
over: it is arbitrary, and because losses are floored at -100% while gains
are unbounded, it manufactures positive expectancy out of pure volatility (a
coin-flip signal on daily bars scored +11% per trade before this was fixed).

So: a barrier tripped WITHIN a session books the barrier level, because the
live engine polls the option every loop_interval_sec (30s) and really does
exit at approximately the barrier. A barrier tripped ACROSS a session gap
books the full move, because the engine is not running and cannot act.

Consequence worth stating plainly: on DAILY bars every step is a session gap,
so nothing here is fixed for them — the linear-delta model is simply not
valid for moves that large, and research/null.py exists to keep proving it.
"""

from dataclasses import dataclass, field
from datetime import datetime

from bot.config import Settings
from bot.indicators import ema, rsi
from bot.strategy import Action


@dataclass(frozen=True)
class SimParams:
    """The whole option-pricing approximation, in four numbers.

    Known limitations, none of them hidden:
      - delta is constant, so there is no gamma and no convexity. A first-OTM
        contract also has to travel to its strike before it gains intrinsic
        value, which this model does not charge for — so large favourable
        moves are priced too generously.
      - theta is flat per calendar day regardless of DTE, which is wrong for
        the 7-14 DTE window the live bot actually trades.
      - roundtrip_cost=0.03 is optimistic against the live liquidity gate:
        max_spread_pct_of_mid=0.10 permits paying ~mid+1% on entry and selling
        at the bid on exit, i.e. ~6% roundtrip in the worst permitted case.
      - close-only bars mean intra-bar path is unknown; see the module
        docstring for how barrier exits are booked and why.
    """

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
    symbol: str = ""    # set when the caller simulates one known underlying


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
    signal_fn=None,
    symbol: str = "",
) -> SimResult:
    """One position at a time (mirrors the live one-per-underlying cap).

    `signal_fn(closes) -> list[Action]` overrides the built-in live signal so
    the research loop can simulate alternative strategy families through this
    same P&L engine. It must be causal: signals[i] may only use closes[:i+1].
    Default (None) is the live EMA+RSI logic, unchanged."""
    result = SimResult()
    signals = signal_fn(closes) if signal_fn is not None else signal_series(closes, cfg)
    gearing = gearing_of(sp)

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
        pnl = option_return(closes[n - 1], entry_px, direction,
                          (times[n - 1] - entry_t).total_seconds() / 86400, gearing, sp)

        for j in range(i + 1, n):
            days = (times[j] - entry_t).total_seconds() / 86400
            ret = option_return(closes[j], entry_px, direction, days, gearing, sp)
            # A US session never straddles midnight UTC (09:30-16:00 ET is
            # 13:30-20:00 UTC), so a UTC date change between adjacent bars is
            # exactly "the engine was not running in between".
            gapped = times[j].date() != times[j - 1].date()
            if ret >= cfg.take_profit_pct:
                exit_reason, exit_j = "take profit", j
                pnl = ret if gapped else cfg.take_profit_pct
                break
            if ret <= -cfg.stop_loss_pct:
                exit_reason, exit_j = "stop loss", j
                pnl = ret if gapped else -cfg.stop_loss_pct
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
                symbol=symbol,
            )
        )
        i = exit_j + 1  # flat again; scan for the next signal

    return result


def gearing_of(sp: SimParams) -> float:
    """Option return per 1.0 of underlying return. ~80x at the defaults."""
    return sp.delta / sp.premium_pct_of_spot


def option_return(px: float, entry_px: float, direction: float, days: float,
                  gearing: float, sp: SimParams) -> float:
    """The single P&L model. Public because research/replay.py reprices open
    positions with it — one model with two callers, never two models."""
    move = direction * (px - entry_px) / entry_px
    ret = move * gearing - sp.theta_daily * days - sp.roundtrip_cost
    return max(ret, -1.0)
