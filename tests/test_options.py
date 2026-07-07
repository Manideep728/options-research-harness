from datetime import date, timedelta

from bot.config import Settings
from bot.options import Contract, pick_contract
from bot.strategy import Action

CFG = Settings(api_key="", secret_key="")
TODAY = date(2026, 7, 6)
PX = 100.0


def make(strike, cp="call", dte=10, bid=1.00, ask=1.05, oi=500):
    expiry = TODAY + timedelta(days=dte)
    return Contract(
        symbol=f"TST{expiry:%y%m%d}{'C' if cp == 'call' else 'P'}{int(strike * 1000):08d}",
        underlying="TST",
        expiry=expiry,
        strike=strike,
        call_put=cp,
        bid=bid,
        ask=ask,
        open_interest=oi,
    )


def test_picks_first_otm_call():
    chain = [make(95.0), make(101.0), make(103.0), make(105.0)]
    picked, _ = pick_contract(chain, Action.BUY_CALL, PX, TODAY, CFG)
    assert picked is not None
    assert picked.strike == 101.0


def test_picks_first_otm_put():
    chain = [make(105.0, "put"), make(99.0, "put"), make(95.0, "put")]
    picked, _ = pick_contract(chain, Action.BUY_PUT, PX, TODAY, CFG)
    assert picked is not None
    assert picked.strike == 99.0


def test_rejects_itm_contracts():
    chain = [make(95.0)]  # ITM call
    picked, rejections = pick_contract(chain, Action.BUY_CALL, PX, TODAY, CFG)
    assert picked is None
    assert any("not OTM" in r.reason for r in rejections)


def test_rejects_dte_outside_window():
    chain = [make(101.0, dte=2), make(101.0, dte=30)]
    picked, rejections = pick_contract(chain, Action.BUY_CALL, PX, TODAY, CFG)
    assert picked is None
    assert all("DTE" in r.reason for r in rejections)


def test_prefers_nearest_valid_expiry():
    chain = [make(101.0, dte=14), make(101.0, dte=8)]
    picked, _ = pick_contract(chain, Action.BUY_CALL, PX, TODAY, CFG)
    assert (picked.expiry - TODAY).days == 8


def test_rejects_no_bid():
    chain = [make(101.0, bid=0.0, ask=0.50)]
    picked, rejections = pick_contract(chain, Action.BUY_CALL, PX, TODAY, CFG)
    assert picked is None
    assert any("no bid" in r.reason for r in rejections)


def test_rejects_wide_spread():
    # mid = 1.00, spread = 0.40 -> 40% of mid, way over the 10% cap
    chain = [make(101.0, bid=0.80, ask=1.20)]
    picked, rejections = pick_contract(chain, Action.BUY_CALL, PX, TODAY, CFG)
    assert picked is None
    assert any("spread" in r.reason for r in rejections)


def test_rejects_low_open_interest():
    chain = [make(101.0, oi=10)]
    picked, rejections = pick_contract(chain, Action.BUY_CALL, PX, TODAY, CFG)
    assert picked is None
    assert any("open interest" in r.reason for r in rejections)


def test_ignores_wrong_right_without_rejection_noise():
    chain = [make(99.0, "put")]
    picked, rejections = pick_contract(chain, Action.BUY_CALL, PX, TODAY, CFG)
    assert picked is None
    assert rejections == []  # wrong type is silently skipped, not "rejected"


def test_empty_chain():
    picked, rejections = pick_contract([], Action.BUY_CALL, PX, TODAY, CFG)
    assert picked is None
    assert rejections == []
