"""Trade-level scoring: Sharpe, probabilistic Sharpe, deflated Sharpe.

The deflated Sharpe ratio (Bailey & Lopez de Prado) answers the question the
research loop must keep answering honestly: "is this Sharpe better than the
best result N random strategies would have produced?" It deflates by two
things: the number of trials taken from the registry (more tries -> higher
bar) and the non-normality of the returns (fat tails -> less trust).

All Sharpes here are per-trade, not annualized — candidates are only ever
compared against each other on the same windows, so the scale cancels out.
"""

import math
from statistics import NormalDist, fmean, pstdev, pvariance

EULER_GAMMA = 0.5772156649015329
_NORMAL = NormalDist()


def sharpe(returns: list[float]) -> float:
    """Mean/stdev of per-trade returns. 0.0 when undefined (too few trades
    or zero variance) — a degenerate candidate scores as 'no evidence'."""
    if len(returns) < 2:
        return 0.0
    sd = pstdev(returns)
    if sd == 0:
        return 0.0
    return fmean(returns) / sd


def _skew(returns: list[float]) -> float:
    sd = pstdev(returns)
    if sd == 0:
        return 0.0
    m = fmean(returns)
    return fmean([(r - m) ** 3 for r in returns]) / sd**3


def _kurtosis(returns: list[float]) -> float:
    """Plain (non-excess) kurtosis; a normal distribution scores 3."""
    sd = pstdev(returns)
    if sd == 0:
        return 3.0
    m = fmean(returns)
    return fmean([(r - m) ** 4 for r in returns]) / sd**4


def expected_max_sharpe(n_trials: int, var_across_trials: float) -> float:
    """E[max Sharpe] among n_trials skill-less candidates whose Sharpe
    estimates vary by var_across_trials. This is the benchmark a real
    candidate must beat: with enough tries, SOME random strategy always
    looks this good."""
    if n_trials <= 1 or var_across_trials <= 0:
        return 0.0
    sd = math.sqrt(var_across_trials)
    return sd * (
        (1 - EULER_GAMMA) * _NORMAL.inv_cdf(1 - 1 / n_trials)
        + EULER_GAMMA * _NORMAL.inv_cdf(1 - 1 / (n_trials * math.e))
    )


def probabilistic_sharpe(returns: list[float], benchmark_sr: float) -> float:
    """P(true Sharpe > benchmark | observed returns), adjusting the estimate's
    error for sample size, skew, and kurtosis. In [0, 1]."""
    n = len(returns)
    if n < 2:
        return 0.0
    sr = sharpe(returns)
    denom = math.sqrt(
        max(1e-12, 1 - _skew(returns) * sr + (_kurtosis(returns) - 1) / 4 * sr**2)
    )
    return _NORMAL.cdf((sr - benchmark_sr) * math.sqrt(n - 1) / denom)


def deflated_sharpe(returns: list[float], trial_sharpes: list[float]) -> float:
    """PSR against the expected-max-Sharpe of everything ever tried.
    > 0.95 means: fewer than 5% odds this is just the luckiest of N tries."""
    if len(trial_sharpes) >= 2:
        benchmark = expected_max_sharpe(len(trial_sharpes), pvariance(trial_sharpes))
    else:
        benchmark = 0.0
    return probabilistic_sharpe(returns, benchmark)


def summarize(returns: list[float]) -> dict:
    """Registry-ready score dict (finite floats only — JSONL-safe)."""
    return {
        "trades": len(returns),
        "expectancy": round(fmean(returns), 6) if returns else 0.0,
        "sharpe": round(sharpe(returns), 6),
    }
