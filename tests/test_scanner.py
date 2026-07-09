"""Scoring/ranking tests. Pure logic, no broker."""

from bot.config import Settings
from bot.scanner import _recent_volatility, rank_symbols, score_symbol

CFG = Settings(api_key="k", secret_key="s")


def steady_uptrend(n: int = 80, step: float = 0.5) -> list[float]:
    """Monotonic rise: clear EMA uptrend, but RSI pins near 100 (overbought),
    far from the 45 bull trigger."""
    return [100.0 + step * i for i in range(n)]


def primed_uptrend(amp: float = 1.0) -> list[float]:
    """Uptrend, then a pullback, then a recovery bar — RSI dips and crosses
    back up through ~45. This is the setup the strategy actually trades."""
    closes = [100.0 + 0.3 * amp * (i + 1) for i in range(60)]
    px = closes[-1]
    closes += [px - 1.0 * amp * (i + 1) for i in range(6)]
    closes += [closes[-1] + 3.0 * amp]
    return closes


def test_flat_market_scores_zero():
    # Equal EMAs -> no trend -> no trigger in play -> zero.
    assert score_symbol("FLAT", [100.0] * 80, CFG).score == 0.0


def test_insufficient_bars_scores_zero():
    assert score_symbol("SHORT", [100.0, 101.0, 102.0], CFG).score == 0.0


def test_primed_beats_overbought_in_same_uptrend():
    # Both are uptrends with real EMA separation; the ONLY thing that should
    # separate them is RSI proximity to the trigger. If the score ignored
    # proximity, the steeper steady trend could win — this catches that.
    primed = score_symbol("PRIMED", primed_uptrend(), CFG).score
    overbought = score_symbol("OB", steady_uptrend(), CFG).score
    assert primed > overbought


def test_more_movement_scores_higher():
    # Same shape, bigger swings -> more tradeable movement -> higher score.
    small = score_symbol("SMALL", primed_uptrend(amp=1.0), CFG).score
    big = score_symbol("BIG", primed_uptrend(amp=2.0), CFG).score
    assert big > small


def test_recent_volatility_higher_for_choppier_series():
    smooth = [100.0 + i for i in range(20)]          # ~constant tiny returns
    choppy = [100, 112, 96, 118, 92, 121, 90, 125,   # violent swings
              88, 130, 85, 133, 84, 136, 82, 140]
    assert _recent_volatility(choppy, 14) > _recent_volatility(smooth, 14)


def test_rank_orders_best_first_and_breaks_ties():
    closes = {
        "AAA": [100.0] * 80,          # flat -> 0.0
        "BBB": primed_uptrend(),      # highest
        "CCC": [100.0] * 80,          # flat -> 0.0 (ties AAA)
    }
    cfg = Settings(api_key="k", secret_key="s", symbols=("CCC", "AAA", "BBB"))
    ranked = rank_symbols(closes, cfg)
    # Best score first; the two zero-score names tie and break alphabetically.
    assert [s.symbol for s in ranked] == ["BBB", "AAA", "CCC"]
