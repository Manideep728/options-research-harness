"""Option contract selection. Pure logic over an already-fetched chain."""

from dataclasses import dataclass
from datetime import date

from bot.config import Settings
from bot.strategy import Action


@dataclass(frozen=True)
class Contract:
    """Broker-agnostic view of one option contract with a current quote."""

    symbol: str            # OCC symbol, e.g. SPY260717C00560000
    underlying: str
    expiry: date
    strike: float
    call_put: str          # "call" | "put"
    bid: float
    ask: float
    open_interest: int

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0


@dataclass(frozen=True)
class Rejection:
    contract_symbol: str
    reason: str


def pick_contract(
    chain: list[Contract],
    action: Action,
    underlying_price: float,
    today: date,
    cfg: Settings,
) -> tuple[Contract | None, list[Rejection]]:
    """Pick the first-strike-OTM contract in the nearest valid expiry.

    Filters: right type, DTE window, OTM, liquidity (bid, spread, OI).
    Returns (contract, rejections) — rejections explain every discard so the
    engine can log why no trade happened.
    """
    want = "call" if action == Action.BUY_CALL else "put"
    rejections: list[Rejection] = []
    candidates: list[Contract] = []

    for c in chain:
        if c.call_put != want:
            continue
        dte = (c.expiry - today).days
        if not (cfg.min_dte <= dte <= cfg.max_dte):
            rejections.append(Rejection(c.symbol, f"DTE {dte} outside [{cfg.min_dte},{cfg.max_dte}]"))
            continue
        is_otm = c.strike > underlying_price if want == "call" else c.strike < underlying_price
        if not is_otm:
            rejections.append(Rejection(c.symbol, f"not OTM (strike {c.strike} vs px {underlying_price:.2f})"))
            continue
        if c.bid <= 0:
            rejections.append(Rejection(c.symbol, "no bid"))
            continue
        if c.mid <= 0 or (c.ask - c.bid) / c.mid > cfg.max_spread_pct_of_mid:
            rejections.append(Rejection(c.symbol, f"spread too wide (bid {c.bid} ask {c.ask})"))
            continue
        if c.open_interest < cfg.min_open_interest:
            rejections.append(Rejection(c.symbol, f"open interest {c.open_interest} < {cfg.min_open_interest}"))
            continue
        candidates.append(c)

    if not candidates:
        return None, rejections

    # Nearest expiry first, then the strike closest to the money.
    def otm_distance(c: Contract) -> float:
        return abs(c.strike - underlying_price)

    best = min(candidates, key=lambda c: ((c.expiry - today).days, otm_distance(c)))
    return best, rejections
