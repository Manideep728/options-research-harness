"""Tests for the self-improvement guardrails: clamping, non-tunable keys,
rejection on thin evidence, and the tuned-file round trip."""

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from bot.config import Settings, apply_tuned_params, clamp_tunables
from bot.simulator import SimResult, SimTrade
from bot.tuner import MIN_TRADES, TuneOutcome, candidate_params, tune, write_tuned_params

T0 = datetime(2026, 1, 5, 15, 0, tzinfo=UTC)


# --- clamping (guideline 2) ---

def test_out_of_bounds_values_are_clamped():
    out = clamp_tunables({"rsi_bull_level": 5.0, "take_profit_pct": 5.0})
    assert out["rsi_bull_level"] == 25.0   # floor of (25, 45)
    assert out["take_profit_pct"] == 0.80  # ceiling of (0.30, 0.80)


def test_risk_caps_are_not_tunable():
    """The hard guideline: risk parameters pass through clamp untouched-out."""
    out = clamp_tunables({
        "max_trades_per_day": 500,
        "max_premium_pct_of_equity": 0.90,
        "daily_loss_limit_pct": 1.0,
        "stop_loss_pct": 0.25,
    })
    assert set(out) == {"stop_loss_pct"}  # only the tunable survived


def test_degenerate_ema_pair_dropped():
    out = clamp_tunables({"ema_fast": 15, "ema_slow": 18, "rsi_bull_level": 30})
    assert "ema_fast" not in out and "ema_slow" not in out
    assert out["rsi_bull_level"] == 30


def test_all_grid_candidates_are_within_bounds():
    from bot.config import TUNABLE_BOUNDS
    for params in candidate_params():
        for key, value in params.items():
            lo, hi = TUNABLE_BOUNDS[key]
            assert lo <= value <= hi


# --- tuned-file loading ---

def test_tuned_file_round_trip_applies_params(tmp_path):
    path = tmp_path / "tuned.json"
    best = SimResult(trades=[SimTrade(T0, T0, "call", 1, 1, 0.1, "x")] * 40)
    outcome = TuneOutcome(True, "ok", {"rsi_bull_level": 40.0, "take_profit_pct": 0.60},
                          SimResult(), best)
    write_tuned_params(str(path), outcome)
    cfg = apply_tuned_params(Settings(api_key="", secret_key=""), str(path))
    assert cfg.rsi_bull_level == 40.0
    assert cfg.take_profit_pct == 0.60
    assert cfg.max_trades_per_day == 3  # untouched


def test_hand_edited_file_cannot_escape_bounds(tmp_path):
    path = tmp_path / "tuned.json"
    path.write_text(json.dumps({"params": {
        "stop_loss_pct": 0.99,          # trying to loosen the stop
        "max_trades_per_day": 100,      # trying to raise a risk cap
    }}))
    cfg = apply_tuned_params(Settings(api_key="", secret_key=""), str(path))
    assert cfg.stop_loss_pct == 0.35    # clamped to ceiling
    assert cfg.max_trades_per_day == 3  # risk cap ignored entirely


def test_corrupt_tuned_file_ignored(tmp_path):
    path = tmp_path / "tuned.json"
    path.write_text("{not json")
    cfg = apply_tuned_params(Settings(api_key="", secret_key=""), str(path))
    assert cfg == Settings(api_key="", secret_key="")


def test_missing_tuned_file_ignored(tmp_path):
    cfg = apply_tuned_params(Settings(api_key="", secret_key=""), str(tmp_path / "nope.json"))
    assert cfg.rsi_bull_level == 45.0


# --- rejection on thin evidence (guideline 3) ---

def test_tune_rejects_when_not_enough_trades():
    closes = [100.0 + 0.1 * i for i in range(120)]  # steady drift, no signals
    times = [T0 + timedelta(minutes=15 * i) for i in range(len(closes))]
    outcome = tune({"SPY": (closes, times)}, Settings(api_key="", secret_key=""))
    assert not outcome.accepted
    assert outcome.best_val is None or outcome.best_val.n < MIN_TRADES


# --- selection bias (guideline 4): winner must be picked on TRAIN, not val ---

def test_winner_is_selected_on_train_not_validation(monkeypatch):
    """Regression test for a data-snooping bug: the tuner used to rank
    candidates by validation expectancy, so the reported "evidence" was
    selected on the same data it was validated against. Here candidate B
    looks better on validation but worse on train; the fix must pick A (best
    on train) and must never even look at B's validation result."""
    import bot.tuner as tuner_mod

    params_a = {"rsi_bull_level": 40.0}   # best on TRAIN
    params_b = {"rsi_bull_level": 45.0}   # best on VALIDATION (the trap)

    results = {
        ("train", 40.0): FakeResult(n=40, expectancy=0.20),
        ("val", 40.0): FakeResult(n=40, expectancy=0.02),
        ("train", 45.0): FakeResult(n=40, expectancy=0.05),
        ("val", 45.0): FakeResult(n=40, expectancy=0.30),
        ("val", 50.0): FakeResult(n=40, expectancy=0.0),  # current params' baseline
    }
    calls: list[tuple[str, float]] = []

    def fake_run_all(windows, cfg, sp):
        which = next(iter(windows.values()))  # "train" or "val" marker string
        calls.append((which, cfg.rsi_bull_level))
        return results[(which, cfg.rsi_bull_level)]

    monkeypatch.setattr(tuner_mod, "candidate_params", lambda: [params_a, params_b])
    monkeypatch.setattr(
        tuner_mod, "split_windows",
        lambda bars: tuner_mod.Windows(train={"SPY": "train"}, val={"SPY": "val"}),
    )
    monkeypatch.setattr(tuner_mod, "run_all", fake_run_all)

    cfg = Settings(api_key="", secret_key="", rsi_bull_level=50.0)
    outcome = tuner_mod.tune({"SPY": ([], [])}, cfg)

    assert outcome.params == params_a
    assert outcome.accepted
    assert ("val", 45.0) not in calls  # B's validation was never evaluated


@dataclass
class FakeResult:
    n: int
    expectancy: float
