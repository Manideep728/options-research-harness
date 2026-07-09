"""Central configuration. Every strategy/risk threshold lives here.

Self-tuning contract: the backtester may propose new values for the SIGNAL
and EXIT parameters listed in TUNABLE_BOUNDS, and only within those bounds.
Risk caps (position limits, sizing, daily limits, circuit breaker) are NOT
tunable — they are the hard guidelines and never move automatically.
"""

import json
import logging
import os
from dataclasses import dataclass, field, replace
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger("bot.config")

# Tunable parameter -> (min, max). Anything outside is clamped at load time,
# so even a hand-edited tuned_params.json cannot escape these rails.
# The RSI bands stop at the 50 midline so bull/bear levels can never invert.
TUNABLE_BOUNDS: dict[str, tuple[float, float]] = {
    "ema_fast": (5, 15),
    "ema_slow": (18, 30),
    "rsi_bull_level": (25.0, 50.0),
    "rsi_bear_level": (50.0, 75.0),
    "take_profit_pct": (0.30, 0.80),
    "stop_loss_pct": (0.15, 0.35),
}


@dataclass(frozen=True)
class Settings:
    # --- credentials / mode ---
    api_key: str = os.getenv("ALPACA_API_KEY", "")
    secret_key: str = os.getenv("ALPACA_SECRET_KEY", "")
    dry_run: bool = os.getenv("DRY_RUN", "true").strip().lower() != "false"

    # --- universe & cadence ---
    # 30-name watchlist: ~10 liquid index/sector ETFs (no earnings gaps) plus
    # ~20 mega-caps for movement and to decorrelate the shortlist. Individual
    # names carry earnings-gap risk, mitigated by the earnings blackout below.
    symbols: tuple[str, ...] = (
        # ETFs
        "SPY", "QQQ", "IWM", "DIA", "XLF", "XLE", "XLK", "SMH", "GLD", "TLT",
        # mega-cap tech / growth
        "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AMD",
        "AVGO", "NFLX",
        # mega-cap financials / staples / energy / health
        "JPM", "V", "MA", "COST", "HD", "WMT", "JNJ", "XOM", "CVX", "BAC",
    )
    # Two-tier scan: rank all `symbols` on a slow cadence, then poll only the
    # top `active_list_size` on the fast `loop_interval_sec`.
    loop_interval_sec: int = 30           # fast poll of the active shortlist
    scan_interval_sec: int = 1800         # re-rank the full universe every 30 min
    active_list_size: int = 5             # how many top-ranked names to poll
    signal_cooldown_sec: int = 1800       # per (symbol, action): suppress repeats
    bar_timeframe_minutes: int = 15
    bar_history_count: int = 100
    skip_open_minutes: int = 15   # no entries in first 15 min of session
    skip_close_minutes: int = 15  # no entries in last 15 min of session

    # --- indicators (tunable within TUNABLE_BOUNDS) ---
    ema_fast: int = 9
    ema_slow: int = 21
    rsi_period: int = 14
    # Measured on real SPY/QQQ 15-min data: with the EMA trend filter active,
    # RSI(14) almost never reaches the classic 30/70 extremes — textbook
    # levels produced ZERO signals in a year. 45/55 = shallow-pullback
    # resumption, firing roughly once per couple of days per symbol.
    rsi_bull_level: float = 45.0  # RSI crossing back UP through this = bullish trigger
    rsi_bear_level: float = 55.0  # RSI crossing back DOWN through this = bearish trigger

    # --- earnings blackout (single-name gap protection) ---
    # Skip entries on a symbol whose earnings fall within this many days of
    # today, in either direction — we hold up to max_hold_days across
    # overnights, and an earnings gap can open straight through the stop.
    earnings_blackout_days: int = 3

    # --- contract selection ---
    min_dte: int = 7
    max_dte: int = 14
    max_spread_pct_of_mid: float = 0.10
    min_open_interest: int = 100

    # --- exits (tunable within TUNABLE_BOUNDS, except time stops) ---
    take_profit_pct: float = 0.50   # +50% of premium
    stop_loss_pct: float = 0.25     # -25% of premium
    time_stop_dte: int = 2          # close at <= 2 days to expiry
    max_hold_days: float = 2.0      # close after 2 calendar days regardless

    # --- execution ---
    limit_buffer_pct: float = 0.01     # entry limit = mid * (1 + this), capped at ask
    stale_order_cancel_sec: int = 120  # cancel unfilled limit orders after this

    # --- risk gates (HARD guidelines — never self-tuned) ---
    max_positions: int = 3
    max_positions_per_underlying: int = 1
    max_premium_pct_of_equity: float = 0.02
    max_trades_per_day: int = 3
    daily_loss_limit_pct: float = 0.04

    # --- files ---
    state_file: str = field(default="state.json")
    control_file: str = field(default="control.json")
    log_file: str = field(default="bot.log")
    journal_file: str = field(default="trades.csv")
    tuned_params_file: str = field(default="tuned_params.json")
    earnings_file: str = field(default="earnings.json")

    def validate(self) -> None:
        if not self.dry_run and (not self.api_key or not self.secret_key):
            raise ValueError(
                "DRY_RUN=false requires ALPACA_API_KEY and ALPACA_SECRET_KEY in .env"
            )

    @classmethod
    def load(cls) -> "Settings":
        """Defaults + clamped overrides from tuned_params.json (if present)."""
        cfg = cls()
        return apply_tuned_params(cfg, cfg.tuned_params_file)


def clamp_tunables(params: dict) -> dict:
    """Restrict a raw override dict to known tunables, clamped into bounds."""
    out: dict = {}
    for key, (lo, hi) in TUNABLE_BOUNDS.items():
        if key not in params:
            continue
        value = max(lo, min(hi, float(params[key])))
        if isinstance(getattr(Settings, key), int):
            value = int(round(value))
        out[key] = value
    # A near-equal EMA pair would make the trend filter meaningless;
    # require the slow period to exceed the fast by more than 3.
    if "ema_fast" in out or "ema_slow" in out:
        fast = out.get("ema_fast", Settings.ema_fast)
        slow = out.get("ema_slow", Settings.ema_slow)
        if slow - fast <= 3:
            out.pop("ema_fast", None)
            out.pop("ema_slow", None)
    return out


def apply_tuned_params(cfg: Settings, path: str) -> Settings:
    file = Path(path)
    if not file.exists():
        return cfg
    try:
        raw = json.loads(file.read_text())
        overrides = clamp_tunables(raw.get("params", {}))
    except (json.JSONDecodeError, TypeError, ValueError) as e:
        log.warning("ignoring unreadable %s: %s", path, e)
        return cfg
    if overrides:
        log.info("applying tuned params from %s: %s", path, overrides)
        return replace(cfg, **overrides)
    return cfg
