"""The `null --window daily` path, and the unit conversion it used to skip.

The bug: research/robustness.py rescales per-bar parameters before it crosses
from 15-minute bars to daily bars, and the CLI's own daily null did not. A
volatility threshold of 0.002 is calm for a 15-minute bar and unreachable for a
daily one, so the candidate line printed next to the null matched almost
nothing while reporting a number as if it had.
"""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from research import cli, data


def _daily_cache(tmp_path: Path, symbols=("SPY",), n=400) -> None:
    start = datetime(2023, 1, 3, 5, 0, tzinfo=UTC)   # 00:00 ET
    for sym in symbols:
        closes, times = [], []
        price = 100.0
        for i in range(n):
            price *= 1.01 if i % 3 else 0.992
            closes.append(price)
            times.append(start + timedelta(days=i))
        data.save_bars(tmp_path / "daily" / f"{sym}.csv", closes, times)


def _candidate(tmp_path: Path, signal_params: dict, filters: list[str]) -> Path:
    path = tmp_path / "candidate.json"
    path.write_text(json.dumps({
        "family": "spec:test",
        "signal_params": signal_params,
        "exit_params": {"take_profit_pct": 0.5, "stop_loss_pct": 0.25},
        "spec": {"name": "test", "hypothesis": "h", "trend": "none",
                 "trigger": "rsi_cross", "filters": filters,
                 "grid": {"rsi_bull_level": [40.0], "rsi_bear_level": [60.0]}},
    }), encoding="utf-8")
    return path


def _run(tmp_path: Path, candidate: Path, extra=()) -> int:
    return cli.main(["--data-dir", str(tmp_path), "--candidate", str(candidate),
                     "--registry", str(tmp_path / "t.jsonl"),
                     "null", "--window", "daily", "--seeds", "3",
                     "--trades", "40", *extra])


def test_daily_null_rescales_a_per_bar_volatility_threshold(tmp_path, capsys):
    """0.002 per 15-minute bar becomes about 0.010 on daily bars, because return
    stdev grows with the square root of the aggregation period."""
    _daily_cache(tmp_path)
    candidate = _candidate(
        tmp_path,
        {"rsi_bull_level": 40.0, "rsi_bear_level": 60.0, "max_entry_vol": 0.002},
        ["max_entry_vol"])
    _run(tmp_path, candidate)
    out = capsys.readouterr().out
    assert "rescaled per-bar params for daily bars" in out
    assert "max_entry_vol 0.002 ->" in out


def test_daily_null_refuses_a_param_with_no_daily_meaning(tmp_path, capsys):
    """entry_hours cannot be judged on daily bars at all: Alpaca stamps them at
    00:00 ET, so an hour filter matches every bar or none. Reporting that as a
    plain result would read as evidence."""
    _daily_cache(tmp_path)
    candidate = _candidate(
        tmp_path,
        {"rsi_bull_level": 40.0, "rsi_bear_level": 60.0, "entry_hours": [14, 15]},
        ["entry_hours"])
    assert _run(tmp_path, candidate) == 1
    out = capsys.readouterr().out
    assert "NOT EVALUABLE" in out
    assert "entry_hours" in out


def test_daily_null_leaves_bar_counts_alone(tmp_path, capsys):
    """A lookback of 20 BARS is 20 bars on any timeframe. Only per-bar
    quantities get the square-root conversion."""
    _daily_cache(tmp_path)
    candidate = _candidate(
        tmp_path, {"rsi_bull_level": 40.0, "rsi_bear_level": 60.0}, [])
    _run(tmp_path, candidate)
    assert "rescaled" not in capsys.readouterr().out
