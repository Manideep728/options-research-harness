"""Robustness: perturbation grid shape/verdict and daily regime folds."""

from datetime import UTC, datetime, timedelta

import pytest

from bot.config import Settings
from bot.simulator import SimParams
from research import data, robustness
from research.families import FAMILIES


def _cfg(**kw) -> Settings:
    return Settings(api_key="k", secret_key="s", **kw)


def _sawtooth_bars(n, start_year=2026, step_minutes=15):
    start = datetime(start_year, 1, 5, 14, 30, tzinfo=UTC)
    times = [start + timedelta(minutes=step_minutes * i) for i in range(n)]
    closes = []
    while len(closes) < n:
        closes += [100.0 + j for j in range(12)]
        closes += [111.0 - j for j in range(12)]
    return closes[:n], times


def test_perturbation_grid_covers_all_factor_combos():
    grid = robustness.perturbation_grid(SimParams())
    assert len(grid) == 27
    labels = [label for label, _ in grid]
    assert "iv x1.0 dte x1.0 cost x1.0" in labels
    ivs = {round(sp.iv, 6) for _, sp in grid}
    assert len(ivs) == 3


def test_check_perturbations_flags_cost_sensitive_edge():
    closes, times = _sawtooth_bars(600)
    windows = {"AAA": (closes, times)}
    family = FAMILIES["rsi_only"]
    params = {"rsi_bull_level": 40.0, "rsi_bear_level": 60.0}
    passed, rows = robustness.check_perturbations(windows, _cfg(), family, params)
    assert len(rows) == 27
    assert isinstance(passed, bool)
    # verdict must be consistent with the rows it reports
    should_fail = any(r.trades > 0 and r.expectancy <= 0 for r in rows)
    assert passed == (not should_fail)


def test_daily_regime_check_requires_enough_folds(tmp_path):
    # one thin year of daily bars -> not enough evaluable folds -> FAIL
    closes, times = _sawtooth_bars(30, step_minutes=60 * 24)
    data.save_bars(tmp_path / "daily" / "AAA.csv", closes, times)
    cfg = _cfg(symbols=("AAA",))
    passed, _rows, _note = robustness.daily_regime_check(
        cfg, FAMILIES["rsi_only"],
        {"rsi_bull_level": 40.0, "rsi_bear_level": 60.0}, data_dir=tmp_path)
    assert not passed


def test_daily_regime_check_groups_by_year(tmp_path):
    closes, times = _sawtooth_bars(700, start_year=2024, step_minutes=60 * 24)
    data.save_bars(tmp_path / "daily" / "AAA.csv", closes, times)
    cfg = _cfg(symbols=("AAA",))
    _, rows, _note = robustness.daily_regime_check(
        cfg, FAMILIES["rsi_only"],
        {"rsi_bull_level": 40.0, "rsi_bear_level": 60.0}, data_dir=tmp_path)
    years = [r.label for r in rows]
    assert years == sorted(years) and len(years) >= 2


# --- the tail: what a short-premium strategy loses when it is wrong ---

def _result(returns):
    from datetime import UTC, datetime

    from bot.simulator import SimResult, SimTrade
    t0 = datetime(2026, 1, 5, tzinfo=UTC)
    return SimResult(trades=[SimTrade(t0, t0, "put", 1.0, 1.0, r, "x") for r in returns])


def test_tail_check_fails_a_book_that_looks_fine_on_average():
    """The whole reason this check exists. Selling premium wins small and often
    and loses large and rarely, so a strategy about to destroy itself and a
    sound one are indistinguishable on expectancy."""
    # 200 wins at +5% outweigh one -900% loss, so the average is positive while
    # the single worst trade is nine times the whole account's capital at risk.
    ruinous = _result([0.05] * 200 + [-9.0])
    assert ruinous.expectancy > 0                    # looks profitable
    passed, row = robustness.check_tail(ruinous)
    assert not passed
    assert row.worst_trade == pytest.approx(-9.0)
    assert row.loss_ratio == pytest.approx(180.0)    # 9.0 / 0.05


def test_tail_check_passes_a_defined_risk_book():
    """A spread cannot lose more than its capital at risk, so a worst trade at
    the -100% floor is not by itself a failure — the drawdown decides."""
    passed, row = robustness.check_tail(_result([0.10] * 30 + [-1.0] * 2))
    assert passed
    assert row.worst_trade == pytest.approx(-1.0)


def test_tail_check_fails_when_losses_arrive_together():
    """One bounded loss is survivable; six in a row is the thing that ends a
    short-volatility book, and expectancy alone cannot distinguish them."""
    clustered = _result([-1.0] * 9 + [0.10] * 60)
    passed, row = robustness.check_tail(clustered)
    assert not passed
    assert row.max_drawdown > robustness.MAX_DRAWDOWN


def test_tail_check_on_no_trades_does_not_pass():
    passed, row = robustness.check_tail(_result([]))
    assert not passed and row.trades == 0
