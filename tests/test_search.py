"""Search: select-on-train, registry logging, candidate handoff, simulator
signal_fn regression."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

from bot.config import Settings
from bot.simulator import signal_series, simulate
from bot.strategy import Action
from research import data, registry, search


def _bars(n: int, sawtooth: bool = True) -> tuple[list[float], list[datetime]]:
    start = datetime(2026, 1, 5, 14, 30, tzinfo=UTC)
    times = [start + timedelta(minutes=15 * i) for i in range(n)]
    closes = []
    i = 0
    while len(closes) < n:
        closes += [100.0 + j for j in range(12)]
        closes += [111.0 - j for j in range(12)]
        i += 1
    return closes[:n], times


def _cfg(**kw) -> Settings:
    return Settings(api_key="k", secret_key="s", **kw)


# --- simulate(signal_fn=...) regression ---

def test_simulate_default_signal_unchanged():
    closes, times = _bars(400)
    cfg = _cfg()
    default = simulate(closes, times, cfg)
    explicit = simulate(closes, times, cfg,
                        signal_fn=lambda c: signal_series(c, cfg))
    assert [t.pnl_pct for t in default.trades] == [t.pnl_pct for t in explicit.trades]


def test_simulate_stamps_symbol_and_uses_custom_signal():
    closes, times = _bars(100)
    fire_once = [Action.NONE] * 50 + [Action.BUY_CALL] + [Action.NONE] * 49
    result = simulate(closes, times, _cfg(), signal_fn=lambda c: fire_once,
                      symbol="SPY")
    assert result.n == 1
    assert result.trades[0].symbol == "SPY"


# --- search_family end to end on cached synthetic data ---

def _write_cache(tmp_path: Path, symbols=("AAA", "BBB"), n=600):
    for sym in symbols:
        closes, times = _bars(n)
        data.save_bars(tmp_path / "intraday" / f"{sym}.csv", closes, times)


def test_search_logs_every_candidate_and_selects_on_train(tmp_path, monkeypatch):
    _write_cache(tmp_path)
    monkeypatch.setattr(search, "MIN_TRADES", 1)
    reg = tmp_path / "trials.jsonl"
    cand = tmp_path / "candidate.json"
    cfg = _cfg(symbols=("AAA", "BBB"))

    outcome = search.search_family("rsi_only", cfg, data_dir=tmp_path,
                                   registry_path=reg, candidate_path=cand)

    trials = [e for e in registry.entries(reg) if e["kind"] == "trial"]
    train_trials = [e for e in trials if e["window"] == "train"]
    # every grid candidate (3 bull x 3 bear x 9 exits) logged on train
    assert len(train_trials) == 3 * 3 * 9
    # exactly ONE validation entry: the winner (select-on-train discipline)
    assert sum(1 for e in trials if e["window"] == "val") == 1
    assert outcome.train is not None and outcome.train.n >= 1


def test_accepted_search_writes_candidate_rejected_does_not(tmp_path, monkeypatch):
    _write_cache(tmp_path)
    reg = tmp_path / "trials.jsonl"
    cand = tmp_path / "candidate.json"
    cfg = _cfg(symbols=("AAA", "BBB"))

    # default MIN_TRADES=30 on tiny data -> rejected, no candidate file
    outcome = search.search_family("rsi_only", cfg, data_dir=tmp_path,
                                   registry_path=reg, candidate_path=cand)
    assert not outcome.accepted
    assert not cand.exists()

    monkeypatch.setattr(search, "MIN_TRADES", 1)
    outcome = search.search_family("rsi_only", cfg, data_dir=tmp_path,
                                   registry_path=reg, candidate_path=cand)
    if outcome.accepted:  # sawtooth data may or may not beat baseline margin
        saved = search.load_candidate(cand)
        assert saved["family"] == "rsi_only"
        assert "signal_params" in saved and "exit_params" in saved


def test_search_without_cache_fails_cleanly(tmp_path):
    outcome = search.search_family("baseline", _cfg(symbols=("AAA",)),
                                   data_dir=tmp_path,
                                   registry_path=tmp_path / "t.jsonl",
                                   candidate_path=tmp_path / "c.json")
    assert not outcome.accepted
    assert "no cached bars" in outcome.reason


# --- registry-aware skip: don't re-test what the registry already scored ---

def _train_trial_keys(reg: Path) -> list[str]:
    return [e["key"] for e in registry.entries(reg)
            if e["kind"] == "trial" and e["window"] == "train"]


def test_rerun_does_not_relog_train_trials(tmp_path, monkeypatch):
    """Second search of the same family on the same window must reuse the
    registry, not append duplicate train rows. A regression that dropped the
    skip would double every train trial here."""
    _write_cache(tmp_path)
    monkeypatch.setattr(search, "MIN_TRADES", 1)
    reg = tmp_path / "trials.jsonl"
    cfg = _cfg(symbols=("AAA", "BBB"))

    search.search_family("rsi_only", cfg, data_dir=tmp_path, registry_path=reg,
                         candidate_path=tmp_path / "c1.json")
    keys_after_first = _train_trial_keys(reg)

    search.search_family("rsi_only", cfg, data_dir=tmp_path, registry_path=reg,
                         candidate_path=tmp_path / "c2.json")
    keys_after_second = _train_trial_keys(reg)

    # No new train rows on the second run — every candidate was reused.
    assert keys_after_second == keys_after_first


def test_rerun_selects_the_same_winner_from_registry(tmp_path, monkeypatch):
    """The reused-from-registry path must still pick the same winner and
    produce the same evidence as the fresh run. A bug that skipped a
    candidate without carrying its score forward could drop the true winner."""
    _write_cache(tmp_path)
    monkeypatch.setattr(search, "MIN_TRADES", 1)
    reg = tmp_path / "trials.jsonl"
    cfg = _cfg(symbols=("AAA", "BBB"))

    first = search.search_family("rsi_only", cfg, data_dir=tmp_path,
                                 registry_path=reg, candidate_path=tmp_path / "c1.json")
    second = search.search_family("rsi_only", cfg, data_dir=tmp_path,
                                  registry_path=reg, candidate_path=tmp_path / "c2.json")

    assert second.signal_params == first.signal_params
    assert second.exit_params == first.exit_params
    assert second.accepted == first.accepted
    # Re-simulated winner reproduces the same train stats bit-for-bit.
    assert second.train.n == first.train.n
    assert second.train.expectancy == first.train.expectancy


def test_new_candidate_still_logged_when_registry_has_others(tmp_path, monkeypatch):
    """Skip must be per-candidate, not all-or-nothing: a family sharing no
    keys with a prior family's trials is fully simulated and logged."""
    _write_cache(tmp_path)
    monkeypatch.setattr(search, "MIN_TRADES", 1)
    reg = tmp_path / "trials.jsonl"
    cfg = _cfg(symbols=("AAA", "BBB"))

    search.search_family("rsi_only", cfg, data_dir=tmp_path, registry_path=reg,
                         candidate_path=tmp_path / "c1.json")
    before = len(_train_trial_keys(reg))

    # donchian shares no candidate keys with rsi_only -> all newly logged.
    search.search_family("donchian", cfg, data_dir=tmp_path, registry_path=reg,
                         candidate_path=tmp_path / "c2.json")
    after = len(_train_trial_keys(reg))
    assert after > before
