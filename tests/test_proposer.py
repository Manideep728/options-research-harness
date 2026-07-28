"""Proposer: prompt building, offline mode, validation retry, spec search."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from bot.config import Settings
from research import data, proposer, registry, search

GOOD_SPEC = {
    "name": "calm-uptrend-calls",
    "hypothesis": "report shows high-vol entries lose; only buy calls in calm uptrends",
    "trend": "ema_cross",
    "trigger": "rsi_cross",
    "filters": ["calls_only", "max_entry_vol"],
    "grid": {
        "ema_fast": [8, 9], "ema_slow": [18, 21],
        "rsi_bull_level": [40.0, 45.0], "rsi_bear_level": [55.0],
        "max_entry_vol": [0.003, 0.005],
    },
}

BAD_SPEC = {**GOOD_SPEC, "trigger": "macd"}


class _FakeBlock:
    type = "text"

    def __init__(self, text: str):
        self.text = text


class _FakeResponse:
    stop_reason = "end_turn"

    def __init__(self, spec: dict):
        self.content = [_FakeBlock(json.dumps(spec))]


class FakeClient:
    """Stands in for anthropic.Anthropic(); returns queued specs in order."""

    def __init__(self, specs: list[dict]):
        self._queue = [_FakeResponse(s) for s in specs]
        self.requests: list[dict] = []
        self.messages = self

    def create(self, **kwargs):
        self.requests.append(kwargs)
        return self._queue.pop(0)


def _report(tmp_path: Path) -> Path:
    path = tmp_path / "report.txt"
    path.write_text("losses cluster in high vol entries", encoding="utf-8")
    return path


def test_offline_mode_writes_prompt(tmp_path):
    out = proposer.propose(
        _report(tmp_path), registry_path=tmp_path / "t.jsonl", offline=True,
        prompt_path=tmp_path / "prompt.txt", proposals_dir=tmp_path / "p",
    )
    prompt = (tmp_path / "prompt.txt").read_text(encoding="utf-8")
    assert out == {"offline_prompt": str(tmp_path / "prompt.txt")}
    assert "high vol entries" in prompt           # report embedded
    assert "ema_cross" in prompt and "donchian" in prompt  # vocabulary listed


def test_missing_report_fails_cleanly(tmp_path):
    assert proposer.propose(tmp_path / "nope.txt",
                            registry_path=tmp_path / "t.jsonl") is None


def test_valid_proposal_saved(tmp_path):
    client = FakeClient([GOOD_SPEC])
    spec = proposer.propose(_report(tmp_path), registry_path=tmp_path / "t.jsonl",
                            proposals_dir=tmp_path / "props", client=client)
    assert spec["name"] == "calm-uptrend-calls"
    saved = json.loads(Path(spec["_path"]).read_text(encoding="utf-8"))
    assert saved["trigger"] == "rsi_cross"
    assert len(client.requests) == 1


def test_invalid_spec_gets_one_retry_with_errors(tmp_path):
    client = FakeClient([BAD_SPEC, GOOD_SPEC])
    spec = proposer.propose(_report(tmp_path), registry_path=tmp_path / "t.jsonl",
                            proposals_dir=tmp_path / "props", client=client)
    assert spec is not None and spec["name"] == "calm-uptrend-calls"
    assert len(client.requests) == 2
    retry_messages = client.requests[1]["messages"]
    assert "failed validation" in retry_messages[-1]["content"]


def test_two_invalid_specs_fail(tmp_path):
    client = FakeClient([BAD_SPEC, BAD_SPEC])
    assert proposer.propose(_report(tmp_path), registry_path=tmp_path / "t.jsonl",
                            proposals_dir=tmp_path / "props", client=client) is None


# --- spec search end-to-end on synthetic cache ---

def _write_cache(tmp_path: Path, symbols=("AAA", "BBB"), n=600):
    start = datetime(2026, 1, 5, 14, 30, tzinfo=UTC)
    for sym in symbols:
        closes: list[float] = []
        while len(closes) < n:
            closes += [100.0 + j for j in range(12)]
            closes += [111.0 - j for j in range(12)]
        times = [start + timedelta(minutes=15 * i) for i in range(n)]
        data.save_bars(tmp_path / "intraday" / f"{sym}.csv", closes[:n], times)


def test_search_spec_runs_and_logs_with_spec_name(tmp_path, monkeypatch):
    _write_cache(tmp_path)
    monkeypatch.setattr(search, "MIN_TRADES", 1)
    cfg = Settings(api_key="k", secret_key="s", symbols=("AAA", "BBB"))
    reg = tmp_path / "trials.jsonl"

    outcome = search.search_spec(GOOD_SPEC, cfg, data_dir=tmp_path,
                                 registry_path=reg,
                                 candidate_path=tmp_path / "cand.json")
    assert outcome.family == "spec:calm-uptrend-calls"
    trials = [e for e in registry.entries(reg) if e["kind"] == "trial"]
    assert trials and all(t["family"] == "spec:calm-uptrend-calls" for t in trials)
    if outcome.accepted:  # candidate must embed the spec for the gate
        saved = search.load_candidate(tmp_path / "cand.json")
        assert saved["spec"]["trigger"] == "rsi_cross"


def test_search_spec_rejects_invalid_spec(tmp_path):
    cfg = Settings(api_key="k", secret_key="s", symbols=("AAA",))
    outcome = search.search_spec(BAD_SPEC, cfg, data_dir=tmp_path,
                                 registry_path=tmp_path / "t.jsonl",
                                 candidate_path=tmp_path / "c.json")
    assert not outcome.accepted and "invalid spec" in outcome.reason
