"""Pure indicator math. Input: list of closes (oldest -> newest). No I/O."""


def ema(closes: list[float], period: int) -> list[float]:
    """Exponential moving average. Returns a list aligned with `closes`;
    the first `period - 1` entries are seeded with a simple average ramp."""
    if period <= 0:
        raise ValueError("period must be positive")
    if len(closes) < period:
        return []

    k = 2.0 / (period + 1)
    out: list[float] = []
    # Seed: SMA of the first `period` closes, then standard EMA recursion.
    sma_seed = sum(closes[:period]) / period
    for i, price in enumerate(closes):
        if i < period - 1:
            out.append(sum(closes[: i + 1]) / (i + 1))
        elif i == period - 1:
            out.append(sma_seed)
        else:
            out.append(price * k + out[-1] * (1 - k))
    return out


def rsi(closes: list[float], period: int = 14) -> list[float]:
    """Wilder's RSI. Returns a list aligned with `closes`; entries before
    the first computable value are set to 50.0 (neutral)."""
    if period <= 0:
        raise ValueError("period must be positive")
    if len(closes) < period + 1:
        return []

    out: list[float] = [50.0] * len(closes)
    gains = 0.0
    losses = 0.0
    for i in range(1, period + 1):
        change = closes[i] - closes[i - 1]
        if change > 0:
            gains += change
        else:
            losses -= change
    avg_gain = gains / period
    avg_loss = losses / period
    out[period] = _rsi_value(avg_gain, avg_loss)

    for i in range(period + 1, len(closes)):
        change = closes[i] - closes[i - 1]
        gain = max(change, 0.0)
        loss = max(-change, 0.0)
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        out[i] = _rsi_value(avg_gain, avg_loss)
    return out


def _rsi_value(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)
