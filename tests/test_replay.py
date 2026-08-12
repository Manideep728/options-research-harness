"""Historical replay of the REAL engine over cached bars.

What these pin, in order of how badly a regression would hurt:
  1. Causality — get_closes must never return a bar at or after `now`. A leak
     here is invisible and makes every replay result meaningless.
  2. One P&L model — positions reprice through bot.simulator.option_return, not
     a second copy of it living in research/replay.py.
  3. The risk caps bot/simulator.py never simulated: max_trades_per_day,
     max_positions, max_positions_per_underlying, the signal cooldown.
"""

from datetime import UTC, date, datetime, time, timedelta

from bot.config import Settings
from bot.options import Contract, pick_contract
from bot.simulator import SimParams, option_return
from bot.strategy import Action
from research import replay
from research.data import MARKET_TZ

SP = SimParams()
BARS_PER_DAY = 26           # 09:30..15:45 ET in 15-minute steps


def session_bars(days: int, prices) -> tuple[list[float], list[datetime]]:
    """`days` weekdays of 15-min regular-session bars, timestamped in UTC."""
    times: list[datetime] = []
    day = date(2026, 3, 2)  # a Monday
    while len(times) < days * BARS_PER_DAY:
        start = datetime.combine(day, time(9, 30), tzinfo=MARKET_TZ)
        for b in range(BARS_PER_DAY):
            times.append((start + timedelta(minutes=15 * b)).astimezone(UTC))
        day += timedelta(days=1)
        while day.weekday() >= 5:
            day += timedelta(days=1)
    return [prices(i) for i in range(len(times))], times


def bounce_wave(n: int) -> list[float]:
    """A repeating version of the exact shape tests/test_engine.py uses to
    trigger a BUY_CALL: a long +0.3/bar uptrend (so EMA9 > EMA21), a 6-bar
    -1.0/bar pullback (dragging RSI under 45), then a +3.0 recovery bar that
    crosses RSI back up through it. 67 bars per cycle."""
    out: list[float] = []
    px = 100.0
    while len(out) < n:
        for _ in range(60):
            px += 0.3
            out.append(px)
        for _ in range(6):
            px -= 1.0
            out.append(px)
        px += 3.0
        out.append(px)
    return out[:n]


def wave_bars(days: int) -> tuple[list[float], list[datetime]]:
    _, times = session_bars(days, lambda i: 0.0)
    return bounce_wave(len(times)), times


def cfg_for(tmp_path, symbols, **overrides) -> Settings:
    return Settings(
        api_key="k", secret_key="s", symbols=symbols,
        state_file=str(tmp_path / "state.json"),
        journal_file=str(tmp_path / "trades.csv"),
        control_file=str(tmp_path / "control.json"),
        active_file=str(tmp_path / "active.json"),
        earnings_file=str(tmp_path / "earnings.json"),
        **overrides,
    )


# --- 1. causality ---

def test_get_closes_never_returns_a_bar_at_or_after_now(tmp_path):
    """The engine is entitled to assume it cannot see the future. Checked at
    every step, not just once, because an off-by-one in bisect would only show
    up on exact timestamp matches."""
    from bisect import bisect_right

    closes, times = wave_bars(12)
    cfg = cfg_for(tmp_path, ("SPY",))
    broker = replay.ReplayBroker({"SPY": (closes, times)}, cfg)
    while True:
        visible = broker.get_closes("SPY")
        if visible:
            # Compare against the bisect position rather than len(visible),
            # because get_closes also truncates to bar_history_count.
            cut = bisect_right(times, broker.now)
            assert visible[-1] == closes[cut - 1]
            assert times[cut - 1] <= broker.now
            if cut < len(times):
                assert times[cut] > broker.now
                assert closes[cut] not in visible[-1:]
        if not broker.step():
            break


def test_get_closes_matches_the_live_history_window(tmp_path):
    """Live, the broker returns at most bar_history_count bars. Returning more
    here would let a replayed strategy use lookback the live bot never has."""
    closes, times = wave_bars(10)
    cfg = cfg_for(tmp_path, ("SPY",), bar_history_count=40)
    broker = replay.ReplayBroker({"SPY": (closes, times)}, cfg)
    broker.index = len(times) - 1
    assert len(broker.get_closes("SPY")) == 40


def test_symbols_with_different_bar_grids_stay_aligned(tmp_path):
    """The timeline is the union of all symbols' timestamps, so a symbol with
    sparser bars must not leak a bar from the future when another symbol's bar
    advances the clock."""
    closes, times = wave_bars(6)
    sparse = (closes[::2], times[::2])
    cfg = cfg_for(tmp_path, ("SPY", "QQQ"))
    broker = replay.ReplayBroker({"SPY": (closes, times), "QQQ": sparse}, cfg)
    for _ in range(20):
        broker.step()
    visible = broker.get_closes("QQQ")
    assert all(t <= broker.now for t in sparse[1][:len(visible)])


# --- 2. one P&L model ---

def test_positions_reprice_through_the_shared_option_model(tmp_path):
    """research/replay.py must not grow its own pricing model — that is the
    backtest/live drift this whole module exists to remove."""
    closes, times = wave_bars(6)
    cfg = cfg_for(tmp_path, ("SPY",))
    broker = replay.ReplayBroker({"SPY": (closes, times)}, cfg)
    broker.index = 40
    px = broker.get_underlying_price("SPY")
    chain = broker.get_chain("SPY", "call", broker.now.date(), px)
    broker.buy_option(chain[0], qty=1)
    entry_premium, entry_spot, entry_time = chain[0].ask, px, broker.now

    broker.index = 50
    position = broker.get_option_positions()[0]
    expected_ret = option_return(
        broker.get_underlying_price("SPY"), entry_spot, 1.0,
        (broker.now - entry_time).total_seconds() / 86400, SP,
    )
    assert position.current_price == max(0.0, entry_premium * (1 + expected_ret))


def test_synthesized_chain_passes_the_live_liquidity_filter(tmp_path):
    """A chain the real pick_contract rejects would make every replay trade
    zero, which would look like 'the strategy never fires' rather than 'the
    fixture is broken'."""
    closes, times = wave_bars(6)
    cfg = cfg_for(tmp_path, ("SPY",))
    broker = replay.ReplayBroker({"SPY": (closes, times)}, cfg)
    broker.index = 40
    px = broker.get_underlying_price("SPY")
    today = broker.now.date()
    chain = broker.get_chain("SPY", "call", today, px)
    contract, rejections = pick_contract(chain, Action.BUY_CALL, px, today, cfg)
    assert contract is not None, f"chain rejected: {rejections}"
    # Strictly inside the gate, not sitting on it — rounding to cents on a
    # sub-dollar premium is enough to tip a limit-hugging quote over.
    assert (chain[0].ask - chain[0].bid) / chain[0].mid < cfg.max_spread_pct_of_mid


def test_a_total_loss_reports_a_price_of_exactly_zero(tmp_path):
    """The state that surfaced a live-engine bug, pinned here so it stays
    reachable. option_return floors at -100%, so a total loss prices the
    premium at exactly 0.0 rather than at some small residual.

    That is what a dying option really quotes, and bot/engine.py's manage_exits
    used to skip any position with current_price <= 0 — so exit_reason never
    ran and the dead contract held one of three max_positions slots until
    expiry. The engine now falls through instead; the fix is covered by
    test_worthless_position_is_closed_not_stranded in tests/test_engine.py.

    This test guards the other half: if ReplayBroker ever priced a total loss
    at 0.01 instead of 0.0, replay would stop being able to produce the case
    at all, and the next bug of this shape would go unfound."""
    closes, times = wave_bars(6)
    cfg = cfg_for(tmp_path, ("SPY",))
    broker = replay.ReplayBroker({"SPY": (closes, times)}, cfg)
    broker.index = 40
    px = broker.get_underlying_price("SPY")
    # Buy a PUT, then move the underlying up hard enough to exceed 100%/gearing.
    chain = broker.get_chain("SPY", "put", broker.now.date(), px)
    broker.buy_option(chain[0], qty=1)
    holding = next(iter(broker._holdings.values()))
    broker._closes["SPY"] = [c * 3.0 for c in broker._closes["SPY"]]

    assert broker._premium_now(holding) == 0.0
    assert broker.get_option_positions()[0].current_price == 0.0


# --- 3. the risk caps simulate() never modelled ---

def test_max_trades_per_day_is_enforced(tmp_path):
    """bot/simulator.py has no concept of a daily trade cap; the live bot stops
    at 3. This is the whole reason ReplayBroker exists."""
    closes, times = wave_bars(12)
    bars = dict.fromkeys(("SPY", "QQQ", "IWM", "DIA", "XLF"), (closes, times))
    cfg = cfg_for(tmp_path, tuple(bars), max_trades_per_day=2, signal_cooldown_sec=0)
    stats = replay.run_replay(bars, cfg)

    per_day: dict[date, int] = {}
    for fill in stats.fills:
        if fill.side == "buy":
            per_day[fill.filled_at.date()] = per_day.get(fill.filled_at.date(), 0) + 1
    assert per_day, "fixture produced no entries at all"
    assert max(per_day.values()) <= 2


def test_max_positions_is_enforced(tmp_path):
    closes, times = wave_bars(12)
    bars = dict.fromkeys(("SPY", "QQQ", "IWM", "DIA", "XLF"), (closes, times))
    cfg = cfg_for(tmp_path, tuple(bars), max_positions=1, max_trades_per_day=99,
                  signal_cooldown_sec=0)
    broker = replay.ReplayBroker(bars, cfg)
    from bot.engine import Engine
    engine = Engine(broker, cfg)
    while True:
        engine.run_cycle()
        assert len(broker.get_option_positions()) <= 1
        if not broker.step():
            break


def test_one_position_per_underlying_is_enforced(tmp_path):
    closes, times = wave_bars(12)
    cfg = cfg_for(tmp_path, ("SPY",), max_trades_per_day=99, signal_cooldown_sec=0)
    broker = replay.ReplayBroker({"SPY": (closes, times)}, cfg)
    from bot.engine import Engine
    engine = Engine(broker, cfg)
    while True:
        engine.run_cycle()
        held = [p for p in broker.get_option_positions() if p.underlying == "SPY"]
        assert len(held) <= cfg.max_positions_per_underlying
        if not broker.step():
            break


def test_cooldown_reduces_entry_count(tmp_path):
    """A 30-minute per-signal cooldown is invisible to simulate(). With it off,
    the same setup can re-enter far more often."""
    closes, times = wave_bars(12)
    bars = dict.fromkeys(("SPY", "QQQ", "IWM"), (closes, times))
    hot = replay.run_replay(bars, cfg_for(tmp_path / "hot", tuple(bars),
                                          max_trades_per_day=99, signal_cooldown_sec=0))
    cool = replay.run_replay(bars, cfg_for(tmp_path / "cool", tuple(bars),
                                           max_trades_per_day=99,
                                           signal_cooldown_sec=7200))
    assert hot.buys > 0
    assert cool.buys <= hot.buys


# --- session helpers ---

def test_session_bound_follows_dst():
    """09:30 ET is 14:30 UTC in March and 13:30 UTC in July. A fixed offset
    would put the entry window in the wrong place for half the year."""
    winter = datetime(2026, 3, 2, 18, 0, tzinfo=UTC)
    summer = datetime(2026, 7, 15, 18, 0, tzinfo=UTC)
    assert replay._session_bound(winter, time(9, 30)).astimezone(UTC).hour == 14
    assert replay._session_bound(summer, time(9, 30)).astimezone(UTC).hour == 13


def test_occ_symbol_shape():
    symbol = replay._occ_symbol("SPY", date(2026, 4, 17), 612.5, "call")
    assert symbol == "SPY260417C00612500"
    assert replay._occ_symbol("SPY", date(2026, 4, 17), 612.5, "put").endswith("P00612500")


def test_realized_returns_matches_buys_to_sells():
    stats = replay.ReplayStats()
    from bot.broker import Fill
    now = datetime(2026, 3, 2, 15, 0, tzinfo=UTC)
    stats.fills = [
        Fill("1", now, "SPYX", "SPY", "buy", 1, 2.00),
        Fill("2", now, "SPYX", "SPY", "sell", 1, 3.00),   # +50%
        Fill("3", now, "QQQX", "QQQ", "buy", 1, 4.00),
        Fill("4", now, "QQQX", "QQQ", "sell", 1, 3.00),   # -25%
        Fill("5", now, "IWMX", "IWM", "buy", 1, 1.00),    # still open
    ]
    assert stats.realized_returns == [0.5, -0.25]


def test_empty_bars_do_not_crash(tmp_path):
    cfg = cfg_for(tmp_path, ("SPY",))
    broker = replay.ReplayBroker({"SPY": ([], [])}, cfg)
    assert broker.get_closes("SPY") == []
    assert broker.get_underlying_price("SPY") == 0.0
    assert broker.get_chain("SPY", "call", date(2026, 3, 2), 0.0) == []


def test_chain_is_otm_on_both_sides(tmp_path):
    closes, times = wave_bars(4)
    cfg = cfg_for(tmp_path, ("SPY",))
    broker = replay.ReplayBroker({"SPY": (closes, times)}, cfg)
    broker.index = 30
    px = broker.get_underlying_price("SPY")
    call: Contract = broker.get_chain("SPY", "call", broker.now.date(), px)[0]
    put: Contract = broker.get_chain("SPY", "put", broker.now.date(), px)[0]
    assert call.strike > px and put.strike < px
