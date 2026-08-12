"""Append-only trial registry: every candidate ever scored, forever.

The number of things you have tried is the denominator of every honest
statistical claim the research loop makes (the deflated Sharpe ratio is
"your Sharpe vs the best of N random tries"). If trials can be deleted or
forgotten, N shrinks and every result looks better than it is. So: one
JSONL file, append-only, and holdout-gate attempts are recorded here too —
that record is what makes the gate burn-once.

A trial's identity includes the DATASET it was scored on, not just the window
name. It used to be keyed on the literal string "train", which meant a score
survived `python -m research fetch` moving the train/val boundaries — so the
searcher would reuse a number computed on different bars and never notice. The
gate always got this right (it keys on the holdout's date range); trials did
not. `dataset` is omitted from the hash when empty so rows written before this
existed keep their keys and stay countable in N.
"""

import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DEFAULT_PATH = Path(__file__).resolve().parent / "trials.jsonl"

# Either shape the searcher may hold: a (closes, times) pair or full bars.
Windows = Mapping[str, Any]


def _times_of(window: object) -> list:
    """Timestamps from either a (closes, times) pair or full OhlcBars.

    Both shapes reach here now that the searcher passes full bars, and the
    dataset identity must not depend on which one a caller happens to hold —
    the same bars under two shapes have to hash to the same window.
    """
    stamps = getattr(window, "times", None)
    if stamps is not None:
        return list(stamps)
    return list(window[1])          # type: ignore[index]


def window_id(windows: Windows) -> str:
    """Calendar identity of a dataset: earliest..latest bar date across all
    symbols. Two windows with the same name but different dates are different
    datasets, and a score from one says nothing about the other."""
    all_times = [t for w in windows.values() if (t := _times_of(w))]
    if not all_times:
        return "empty"
    starts = [t[0] for t in all_times]
    ends = [t[-1] for t in all_times]
    return f"{min(starts).date().isoformat()}..{max(ends).date().isoformat()}"


def _key(family: str, params: dict, window: str, dataset: str = "") -> str:
    payload: dict = {"family": family, "params": params, "window": window}
    if dataset:
        # Conditional so pre-dataset rows hash identically to how they were
        # written. Their keys stay valid; they simply can never collide with a
        # dataset-qualified key, which is precisely the stale-reuse fix.
        payload["dataset"] = dataset
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]


def _append(path: Path, entry: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, sort_keys=True) + "\n")


def entries(path: Path = DEFAULT_PATH) -> list[dict]:
    file = Path(path)
    if not file.exists():
        return []
    out = []
    with file.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # a torn last line must not invalidate the history
    return out


def log_trial(path: Path, family: str, params: dict, window: str,
              scores: dict, dataset: str = "") -> None:
    _append(Path(path), {
        "kind": "trial",
        "key": _key(family, params, window, dataset),
        "family": family,
        "params": params,
        "window": window,
        "dataset": dataset,
        "scores": scores,
        "at": datetime.now(UTC).isoformat(),
    })


def log_dataset_change(path: Path, reason: str, dataset: str = "") -> None:
    """Record that prior scores are no longer comparable. Nothing is deleted —
    a reader that finds this row knows to distrust trials logged before it,
    while N keeps counting every attempt that genuinely happened."""
    _append(Path(path), {
        "kind": "dataset_change",
        "reason": reason,
        "dataset": dataset,
        "at": datetime.now(UTC).isoformat(),
    })


def log_gate(path: Path, family: str, params: dict, window_id: str,
             scores: dict, passed: bool) -> None:
    """Record a holdout-gate attempt. `burned` is permanent: pass or fail,
    this holdout window has now been seen and can never judge again."""
    _append(Path(path), {
        "kind": "gate",
        "key": _key(family, params, window_id),
        "family": family,
        "params": params,
        "window_id": window_id,
        "scores": scores,
        "passed": passed,
        "burned": True,
        "at": datetime.now(UTC).isoformat(),
    })


def trial_count(path: Path = DEFAULT_PATH) -> int:
    """Unique candidates ever tried. Re-running the same candidate on the
    same window is not a new trial — but the same params on a NEW window is."""
    return len({e["key"] for e in entries(path) if e.get("kind") == "trial"})


def trial_scores(path: Path = DEFAULT_PATH) -> dict[str, dict]:
    """Map of trial key -> its logged scores dict, for every recorded trial.
    Lets the searcher reuse a prior result instead of re-simulating a
    candidate it has already scored on the same window. First write wins:
    a candidate's score on a given window is deterministic, so an accidental
    duplicate row cannot change the answer."""
    out: dict[str, dict] = {}
    for e in entries(path):
        if e.get("kind") != "trial":
            continue
        out.setdefault(e["key"], e.get("scores", {}))
    return out


def trial_key(family: str, params: dict, window: str, dataset: str = "") -> str:
    """Public accessor for the identity hash, so callers key into
    trial_scores() with exactly the same hash log_trial() will write."""
    return _key(family, params, window, dataset)


def trial_sharpes(path: Path = DEFAULT_PATH) -> list[float]:
    """Sharpe of each unique trial — the spread feeds expected_max_sharpe."""
    seen: dict[str, float] = {}
    for e in entries(path):
        if e.get("kind") != "trial":
            continue
        sharpe = e.get("scores", {}).get("sharpe")
        if sharpe is not None:
            seen.setdefault(e["key"], float(sharpe))
    return list(seen.values())


def gate_burned(path: Path, window_id: str) -> bool:
    return any(
        e.get("kind") == "gate" and e.get("window_id") == window_id
        for e in entries(path)
    )
