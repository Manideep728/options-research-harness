"""Holdout gate: verdict logic and — above all — burn-once refusal."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

from bot.config import Settings
from research import data, gate, registry


def _cfg(**kw) -> Settings:
    return Settings(api_key="k", secret_key="s", **kw)


def _write_holdout(tmp_path: Path, symbol="AAA", n=400):
    start = datetime(2026, 5, 1, 14, 30, tzinfo=UTC)
    times = [start + timedelta(minutes=15 * i) for i in range(n)]
    closes = []
    while len(closes) < n:
        closes += [100.0 + j for j in range(12)]
        closes += [111.0 - j for j in range(12)]
    data.save_bars(tmp_path / "holdout" / f"{symbol}.csv", closes[:n], times)


CANDIDATE = {
    "family": "rsi_only",
    "signal_params": {"rsi_bull_level": 40.0, "rsi_bear_level": 60.0},
    "exit_params": {"take_profit_pct": 0.5, "stop_loss_pct": 0.25},
}


def test_gate_without_holdout_data(tmp_path):
    outcome = gate.run_gate(CANDIDATE, _cfg(symbols=("AAA",)),
                            data_dir=tmp_path, registry_path=tmp_path / "t.jsonl")
    assert not outcome.passed and "no holdout" in outcome.reason


def test_gate_runs_once_then_refuses(tmp_path):
    _write_holdout(tmp_path)
    cfg = _cfg(symbols=("AAA",))
    reg = tmp_path / "trials.jsonl"

    first = gate.run_gate(CANDIDATE, cfg, data_dir=tmp_path, registry_path=reg)
    assert not first.refused
    assert first.window_id  # a real window id was computed and burned

    second = gate.run_gate(CANDIDATE, cfg, data_dir=tmp_path, registry_path=reg)
    assert second.refused
    assert "burned" in second.reason
    # refusal renders no verdict and does not double-log
    gates = [e for e in registry.entries(reg) if e["kind"] == "gate"]
    assert len(gates) == 1


def test_gate_burns_even_on_failure(tmp_path):
    _write_holdout(tmp_path, n=100)  # too few bars -> too few trades -> fail
    cfg = _cfg(symbols=("AAA",))
    reg = tmp_path / "trials.jsonl"
    outcome = gate.run_gate(CANDIDATE, cfg, data_dir=tmp_path, registry_path=reg)
    assert not outcome.refused and not outcome.passed
    assert registry.gate_burned(reg, outcome.window_id)


def test_gate_verdict_gates_on_dsr(tmp_path, monkeypatch):
    _write_holdout(tmp_path)
    cfg = _cfg(symbols=("AAA",))
    reg = tmp_path / "trials.jsonl"
    monkeypatch.setattr(gate, "MIN_TRADES", 1)

    outcome = gate.run_gate(CANDIDATE, cfg, data_dir=tmp_path, registry_path=reg)
    assert not outcome.refused
    if outcome.result is not None and outcome.result.expectancy > 0:
        # positive expectancy alone must NOT pass unless DSR clears 0.95
        assert outcome.passed == (outcome.deflated_sharpe >= gate.DSR_CONFIDENCE)
