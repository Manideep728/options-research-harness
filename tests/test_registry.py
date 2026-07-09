"""Trial registry: append-only log, unique counting, burn tracking."""

from pathlib import Path

from research import registry


def test_trial_count_dedupes_same_candidate(tmp_path: Path):
    path = tmp_path / "trials.jsonl"
    scores = {"trades": 40, "expectancy": 0.01, "sharpe": 0.1}
    registry.log_trial(path, "baseline", {"a": 1}, "train", scores)
    registry.log_trial(path, "baseline", {"a": 1}, "train", scores)  # rerun
    registry.log_trial(path, "baseline", {"a": 2}, "train", scores)  # new params
    registry.log_trial(path, "baseline", {"a": 1}, "val", scores)    # new window
    assert registry.trial_count(path) == 3


def test_trial_sharpes_one_per_unique_trial(tmp_path: Path):
    path = tmp_path / "trials.jsonl"
    registry.log_trial(path, "f", {"a": 1}, "train", {"sharpe": 0.5})
    registry.log_trial(path, "f", {"a": 1}, "train", {"sharpe": 0.7})  # dup key
    registry.log_trial(path, "f", {"a": 2}, "train", {"sharpe": -0.2})
    assert sorted(registry.trial_sharpes(path)) == [-0.2, 0.5]


def test_gate_burn_is_permanent(tmp_path: Path):
    path = tmp_path / "trials.jsonl"
    assert not registry.gate_burned(path, "2026-01..2026-03")
    registry.log_gate(path, "f", {"a": 1}, "2026-01..2026-03",
                      {"trades": 40}, passed=False)
    assert registry.gate_burned(path, "2026-01..2026-03")
    assert not registry.gate_burned(path, "2026-04..2026-06")


def test_torn_last_line_does_not_lose_history(tmp_path: Path):
    path = tmp_path / "trials.jsonl"
    registry.log_trial(path, "f", {"a": 1}, "train", {"sharpe": 0.5})
    with path.open("a") as f:
        f.write('{"kind": "trial", "key": "trunc')  # simulated crash mid-write
    assert registry.trial_count(path) == 1


def test_missing_file_is_empty_not_error(tmp_path: Path):
    path = tmp_path / "nope.jsonl"
    assert registry.entries(path) == []
    assert registry.trial_count(path) == 0
