"""The coin-flip control, and the guard it now backs.

The bug these pin: bot/simulator.py used to book a barrier exit at the bar's
overshoot past the barrier, floored at -100% on losses but uncapped on gains.
That turned volatility into expectancy, and robustness.daily_regime_check —
which passed a fold on `expectancy > 0` — could not fail a random signal.
"""

from datetime import UTC, datetime, timedelta

from bot.config import Settings
from bot.simulator import SimParams
from bot.strategy import Action
from research import null, robustness
from research.families import FAMILIES
from research.search import run_family

CFG = Settings(api_key="", secret_key="")
SP = SimParams()
T0 = datetime(2026, 1, 5, 14, 30, tzinfo=UTC)


def bars(closes, minutes=15):
    return closes, [T0 + timedelta(minutes=minutes * i) for i in range(len(closes))]


def flat_window(n=400, price=100.0):
    """A dead-flat series: no move at all, so the ONLY thing a random signal
    can collect is theta plus the roundtrip cost."""
    return {"FLAT": bars([price] * n)}


# --- the random signal itself ---

def test_null_family_is_deterministic_per_seed():
    closes, times = bars([100.0 + i * 0.1 for i in range(200)])
    first = null.null_family(7, 0.2).signal(closes, times, {})
    second = null.null_family(7, 0.2).signal(closes, times, {})
    assert first == second


def test_null_family_decorrelates_across_series():
    """Two symbols must get independent draws. Sharing one draw across a
    correlated universe piles every null trade onto the same bars, which
    inflates the spread of the distribution and makes the threshold uselessly
    wide."""
    a_closes, a_times = bars([100.0] * 300)
    b_closes, b_times = bars([100.0] * 301)  # different length -> different draw
    fam = null.null_family(7, 0.2)
    assert fam.signal(a_closes, a_times, {}) != fam.signal(b_closes, b_times, {})[:300]


def test_null_family_fire_rate_is_respected():
    closes, times = bars([100.0] * 4000)
    fired = [a for a in null.null_family(1, 0.10).signal(closes, times, {})
             if a != Action.NONE]
    assert 300 < len(fired) < 500          # ~10% of 4000, generous band
    calls = sum(1 for a in fired if a == Action.BUY_CALL)
    assert 0.4 < calls / len(fired) < 0.6  # and roughly balanced


def test_null_on_flat_prices_loses_exactly_the_costs():
    """The sanity anchor. With no price movement there is no edge to find, so
    a random signal must lose theta plus the spread — never make money."""
    summary = null.null_distribution(flat_window(), CFG, SP, target_trades=40, seeds=20)
    assert summary.seeds > 0
    assert summary.mean < -SP.roundtrip_cost
    assert summary.threshold < 0


# --- the summary's arithmetic ---

def test_percentile_is_nearest_rank():
    values = [float(i) for i in range(1, 101)]   # 1..100
    assert null.percentile(values, 0.95) == 95.0
    assert null.percentile(values, 0.5) == 50.0
    assert null.percentile([], 0.95) == 0.0      # missing null is not an easy bar


def test_empty_null_cannot_license_a_pass():
    """A null that produced nothing must never wave a candidate through — the
    absence of a control is not evidence of skill."""
    empty = null.NullSummary(expectancies=[], trades=[], fire_rate=0.0)
    assert not empty.beats(9.99)


def test_under_traded_null_cannot_license_a_pass():
    thin = null.NullSummary(expectancies=[0.01] * 50, trades=[1] * 50, fire_rate=0.001)
    assert thin.mean_trades < null.MIN_NULL_TRADES
    assert not thin.beats(9.99)


def test_percentile_of_ranks_the_candidate():
    summary = null.NullSummary(expectancies=[0.0, 0.1, 0.2, 0.3], trades=[50] * 4,
                               fire_rate=0.1)
    assert summary.percentile_of(0.25) == 0.75
    assert summary.percentile_of(-1.0) == 0.0


def test_fire_rate_targets_the_trade_count():
    windows = flat_window(n=1000)
    assert null.fire_rate_for(windows, 100) == 0.1
    assert null.fire_rate_for(windows, 0) == 0.0
    assert null.fire_rate_for({}, 100) == 0.0


# --- the guard the null now backs ---

def test_regime_guard_rejects_a_coin_flip(tmp_path):
    """The regression test for the whole finding: a zero-skill signal must not
    pass the multi-regime check. Before this, `expectancy > 0` let it through
    at +11% per trade on real daily bars."""
    day = datetime(2021, 1, 4, 21, 0, tzinfo=UTC)
    for symbol in ("SPY", "QQQ"):
        rows = ["timestamp,close"]
        price = 100.0
        for i in range(1200):          # ~5 calendar years of daily bars
            price *= 1.004 if i % 3 else 0.994   # volatile, no exploitable pattern
            rows.append(f"{(day + timedelta(days=i)).isoformat()},{price:.6f}")
        target = tmp_path / "daily" / f"{symbol}.csv"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("\n".join(rows) + "\n")

    cfg = Settings(api_key="", secret_key="", symbols=("SPY", "QQQ"))
    passed, rows_out = robustness.daily_regime_check(
        cfg, null.null_family(4242, 0.15), {}, data_dir=tmp_path, null_seeds=25,
    )
    assert not passed
    assert any(r.null_threshold is not None for r in rows_out), "no fold was countable"


def test_regime_guard_reports_the_null_bar_it_used(tmp_path):
    """Every countable fold must carry the threshold it was judged against, so
    a FAIL can be read without re-running anything."""
    day = datetime(2022, 1, 4, 21, 0, tzinfo=UTC)
    rows = ["timestamp,close"]
    price = 100.0
    for i in range(900):
        price *= 1.01 if i % 2 else 0.995
        rows.append(f"{(day + timedelta(days=i)).isoformat()},{price:.6f}")
    target = tmp_path / "daily" / "SPY.csv"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(rows) + "\n")

    cfg = Settings(api_key="", secret_key="", symbols=("SPY",))
    _, rows_out = robustness.daily_regime_check(
        cfg, FAMILIES["donchian"], {"lookback": 20}, data_dir=tmp_path, null_seeds=15,
    )
    countable = [r for r in rows_out if r.trades >= robustness.MIN_FOLD_TRADES]
    assert countable, "fixture produced no countable fold"
    for row in countable:
        assert row.null_threshold is not None
        assert row.null_trades is not None


def test_null_runs_through_the_same_engine_as_a_real_family():
    """The null must not be a parallel implementation — a private simulator
    would drift away from the thing it is supposed to be measuring, which is
    the exact class of bug it exists to catch."""
    windows = flat_window(n=500)
    fam = null.null_family(3, 0.05)
    result = run_family(windows, CFG, SP, fam, {})
    assert result.n > 0
    # run_family is what search.py uses for real candidates, so agreeing with
    # it is the point; every trade must be a well-formed SimTrade.
    assert all(t.symbol == "FLAT" and t.direction in ("call", "put")
               for t in result.trades)
