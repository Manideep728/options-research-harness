import pytest

from bot.indicators import ema, rsi


def test_ema_constant_series_equals_price():
    closes = [50.0] * 30
    result = ema(closes, 9)
    assert result[-1] == pytest.approx(50.0)


def test_ema_rises_with_uptrend_and_tracks_below_price():
    closes = [float(i) for i in range(1, 31)]
    result = ema(closes, 9)
    assert result[-1] > result[-10]
    assert result[-1] < closes[-1]  # EMA lags a rising price


def test_ema_fast_reacts_quicker_than_slow():
    closes = [100.0] * 20 + [110.0] * 10
    fast = ema(closes, 9)[-1]
    slow = ema(closes, 21)[-1]
    assert fast > slow


def test_ema_insufficient_data_returns_empty():
    assert ema([1.0, 2.0], 9) == []


def test_ema_rejects_bad_period():
    with pytest.raises(ValueError):
        ema([1.0] * 20, 0)


def test_rsi_all_gains_is_100():
    closes = [float(i) for i in range(1, 31)]
    assert rsi(closes, 14)[-1] == pytest.approx(100.0)


def test_rsi_all_losses_near_0():
    closes = [float(i) for i in range(31, 1, -1)]
    assert rsi(closes, 14)[-1] == pytest.approx(0.0, abs=1e-6)


def test_rsi_known_wilder_value():
    # Classic Wilder example dataset (14-period): expected first RSI ~70.46
    closes = [
        44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42,
        45.84, 46.08, 45.89, 46.03, 45.61, 46.28, 46.28,
    ]
    result = rsi(closes, 14)
    assert result[14] == pytest.approx(70.46, abs=0.1)


def test_rsi_insufficient_data_returns_empty():
    assert rsi([1.0] * 14, 14) == []
