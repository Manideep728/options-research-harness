"""Backtest simulator: replays the live signal rules over historical bars and
prices the option with Black-Scholes.

P&L model: bot/pricing.py values the contract at entry and again at exit, and
the return is the ratio of the two after the spread is paid on both legs.
Delta, premium and decay are outputs of that model, not constants — so they
change as the underlying moves and as expiry approaches.

This replaced a linear approximation, `underlying_move * delta / premium_pct`,
whose constants made a first-OTM contract cost 0.5% of spot when the real price
is about 0.93%. That put the gearing at 80x instead of ~43x and made every
return roughly twice too large. See bot/pricing.py for the other two defects.

Exit accounting — read this before trusting any number out of here. The
barriers are close together in underlying terms: take_profit_pct=0.50 needs
about a +1% move and stop_loss_pct=0.25 about -0.65%. Bars can cover that
distance, so where price went WITHIN a bar decides which barrier was hit, and
the close alone cannot say.

So the exit rules are, in order:
  - Across a session gap the engine was not polling, and the first price it can
    act on is the next bar's OPEN. A barrier already breached there books the
    open's return, because that loss or gain was not avoidable.
  - Within a session the engine polls every loop_interval_sec (30s) and really
    does exit at approximately the barrier, so the barrier level is booked.
  - When one bar's range touches BOTH barriers, OHLC cannot say which came
    first. The stop is assumed to have come first. That is the only choice
    that cannot flatter the result.

Booking the overshoot past a barrier instead of the barrier itself was the
original defect: it is arbitrary, and because losses floor at -100% while gains
do not, it manufactured positive expectancy out of pure volatility. A coin-flip
signal on daily bars scored +11% per trade before that was fixed.

Without high and low columns every extreme falls back to the close, which
reproduces the close-only behaviour exactly. research/data.py flags such a
cache with has_intrabar=False so the degradation is visible rather than silent.
"""

from dataclasses import dataclass, field
from datetime import datetime

from bot import pricing
from bot.config import Settings
from bot.indicators import ema, rsi
from bot.strategy import Action


@dataclass(frozen=True)
class SimParams:
    """The contract the backtest trades, and the cost of trading it.

    Delta, premium and theta are no longer inputs. bot/pricing.py derives all
    three from these, so they move as the underlying moves and as expiry
    approaches, instead of staying fixed at the values a 7-14 DTE contract has
    on the day it is opened.

    Known limitations, none of them hidden:
      - one implied volatility serves every strike, so there is no smile and no
        term structure. `iv` is a fallback: simulate() prefers a real per-bar
        implied volatility series when the caller has one.
      - `iv` is the volatility the option is PRICED at, and the same value is
        used at entry and exit. A backtest therefore never earns or loses from
        a change in implied volatility, only from spot and from decay.
      - roundtrip_cost=0.03 is optimistic against the live liquidity gate:
        max_spread_pct_of_mid=0.10 permits paying ~mid+1% on entry and selling
        at the bid on exit, i.e. ~6% roundtrip in the worst permitted case.
      - European pricing on American contracts. Close enough on liquid ETFs
        with no dividend in the holding window, wrong through an ex-dividend
        date.
    """

    iv: float = 0.20                 # annualized implied volatility, fallback
    otm_pct: float = 0.01            # how far out of the money the strike sits
    dte_days: float = 10.0           # days to expiry when the trade is opened
    rate: float = pricing.DEFAULT_RATE
    roundtrip_cost: float = 0.03     # spread paid entering + exiting
    # Distance from the short strike to the protective one, as a fraction of
    # spot. Only a credit spread uses it. It sets the maximum loss, so it is
    # the single most important risk number for a short-premium strategy.
    spread_width_pct: float = 0.05


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
    highs: list[float] | None = None,
    lows: list[float] | None = None,
    opens: list[float] | None = None,
    ivs: list[float] | None = None,
) -> SimResult:
    """One position at a time (mirrors the live one-per-underlying cap).

    `signal_fn(closes) -> list[Action]` overrides the built-in live signal so
    the research loop can simulate alternative strategy families through this
    same P&L engine. It must be causal: signals[i] may only use closes[:i+1].
    Default (None) is the live EMA+RSI logic, unchanged.

    `highs`, `lows` and `opens` let the barrier test use where price actually
    went inside a bar. Omit them and every one falls back to the close, which
    reproduces the close-only behaviour exactly — so a cache without those
    columns degrades rather than lying.

    `ivs` is a per-bar implied volatility for the entry bar of each trade. Omit
    it and every trade is priced at SimParams.iv.
    """
    result = SimResult()
    signals = signal_fn(closes) if signal_fn is not None else signal_series(closes, cfg)
    highs = closes if highs is None else highs
    lows = closes if lows is None else lows
    opens = closes if opens is None else opens

    i = 0
    n = len(closes)
    while i < n:
        if signals[i] == Action.NONE:
            i += 1
            continue

        direction = 1.0 if signals[i] == Action.BUY_CALL else -1.0
        entry_px, entry_t = closes[i], times[i]
        iv = None if ivs is None else ivs[i]
        exit_reason = "end of data"
        exit_j = n - 1
        pnl = option_return(closes[n - 1], entry_px, direction,
                            (times[n - 1] - entry_t).total_seconds() / 86400, sp, iv)

        for j in range(i + 1, n):
            days = (times[j] - entry_t).total_seconds() / 86400
            # A US session never straddles midnight UTC (09:30-16:00 ET is
            # 13:30-20:00 UTC), so a UTC date change between adjacent bars is
            # exactly "the engine was not running in between". The first price
            # it can act on is this bar's OPEN, so a gap through a barrier is
            # booked there — not at the close, which would credit the rest of
            # the day's move to a position the engine would already have shut.
            if times[j].date() != times[j - 1].date():
                open_ret = option_return(opens[j], entry_px, direction, days, sp, iv)
                if open_ret >= cfg.take_profit_pct:
                    exit_reason, exit_j, pnl = "take profit", j, open_ret
                    break
                if open_ret <= -cfg.stop_loss_pct:
                    exit_reason, exit_j, pnl = "stop loss", j, open_ret
                    break

            # Within a session the engine polls every loop_interval_sec, so it
            # exits AT the barrier. Test the adverse extreme first: when one bar
            # touches both barriers, OHLC cannot say which came first, and
            # assuming the loss is the only choice that cannot flatter.
            adverse = lows[j] if direction > 0 else highs[j]
            favourable = highs[j] if direction > 0 else lows[j]
            if option_return(adverse, entry_px, direction, days, sp,
                             iv) <= -cfg.stop_loss_pct:
                exit_reason, exit_j, pnl = "stop loss", j, -cfg.stop_loss_pct
                break
            if option_return(favourable, entry_px, direction, days, sp,
                             iv) >= cfg.take_profit_pct:
                exit_reason, exit_j, pnl = "take profit", j, cfg.take_profit_pct
                break
            if days >= cfg.max_hold_days:
                exit_reason, exit_j, pnl = "max hold", j, option_return(
                    closes[j], entry_px, direction, days, sp, iv)
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


@dataclass(frozen=True)
class OpenStructure:
    """What a trade actually holds: a long option, or a credit spread.

    A NAKED short option is deliberately not representable. Its loss is
    unbounded for a call and strike-sized for a put, and a backtest that can
    express a position the risk rules must never allow will eventually report a
    result that depends on taking it. Every short here is defined-risk.

    `max_loss` is the capital genuinely at risk per share, and every return is
    quoted against it. For a long option that is the premium paid, so the
    existing meaning of pnl_pct is unchanged. For a credit spread it is the
    width less the credit, which is the number position sizing has to use — a
    return quoted against the credit alone would call a 20 dollar risk a 100
    percent gain on a 5 dollar credit.
    """

    call_put: str
    short_strike: float
    long_strike: float | None     # None for a long option
    is_credit: bool
    entry_value: float            # cash actually paid, or actually received
    max_loss: float               # per share; > 0

    @property
    def max_gain(self) -> float:
        """Best possible return on risk. A credit spread cannot earn more than
        the credit it collected, so a take-profit above this can never trigger
        — see unreachable_take_profit()."""
        if self.max_loss <= 0.0:
            return 0.0
        return (self.entry_value if self.is_credit else float("inf")) / self.max_loss

    def value_at(self, spot: float, days: float, sp: SimParams,
                 iv: float | None = None) -> float:
        """Mid value of the structure now, per share."""
        years = max(0.0, (sp.dte_days - days) / 365.0)
        vol = sp.iv if iv is None else iv
        if self.long_strike is None:
            return pricing.price(spot, self.short_strike, years, vol,
                                 self.call_put, sp.rate)
        return pricing.spread_value(spot, self.short_strike, self.long_strike,
                                    years, vol, self.call_put, sp.rate)

    def return_at(self, spot: float, days: float, sp: SimParams,
                  iv: float | None = None) -> float:
        """Profit as a fraction of capital at risk.

        Both `entry_value` and `max_loss` already carry the spread, so the
        worst case here is exactly -1. Charging the exit cost on top of a
        structural maximum would report a loss larger than the capital the
        position could ever consume, and position sizing trusts max_loss to be
        a real bound.
        """
        if self.max_loss <= 0.0:
            return 0.0
        half = sp.roundtrip_cost / 2.0
        now = self.value_at(spot, days, sp, iv)
        if self.is_credit:
            # Sold to open below mid, bought back above it.
            return (self.entry_value - now * (1.0 + half)) / self.max_loss
        return (now * (1.0 - half) - self.entry_value) / self.max_loss


def open_structure(spot: float, call_put: str, sp: SimParams,
                   is_credit: bool = False,
                   iv: float | None = None) -> OpenStructure:
    """Build the structure a signal at `spot` would open."""
    short_strike = pricing.strike_for(spot, sp.otm_pct, call_put)
    years = sp.dte_days / 365.0
    vol = sp.iv if iv is None else iv
    half = sp.roundtrip_cost / 2.0
    if not is_credit:
        # Bought above mid. The cash paid is the whole risk.
        paid = pricing.price(spot, short_strike, years, vol, call_put,
                             sp.rate) * (1.0 + half)
        return OpenStructure(call_put, short_strike, None, False, paid,
                             max_loss=max(1e-9, paid))
    width = max(0.01, spot * sp.spread_width_pct)
    long_strike = round(short_strike + (width if call_put == "call" else -width), 2)
    received = pricing.spread_value(spot, short_strike, long_strike, years, vol,
                                    call_put, sp.rate) * (1.0 - half)
    # The width is the most the spread can ever be worth, and closing it costs
    # the spread too, so this is the true worst case rather than the textbook
    # width-minus-credit. Sizing gates on it, so it has to be a real bound.
    worst = abs(short_strike - long_strike) * (1.0 + half)
    return OpenStructure(call_put, short_strike, long_strike, True, received,
                         max_loss=max(0.01, worst - received))


def unreachable_take_profit(structure: OpenStructure, take_profit: float) -> bool:
    """True when the take-profit target is above anything the structure can
    earn. Silently unreachable exits are how a strategy ends up judged only on
    its stops."""
    return take_profit > structure.max_gain


def contract_for(entry_px: float, direction: float,
                 sp: SimParams) -> tuple[str, float]:
    """(call_put, strike) of the contract a signal at `entry_px` would buy."""
    call_put = "call" if direction > 0 else "put"
    return call_put, pricing.strike_for(entry_px, sp.otm_pct, call_put)


def premium_of(spot: float, entry_px: float, direction: float, days: float,
               sp: SimParams, iv: float | None = None) -> float:
    """Price of that contract now, per share. Public because
    research/replay.py quotes and reprices positions with it — one model with
    two callers, never two models."""
    call_put, strike = contract_for(entry_px, direction, sp)
    years = max(0.0, (sp.dte_days - days) / 365.0)
    return pricing.price(spot, strike, years, sp.iv if iv is None else iv,
                         call_put, sp.rate)


def option_return(px: float, entry_px: float, direction: float, days: float,
                  sp: SimParams, iv: float | None = None) -> float:
    """Return on premium after `days`, from Black-Scholes at entry and exit.

    The spread is charged on both legs rather than subtracted at the end: you
    buy above mid and sell below it. That also makes -100% the natural floor —
    when the contract expires worthless the exit leg is zero, so the return is
    exactly -1 with no clamp. Subtracting a flat cost from a return already at
    -1 would report a loss larger than the premium, which cannot happen.
    """
    entry_premium = premium_of(entry_px, entry_px, direction, 0.0, sp, iv)
    if entry_premium <= 0.0:
        return -1.0
    half_spread = sp.roundtrip_cost / 2.0
    paid = entry_premium * (1.0 + half_spread)
    received = premium_of(px, entry_px, direction, days, sp, iv) * (1.0 - half_spread)
    return received / paid - 1.0
