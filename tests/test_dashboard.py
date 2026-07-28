"""Dashboard snapshot: must poll only the engine's active shortlist, not the
whole universe (that was the whole point of the two-tier scan)."""

from datetime import UTC, datetime

from bot.config import Settings
from bot.dashboard import build_dashboard_snapshot
from bot.watchlist import ActiveEntry, save_active

NOW = datetime(2026, 7, 9, 15, 0, tzinfo=UTC)
CLOSE_T = datetime(2026, 7, 9, 20, 0, tzinfo=UTC)


class ClockInfo:
    def __init__(self):
        self.is_open = True
        self.now = NOW
        self.next_open = NOW
        self.next_close = CLOSE_T


class FakeBroker:
    """Minimal broker that records which symbols get a data call."""

    def __init__(self):
        self.closes_calls: list[str] = []

    def get_clock(self):
        return ClockInfo()

    def get_todays_option_orders(self, day_start):
        return [], []

    def get_option_positions(self):
        return []

    def get_equity(self):
        return 100_000.0

    def get_last_equity(self):
        return 100_000.0

    def get_closes(self, symbol):
        self.closes_calls.append(symbol)
        return [100.0] * 80          # flat -> no signal -> no chain fetch

    def get_underlying_price(self, symbol):
        return 100.0

    def get_chain(self, underlying, want, today, px):
        return []


def cfg_for(tmp_path, **overrides):
    return Settings(
        api_key="k", secret_key="s",
        symbols=("SPY", "QQQ", "AAPL", "NVDA", "TSLA"),
        state_file=str(tmp_path / "state.json"),
        journal_file=str(tmp_path / "trades.csv"),
        control_file=str(tmp_path / "control.json"),
        active_file=str(tmp_path / "active.json"),
        **overrides,
    )


def test_snapshot_polls_only_shortlisted_symbols(tmp_path):
    cfg = cfg_for(tmp_path)
    save_active(cfg.active_file, NOW, [
        ActiveEntry("SPY", 0.02, "trend=1.0"),
        ActiveEntry("NVDA", 0.01, "trend=0.5"),
    ])
    broker = FakeBroker()

    snap = build_dashboard_snapshot(broker, cfg)

    # Only the two shortlisted names are snapshotted and polled — the other
    # three symbols in the universe are never touched.
    assert [s.symbol for s in snap.symbols] == ["SPY", "NVDA"]
    assert broker.closes_calls == ["SPY", "NVDA"]
    # Rank metadata is carried through for the UI.
    assert snap.symbols[0].score == 0.02
    assert snap.bot["active_symbols"] == ["SPY", "NVDA"]
    assert snap.bot["ranked_at"] == NOW.isoformat()


def test_snapshot_with_no_shortlist_polls_nothing(tmp_path):
    # No active.json yet (engine hasn't scanned): the dashboard shows no
    # symbol cards and makes zero per-symbol data calls.
    cfg = cfg_for(tmp_path)
    broker = FakeBroker()
    snap = build_dashboard_snapshot(broker, cfg)
    assert snap.symbols == []
    assert broker.closes_calls == []
    assert snap.bot["active_symbols"] == []
