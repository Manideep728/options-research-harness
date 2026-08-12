"""Trade-level scoring: Sharpe, probabilistic Sharpe, deflated Sharpe.

The deflated Sharpe ratio (Bailey & Lopez de Prado) answers the question the
research loop must keep answering honestly: "is this Sharpe better than the
best result N random strategies would have produced?" It deflates by two
things: the number of trials taken from the registry (more tries -> higher
bar) and the non-normality of the returns (fat tails -> less trust).

All Sharpes here are per-trade, not annualized — candidates are only ever
compared against each other on the same windows, so the scale cancels out.

Clustering: every formula below assumes independent observations, and trades on
a 30-name universe of near-100%-beta tickers are not independent. The pending
candidate's 32 validation trades sat on 26 distinct entry bars — one bar held
four of them, and three of the four largest winners shared a single bar. Those
are one market move, not three draws, and counting them as three inflates the
sqrt(n-1) term that probabilistic_sharpe uses to express confidence. Callers
pass returns through cluster_returns() keyed on the entry bar first.
"""

import math
from collections.abc import Hashable, Sequence
from statistics import NormalDist, fmean, pstdev, pvariance

EULER_GAMMA = 0.5772156649015329
_NORMAL = NormalDist()


def cluster_returns(returns: Sequence[float],
                    keys: Sequence[Hashable]) -> list[float]:
    """Average returns that share a key, preserving first-seen order.

    The key is the entry bar. Averaging within a bar is the conservative
    reading — it treats simultaneous trades as perfectly correlated, which for
    SPY/IWM/XLF firing on the same 15-minute bar is very close to true. The
    alternative (a Kish design effect with an estimated intra-cluster
    correlation) needs a parameter this repo has no honest way to fit.

    Mismatched lengths are a caller bug, not a data condition, so this raises
    rather than silently truncating a score everything downstream trusts.
    """
    if len(returns) != len(keys):
        raise ValueError(f"returns/keys length mismatch: {len(returns)} vs {len(keys)}")
    grouped: dict[Hashable, list[float]] = {}
    for value, key in zip(returns, keys, strict=True):
        grouped.setdefault(key, []).append(value)
    return [fmean(values) for values in grouped.values()]


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


def deflated_sharpe(returns: list[float], trial_sharpes: list[float],
                    n_trials: int | None = None) -> float:
    """PSR against the expected-max-Sharpe of everything ever tried.
    > 0.95 means: fewer than 5% odds this is just the luckiest of N tries.

    The benchmark has two independent inputs and they come from different
    places. N is how many attempts happened, including attempts scored on a
    measurement model that has since been replaced — those were still chances
    to get lucky. `trial_sharpes` estimates how much a Sharpe varies between
    candidates, which is a property of the current model only. Pass `n_trials`
    to keep the count honest while the spread stays comparable; it defaults to
    len(trial_sharpes) for callers that want both from the same list.
    """
    n = len(trial_sharpes) if n_trials is None else n_trials
    if len(trial_sharpes) >= 2 and n >= 2:
        benchmark = expected_max_sharpe(n, pvariance(trial_sharpes))
    else:
        benchmark = 0.0
    return probabilistic_sharpe(returns, benchmark)


def summarize(returns: list[float],
              keys: Sequence[Hashable] | None = None) -> dict:
    """Registry-ready score dict (finite floats only — JSONL-safe).

    `keys` are the trades' entry bars. When given, the Sharpe is computed on
    bar-clustered returns while expectancy and the trade count stay raw — the
    mean is unbiased either way, but the confidence in it is not.
    """
    clustered = cluster_returns(returns, keys) if keys is not None else returns
    out = {
        "trades": len(returns),
        "expectancy": round(fmean(returns), 6) if returns else 0.0,
        "sharpe": round(sharpe(clustered), 6),
    }
    if keys is not None:
        out["effective_trades"] = len(clustered)
    return out
