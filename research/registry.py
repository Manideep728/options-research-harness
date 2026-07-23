"""Append-only trial registry: every candidate ever scored, forever.

The number of things you have tried is the denominator of every honest
statistical claim the research loop makes (the deflated Sharpe ratio is
"your Sharpe vs the best of N random tries"). If trials can be deleted or
forgotten, N shrinks and every result looks better than it is. So: one
JSONL file, append-only, and holdout-gate attempts are recorded here too —
that record is what makes the gate burn-once.
"""

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_PATH = Path(__file__).resolve().parent / "trials.jsonl"


def _key(family: str, params: dict, window: str) -> str:
    canonical = json.dumps({"family": family, "params": params, "window": window},
                           sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


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
              scores: dict) -> None:
    _append(Path(path), {
        "kind": "trial",
        "key": _key(family, params, window),
        "family": family,
        "params": params,
        "window": window,
        "scores": scores,
        "at": datetime.now(timezone.utc).isoformat(),
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
        "at": datetime.now(timezone.utc).isoformat(),
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


def trial_key(family: str, params: dict, window: str) -> str:
    """Public accessor for the identity hash, so callers key into
    trial_scores() with exactly the same hash log_trial() will write."""
    return _key(family, params, window)


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
