from bot.config import Settings
from bot.strategy import Action, evaluate

CFG = Settings(api_key="", secret_key="")


def _uptrend_with_rsi_bounce() -> list[float]:
    """Long uptrend, a pullback deep enough to drag RSI under 35 but shallow
    enough that EMA9 stays above EMA21, then a strong recovery bar that
    crosses RSI back up through 35 (verified: RSI 34.9 -> 54.1)."""
    closes = [100.0 + 0.3 * (i + 1) for i in range(60)]  # drift up to 118
    px = closes[-1]
    closes += [px - 1.0 * (i + 1) for i in range(6)]  # pullback to 112
    closes += [closes[-1] + 3.0]  # recovery bar -> RSI crosses up
    return closes


def _downtrend_with_rsi_fade() -> list[float]:
    """Exact mirror of the bounce fixture around 150."""
    return [200.0 - (c - 100.0) for c in _uptrend_with_rsi_bounce()]


def test_uptrend_rsi_bounce_buys_call():
    signal = evaluate(_uptrend_with_rsi_bounce(), CFG)
    assert signal.action == Action.BUY_CALL
    assert "uptrend" in signal.reason


def test_downtrend_rsi_fade_buys_put():
    signal = evaluate(_downtrend_with_rsi_fade(), CFG)
    assert signal.action == Action.BUY_PUT


def test_steady_uptrend_without_dip_is_no_trade():
    closes = [100.0 + 0.5 * i for i in range(80)]
    signal = evaluate(closes, CFG)
    assert signal.action == Action.NONE


def test_oversold_but_downtrend_is_no_trade():
    # RSI bounce happens, but EMAs are bearish -> trend filter blocks the call.
    closes = [200.0 - 1.5 * i for i in range(60)]
    closes += [closes[-1] + 8.0]  # one big up bar
    signal = evaluate(closes, CFG)
    assert signal.action != Action.BUY_CALL


def test_not_enough_bars_is_no_trade():
    signal = evaluate([100.0] * 10, CFG)
    assert signal.action == Action.NONE
    assert "not enough bars" in signal.reason


def test_reason_always_populated():
    for closes in (_uptrend_with_rsi_bounce(), [100.0] * 80):
        assert evaluate(closes, CFG).reason
