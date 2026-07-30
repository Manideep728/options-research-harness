"""Trial registry: append-only log, unique counting, burn tracking, and the
dataset identity that stops stale scores being reused."""

from datetime import UTC, datetime
from pathlib import Path

from research import registry


def _win(symbol: str, first: str, last: str) -> dict:
    return {symbol: ([1.0, 2.0], [datetime.fromisoformat(first).replace(tzinfo=UTC),
                                  datetime.fromisoformat(last).replace(tzinfo=UTC)])}


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
    logged_key = next(e["key"] for e in registry.entries(path))
    assert registry.trial_key("fam", {"x": 3, "y": 4}, "train") == logged_key


# --- dataset identity ---

def test_window_id_spans_all_symbols():
    windows = {**_win("SPY", "2026-01-05T14:30", "2026-03-01T20:45"),
               **_win("QQQ", "2026-01-02T14:30", "2026-03-05T20:45")}
    assert registry.window_id(windows) == "2026-01-02..2026-03-05"


def test_window_id_of_nothing_is_not_a_date_range():
    assert registry.window_id({}) == "empty"
    assert registry.window_id({"SPY": ([], [])}) == "empty"


def test_same_params_on_a_different_dataset_is_a_different_trial(tmp_path: Path):
    """THE bug this fixes. `fetch` moves the train/val boundaries, so the same
    params on the window still called "train" are scored on different bars. The
    key used to ignore that, and search.py reused the stale score."""
    path = tmp_path / "trials.jsonl"
    before = "2025-07-23..2026-03-01"
    after = "2025-09-01..2026-05-01"
    registry.log_trial(path, "baseline", {"a": 1}, "train", {"expectancy": 0.42},
                       dataset=before)
    registry.log_trial(path, "baseline", {"a": 1}, "train", {"expectancy": -0.09},
                       dataset=after)
    assert registry.trial_count(path) == 2
    scores = registry.trial_scores(path)
    assert scores[registry.trial_key("baseline", {"a": 1}, "train", before)][
        "expectancy"] == 0.42
    assert scores[registry.trial_key("baseline", {"a": 1}, "train", after)][
        "expectancy"] == -0.09


def test_dataset_qualified_key_never_collides_with_a_legacy_key(tmp_path: Path):
    """Rows written before `dataset` existed must keep their keys — so they stay
    countable in N — while never being reusable as a dataset-qualified score."""
    path = tmp_path / "trials.jsonl"
    registry.log_trial(path, "f", {"a": 1}, "train", {"expectancy": 0.5})
    legacy_key = next(e["key"] for e in registry.entries(path))
    assert registry.trial_key("f", {"a": 1}, "train") == legacy_key
    assert registry.trial_key("f", {"a": 1}, "train", "2026-01-01..2026-02-01") != legacy_key


def test_dataset_change_marker_is_recorded_without_touching_n(tmp_path: Path):
    """N counts attempts that genuinely happened, so invalidating scores must
    not shrink it — the marker is a note to readers, not a deletion."""
    path = tmp_path / "trials.jsonl"
    registry.log_trial(path, "f", {"a": 1}, "train", {"sharpe": 0.1})
    registry.log_dataset_change(path, "session filter + split adjustment")
    assert registry.trial_count(path) == 1
    kinds = [e["kind"] for e in registry.entries(path)]
    assert kinds == ["trial", "dataset_change"]
