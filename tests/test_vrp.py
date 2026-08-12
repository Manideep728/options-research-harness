"""The variance risk premium kill criterion.

What these pin, in order of how badly a regression would hurt:
  1. Causality — the realized side must look FORWARD from each implied reading.
     A trailing window would compare implied vol against volatility that had
     already happened, which is not a premium, it is an autocorrelation.
  2. Date alignment — the index and the ETF are separate feeds. Joining by
     position silently offsets both series after one mismatched holiday.
  3. The kill criterion is a cost hurdle, not `> 0`. That is the same mistake
     daily_regime_check made with `expectancy > 0`, and a coin flip cleared it.
"""

import itertools
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from research import data, vrp


def _days(n: int, start=datetime(2020, 1, 2, tzinfo=UTC)) -> list[datetime]:
    return [start + timedelta(days=i) for i in range(n)]


def _alternating(n: int, step: float = 0.01) -> list[float]:
    """Closes whose consecutive log returns are exactly +/- `step`."""
    return [100.0 * math.exp(step * (i % 2)) for i in range(n)]


# --- realized volatility arithmetic ---

def test_realized_vol_hand_computed():
    """Every log return is exactly +/-0.01, so mean square = 1e-4 and the
    annualized figure is sqrt(1e-4 * 252) * 100. A wrong annualization factor
    (252 vs 365, or a missing square root) fails here."""
    assert vrp.realized_vol(_alternating(21)) == pytest.approx(
        math.sqrt(1e-4 * 252) * 100.0)


def test_realized_vol_is_zero_mean_not_demeaned():
    """A pure trend has constant log returns: zero dispersion about the mean,
    but real squared returns. The variance-swap estimator must report the
    move; statistics.pstdev would report 0.0 and hide a trending market's risk
    entirely."""
    trending = [100.0 * math.exp(0.01 * i) for i in range(22)]
    assert vrp.realized_vol(trending) == pytest.approx(math.sqrt(1e-4 * 252) * 100.0)


def test_realized_vol_degenerate_inputs():
    assert vrp.realized_vol([]) == 0.0
    assert vrp.realized_vol([100.0]) == 0.0
    assert vrp.realized_vol([100.0, 100.0, 100.0]) == 0.0   # no movement, no vol


def test_log_returns_reject_non_positive_prices():
    """A zero or negative close is a data defect. Silently producing nan would
    propagate into a mean VRP that reads as a number."""
    with pytest.raises(ValueError, match="non-positive"):
        vrp.log_returns([100.0, 0.0, 100.0])


# --- causality: the realized side looks forward ---

def test_forward_vol_ignores_everything_before_the_index():
    """THE causality test. Mutating a bar strictly before index k must not
    change the forward vol at k. An off-by-one that included closes[k-1..k+h]
    would fail, and that error is invisible in the printed output."""
    closes = _alternating(40)
    k, horizon = 20, 5
    before = vrp.forward_realized_vol(closes, horizon)[k]
    closes[0] = 500.0                     # violent change, entirely in the past
    after = vrp.forward_realized_vol(closes, horizon)[k]
    assert before == after


def test_forward_vol_tracks_what_comes_next_not_what_came_before():
    calm = [100.0] * 12
    wild = _alternating(12, step=0.05)
    series = calm + wild
    forward = vrp.forward_realized_vol(series, horizon=5)
    assert forward[0] == 0.0                 # five calm bars ahead
    assert (forward[11] or 0.0) > 50.0       # steps into the wild stretch


def test_incomplete_forward_windows_are_dropped_not_truncated():
    """A short window reads as unnaturally low volatility, which would inflate
    the measured premium precisely at the end of the sample — where a reader is
    most likely to be looking."""
    forward = vrp.forward_realized_vol(_alternating(10), horizon=4)
    # index 5 still spans closes[5:10] — four full returns, the last complete
    # window. Everything from 6 on is short and must be None.
    assert all(v is not None for v in forward[:6])
    assert all(v is None for v in forward[6:])


# --- alignment ---

def test_build_rows_joins_on_date_not_position():
    """The index and the ETF have independent holiday calendars. A positional
    join would pair one series' Tuesday with the other's Wednesday from the
    first mismatch onward, and nothing downstream could detect it."""
    under_times = _days(8)
    under_closes = _alternating(8)
    implied_times = [under_times[0], under_times[3]]   # a gap the ETF does not have
    rows = vrp.build_rows([11.0, 22.0], implied_times, under_closes, under_times,
                          horizon=2)
    assert [r.day for r in rows] == [under_times[0].date(), under_times[3].date()]
    assert [r.implied for r in rows] == [11.0, 22.0]


def test_build_rows_drops_implied_days_with_no_forward_window():
    under_times = _days(6)
    rows = vrp.build_rows([15.0] * 6, under_times, _alternating(6), under_times,
                          horizon=4)
    assert [r.day for r in rows] == [under_times[0].date(), under_times[1].date()]


def test_vrp_is_implied_minus_realized():
    row = vrp.VrpRow(day=datetime(2020, 1, 2, tzinfo=UTC).date(),
                     implied=20.0, realized=12.5)
    assert row.vrp == pytest.approx(7.5)


# --- the effect, on constructed data where the answer is known ---

def _summary(implied_level: float, realized_step: float, n: int = 200,
             horizon: int = 5) -> vrp.VrpSummary:
    times = _days(n)
    return vrp.summarize("IDX", "SYM", [implied_level] * n, times,
                         _alternating(n, realized_step), times, horizon=horizon)


def test_premium_is_detected_when_implied_sits_above_realized():
    realized = math.sqrt(0.01**2 * 252) * 100.0     # ~15.9
    summary = _summary(implied_level=realized + 5.0, realized_step=0.01)
    assert summary.mean == pytest.approx(5.0, abs=0.01)
    assert summary.share_positive == 1.0


def test_negative_premium_is_detected_and_fails_the_criterion():
    """The kill branch has to actually fire. If implied sits below realized the
    thesis is refuted and clears() must say so."""
    realized = math.sqrt(0.01**2 * 252) * 100.0
    summary = _summary(implied_level=realized - 3.0, realized_step=0.01)
    assert summary.mean < 0
    assert not summary.clears()


def test_a_premium_too_small_to_pay_for_itself_does_not_clear():
    """The mistake this repo already made once: judging on `> 0`. A premium of
    0.1 vol points is positive and completely unharvestable."""
    realized = math.sqrt(0.01**2 * 252) * 100.0
    summary = _summary(implied_level=realized + 0.1, realized_step=0.01)
    assert summary.mean > 0
    assert not summary.clears()
    assert summary.clears(roundtrip=0.0)     # only with costs wished away


# --- tail measurement ---

def test_cycles_are_non_overlapping():
    """Consecutive daily rows share 20 of 21 forward days. Measuring drawdown
    on them smears one crash across 21 observations and flatters the tail."""
    times = _days(60)
    rows = vrp.build_rows([15.0] * 60, times, _alternating(60), times, horizon=5)
    independent = vrp.cycles(rows, horizon=5)
    assert len(independent) == math.ceil(len(rows) / 5)
    gaps = {(b.day - a.day).days for a, b in itertools.pairwise(independent)}
    assert gaps == {5}


def test_max_drawdown_hand_computed():
    # cumulative: 2, 1, 4, 0, 1 -> peak 4, trough 0 -> worst fall 4
    assert vrp.max_drawdown([2.0, -1.0, 3.0, -4.0, 1.0]) == pytest.approx(4.0)
    assert vrp.max_drawdown([]) == 0.0
    assert vrp.max_drawdown([1.0, 2.0]) == 0.0          # never gives anything back


def test_drawdown_is_measured_on_independent_cycles_only():
    rows = [vrp.VrpRow(day=datetime(2020, 1, 1, tzinfo=UTC).date() + timedelta(days=i),
                       implied=10.0, realized=10.0 - v)
            for i, v in enumerate([1.0, 1.0, -9.0, 1.0, 1.0, 1.0])]
    summary = vrp.VrpSummary("IDX", "SYM", rows, vrp.cycles(rows, horizon=3))
    # the -9 sits at index 2, which the 3-step cycle sampling skips
    assert [r.vrp for r in summary.independent] == [1.0, 1.0]
    assert summary.drawdown == 0.0


# --- the cost hurdle ---

def test_cost_hurdle_is_implied_times_roundtrip():
    """Maturity and spot cancel out of premium*roundtrip/vega for a near-ATM
    option, leaving sigma*roundtrip. Pinning it keeps the derivation honest."""
    assert vrp.cost_hurdle(20.0, 0.03) == pytest.approx(0.6)
    assert vrp.cost_hurdle(20.0, 0.06) == pytest.approx(1.2)
    assert vrp.cost_hurdle(20.0, 0.0) == 0.0


def test_empty_summary_cannot_clear():
    assert not vrp.VrpSummary("IDX", "SYM", [], []).clears()
    assert vrp.VrpSummary("IDX", "SYM", [], []).by_regime() == []


def test_by_regime_splits_on_implied_terciles():
    times = _days(90)
    implied = [10.0] * 30 + [20.0] * 30 + [40.0] * 30
    rows = vrp.build_rows(implied, times, _alternating(90), times, horizon=5)
    labels = {label: n for label, n, _, _ in vrp.VrpSummary(
        "IDX", "SYM", rows, []).by_regime()}
    assert set(labels) == {"calm", "normal", "stressed"}
    assert sum(labels.values()) == len(rows)


def test_by_regime_reports_the_worst_case_not_only_the_mean():
    """Ranking regimes by mean alone reads as "sell when implied is already
    high". The catastrophes start in the CALM bucket, because a seller is short
    the transition — so the worst column has to be there to be read alongside."""
    day = datetime(2020, 1, 1, tzinfo=UTC).date()
    calm = [vrp.VrpRow(day + timedelta(days=i), 10.0, 9.0) for i in range(4)]
    blowup = [vrp.VrpRow(day + timedelta(days=90), 10.0, 80.0)]     # calm entry
    # Distinct levels so the tercile cut actually separates them: the bucket
    # test is `implied > high`, so a flat block sitting exactly on the boundary
    # would all land in `normal`.
    stressed = [vrp.VrpRow(day + timedelta(days=100 + i), 40.0 + i, 35.0 + i)
                for i in range(5)]
    regimes = {label: (mean, worst) for label, _, mean, worst
               in vrp.VrpSummary("IDX", "SYM", calm + blowup + stressed, []).by_regime()}
    assert regimes["stressed"][0] > regimes["calm"][0]      # calm looks better on mean
    assert regimes["calm"][1] == pytest.approx(-70.0)       # and is where ruin lives
    assert regimes["stressed"][1] == pytest.approx(5.0)


# --- CBOE parsing and the directory guard ---

def test_parse_cboe_csv_reads_dates_and_closes():
    text = ("DATE,OPEN,HIGH,LOW,CLOSE\n"
            "01/02/1990,17.24,17.24,17.24,17.24\n"
            "08/07/2026,15.30,15.36,14.77,14.90\n")
    closes, times = data.parse_cboe_csv(text)
    assert closes == [17.24, 14.90]
    assert [t.date().isoformat() for t in times] == ["1990-01-02", "2026-08-07"]


def test_parse_cboe_csv_drops_unusable_rows():
    """A 0.0 close would read as zero implied vol, i.e. a huge premium, rather
    than as the missing data it actually is."""
    text = ("DATE,OPEN,HIGH,LOW,CLOSE\n"
            "01/02/1990,1,1,1,17.24\n"
            "01/03/1990,1,1,1,0.00\n"        # placeholder
            "01/04/1990,1,1,1,\n"            # blank
            "not-a-date,1,1,1,12.00\n"       # unparseable
            "01/05/1990,1,1,1,18.50\n")
    closes, _ = data.parse_cboe_csv(text)
    assert closes == [17.24, 18.50]


def test_vrp_bars_and_daily_cache_are_separate_directories(tmp_path: Path):
    """The daily/ cache is quarantined to end before the holdout window and is
    read by robustness.daily_regime_check. VRP underlying bars run to the
    present, so the two must never share a directory."""
    # Round values so the 6-decimal CSV format roundtrips exactly.
    closes, times = [100.0, 101.5, 99.25, 102.0], _days(4)
    data.save_bars(tmp_path / "daily" / "SPY.csv", closes, times)
    assert data.load_vrp_bars("SPY", data_dir=tmp_path) == ([], [])

    data.save_bars(tmp_path / "vrp" / "SPY.csv", closes, times)
    assert data.load_vrp_bars("SPY", data_dir=tmp_path) == (closes, times)
    assert data.load_vol_index("VIX", data_dir=tmp_path) == ([], [])
