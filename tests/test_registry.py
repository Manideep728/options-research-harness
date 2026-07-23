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


def test_trial_scores_keyed_by_trial_key(tmp_path: Path):
    path = tmp_path / "trials.jsonl"
    s1 = {"trades": 40, "expectancy": 0.05, "sharpe": 0.2}
    registry.log_trial(path, "baseline", {"a": 1}, "train", s1)
    key = registry.trial_key("baseline", {"a": 1}, "train")
    assert registry.trial_scores(path)[key] == s1


def test_trial_scores_first_write_wins_on_duplicate(tmp_path: Path):
    """A candidate's score on a fixed window is deterministic, so a duplicate
    row must not change the cached score the searcher reuses."""
    path = tmp_path / "trials.jsonl"
    key = registry.trial_key("f", {"a": 1}, "train")
    registry.log_trial(path, "f", {"a": 1}, "train", {"expectancy": 0.10})
    registry.log_trial(path, "f", {"a": 1}, "train", {"expectancy": 0.99})  # dup
    assert registry.trial_scores(path)[key]["expectancy"] == 0.10


def test_trial_key_matches_logged_key(tmp_path: Path):
    """trial_key must reproduce exactly the key log_trial writes, or the
    searcher's skip-lookup would silently miss every prior trial."""
    path = tmp_path / "trials.jsonl"
    registry.log_trial(path, "fam", {"x": 3, "y": 4}, "train", {"sharpe": 0.1})
    logged_key = [e["key"] for e in registry.entries(path)][0]
    assert registry.trial_key("fam", {"x": 3, "y": 4}, "train") == logged_key
