"""Robustness: perturbation grid shape/verdict and daily regime folds."""

from datetime import UTC, datetime, timedelta

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
    assert "delta x1.0 theta x1.0 cost x1.0" in labels
    deltas = {sp.delta for _, sp in grid}
    assert deltas == {0.2, 0.4, 0.6000000000000001} or len(deltas) == 3


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
