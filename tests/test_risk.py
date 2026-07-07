from bot.config import Settings
from bot.risk import OpenPosition, entry_allowed, exit_reason, size_allowed

CFG = Settings(api_key="", secret_key="")


def pos(underlying="SPY", entry=2.00, current=2.00, dte=10):
    return OpenPosition(
        symbol=f"{underlying}260717C00500000",
        underlying=underlying,
        qty=1,
        avg_entry_price=entry,
        current_price=current,
        days_to_expiry=dte,
    )


# --- entry gates ---

def test_entry_allowed_when_clean():
    result = entry_allowed("SPY", [], 0, 100_000, 100_000, CFG)
    assert result.allowed


def test_blocks_after_daily_trade_limit():
    result = entry_allowed("SPY", [], CFG.max_trades_per_day, 100_000, 100_000, CFG)
    assert not result.allowed
    assert "daily trade limit" in result.reason


def test_blocks_at_max_concurrent_positions():
    positions = [pos("SPY"), pos("QQQ"), pos("AAPL")]
    result = entry_allowed("MSFT", positions, 0, 100_000, 100_000, CFG)
    assert not result.allowed
    assert "max concurrent" in result.reason


def test_blocks_duplicate_underlying():
    result = entry_allowed("SPY", [pos("SPY")], 0, 100_000, 100_000, CFG)
    assert not result.allowed
    assert "SPY" in result.reason


def test_circuit_breaker_trips_at_4pct_drawdown():
    result = entry_allowed("SPY", [], 0, 95_900, 100_000, CFG)
    assert not result.allowed
    assert "circuit breaker" in result.reason


def test_circuit_breaker_not_tripped_just_above_limit():
    result = entry_allowed("SPY", [], 0, 96_100, 100_000, CFG)
    assert result.allowed


def test_zero_day_start_equity_does_not_divide_by_zero():
    result = entry_allowed("SPY", [], 0, 100_000, 0.0, CFG)
    assert result.allowed


# --- sizing ---

def test_size_allowed_within_2pct():
    # $1.50 premium -> $150/contract, limit on $100k is $2000
    assert size_allowed(1.50, 100_000, CFG).allowed


def test_size_blocked_over_2pct():
    # $25 premium -> $2500/contract > $2000 limit
    result = size_allowed(25.0, 100_000, CFG)
    assert not result.allowed
    assert "exceeds" in result.reason


# --- exits ---

def test_take_profit_at_plus_50pct():
    assert "take profit" in exit_reason(pos(entry=2.00, current=3.00), CFG)


def test_stop_loss_at_minus_25pct():
    assert "stop loss" in exit_reason(pos(entry=2.00, current=1.50), CFG)


def test_time_stop_at_2_dte():
    assert "time stop" in exit_reason(pos(dte=2), CFG)


def test_holds_inside_all_bounds():
    assert exit_reason(pos(entry=2.00, current=2.20, dte=10), CFG) is None


def test_zero_entry_price_does_not_crash():
    assert exit_reason(pos(entry=0.0, current=1.0, dte=10), CFG) is None


def test_max_hold_exit_with_known_entry_time():
    assert "max hold" in exit_reason(pos(), CFG, held_days=2.5)


def test_max_hold_skipped_when_entry_time_unknown():
    assert exit_reason(pos(), CFG, held_days=None) is None


def test_max_hold_not_triggered_early():
    assert exit_reason(pos(), CFG, held_days=1.5) is None
