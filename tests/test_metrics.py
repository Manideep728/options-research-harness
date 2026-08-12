"""Sharpe / probabilistic Sharpe / deflated Sharpe math."""

import math

import pytest

from research import metrics


def test_sharpe_known_value():
    # mean=0.1, pstdev=0.1 -> sharpe 1.0
    assert math.isclose(metrics.sharpe([0.0, 0.2, 0.0, 0.2]), 1.0)


def test_sharpe_degenerate_inputs():
    assert metrics.sharpe([]) == 0.0
    assert metrics.sharpe([0.5]) == 0.0
    assert metrics.sharpe([0.1, 0.1, 0.1]) == 0.0  # zero variance


def test_expected_max_sharpe_hand_computed():
    # n=100, var=0.01: sd=0.1, z1=invcdf(0.99)=2.3263, z2=invcdf(1-1/(100e))=2.6800
    # E = 0.1 * ((1-y)*z1 + y*z2) with y=0.5772 -> ~0.253
    value = metrics.expected_max_sharpe(100, 0.01)
    assert math.isclose(value, 0.253, abs_tol=0.005)


def test_expected_max_sharpe_grows_with_trials():
    v10 = metrics.expected_max_sharpe(10, 0.01)
    v1000 = metrics.expected_max_sharpe(1000, 0.01)
    assert 0 < v10 < v1000


def test_expected_max_sharpe_edge_cases():
    assert metrics.expected_max_sharpe(1, 0.01) == 0.0
    assert metrics.expected_max_sharpe(100, 0.0) == 0.0


def test_probabilistic_sharpe_bounds_and_direction():
    good = [0.05, 0.10, -0.02, 0.08, 0.03, 0.06, -0.01, 0.07] * 5
    psr = metrics.probabilistic_sharpe(good, benchmark_sr=0.0)
    assert 0.5 < psr <= 1.0
    # against an unbeatable benchmark it collapses
    assert metrics.probabilistic_sharpe(good, benchmark_sr=10.0) < 0.01


def test_more_evidence_raises_confidence():
    returns = [0.05, -0.02, 0.08, 0.01]
    small = metrics.probabilistic_sharpe(returns * 2, 0.0)
    large = metrics.probabilistic_sharpe(returns * 20, 0.0)
    assert large > small


def test_deflated_sharpe_shrinks_as_trials_pile_up():
    returns = [0.05, -0.02, 0.08, 0.01, 0.04, -0.03, 0.06, 0.02] * 4
    few_trials = [0.1, -0.1]
    many_trials = [0.1, -0.1, 0.3, -0.2, 0.25, 0.05, -0.15, 0.2] * 30
    assert metrics.deflated_sharpe(returns, many_trials) < \
        metrics.deflated_sharpe(returns, few_trials)


def test_deflated_sharpe_counts_n_apart_from_the_spread():
    """N and the spread are separate inputs: holding the spread fixed, a larger
    N must still raise the bar. Regression for the two being read off one list,
    which forced discarded trials to be dropped from N to drop them from sd."""
    returns = [0.05, -0.02, 0.08, 0.01, 0.04, -0.03, 0.06, 0.02] * 4
    spread = [0.1, -0.1, 0.3, -0.2, 0.25, 0.05, -0.15, 0.2]
    assert metrics.deflated_sharpe(returns, spread, n_trials=2000) < \
        metrics.deflated_sharpe(returns, spread, n_trials=8)


def test_deflated_sharpe_n_defaults_to_the_spread_length():
    returns = [0.05, -0.02, 0.08, 0.01] * 8
    spread = [0.1, -0.1, 0.3, -0.2]
    assert metrics.deflated_sharpe(returns, spread) == \
        metrics.deflated_sharpe(returns, spread, n_trials=len(spread))


def test_summarize_is_registry_safe():
    out = metrics.summarize([0.1, -0.05])
    assert out["trades"] == 2
    assert all(isinstance(v, (int, float)) for v in out.values())
    assert metrics.summarize([]) == {"trades": 0, "expectancy": 0.0, "sharpe": 0.0}


# --- clustering correlated trades ---

def test_cluster_returns_averages_same_bar_trades():
    """SPY/IWM/XLF firing on one 15-min bar is one market move. Averaging them
    is the conservative reading; counting them as three inflates every
    confidence claim downstream."""
    returns = [3.0, 2.0, 1.0, 0.5]
    keys = ["bar-a", "bar-a", "bar-a", "bar-b"]
    assert metrics.cluster_returns(returns, keys) == [2.0, 0.5]


def test_cluster_returns_is_order_stable_and_identity_on_unique_keys():
    returns = [0.3, -0.1, 0.2]
    assert metrics.cluster_returns(returns, ["c", "a", "b"]) == returns
    assert metrics.cluster_returns([], []) == []


def test_cluster_returns_rejects_mismatched_lengths():
    """A silent truncation here would quietly corrupt a score the gate trusts."""
    with pytest.raises(ValueError, match="length mismatch"):
        metrics.cluster_returns([0.1, 0.2], ["only-one"])


def test_clustering_lowers_confidence_it_should_not_have_had():
    """The real shape of the pending candidate: a handful of big winners
    sharing one entry bar. Clustering must not raise the probabilistic Sharpe,
    because nothing about clustering adds evidence."""
    returns = [2.4, 2.3, 2.2] + [-0.2] * 20     # 3 winners, all the same bar
    keys = ["same"] * 3 + [f"bar-{i}" for i in range(20)]
    clustered = metrics.cluster_returns(returns, keys)
    assert len(clustered) == 21
    assert metrics.probabilistic_sharpe(clustered, 0.0) <= \
        metrics.probabilistic_sharpe(returns, 0.0)


def test_summarize_reports_effective_trades_only_when_clustering():
    returns = [0.5, -0.25, 0.5, -0.25]
    keys = ["a", "a", "b", "c"]
    with_keys = metrics.summarize(returns, keys)
    assert with_keys["trades"] == 4              # raw count is still the truth
    assert with_keys["effective_trades"] == 3    # but only 3 independent bars
    assert with_keys["expectancy"] == 0.125      # the mean is unbiased either way
    assert "effective_trades" not in metrics.summarize(returns)
