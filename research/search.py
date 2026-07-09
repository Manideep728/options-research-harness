"""The mechanical searcher: one family at a time, select on TRAIN only.

Discipline (inherited from bot/tuner.py and non-negotiable):
  - candidates are ranked on train expectancy ONLY; validation is simulated
    for the single winner, never for the losers — so validation data stays
    genuinely unseen by the selection process,
  - every candidate scored is logged to the append-only registry first,
  - the winner must beat the CURRENT live strategy on validation by the
    tuner's improvement margin,
  - exits are clamped through bot.config.clamp_tunables, signal params
    through the family's own bounds.

An accepted winner is written to candidate.json — the handoff point for
report / robustness / gate. Nothing here touches the live bot.
"""

import itertools
import json
import logging
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

from bot.config import Settings, clamp_tunables
from bot.simulator import SimParams, SimResult, simulate
from bot.tuner import IMPROVE_ABS_MARGIN, IMPROVE_FACTOR, MIN_TRADES

from research import data, metrics, registry
from research.families import EXIT_GRID, FAMILIES, Family, signal_candidates

log = logging.getLogger("research.search")

CANDIDATE_PATH = Path(__file__).resolve().parent / "candidate.json"

Bars = tuple[list[float], list]


@dataclass
class SearchOutcome:
    accepted: bool
    reason: str
    family: str
    signal_params: dict
    exit_params: dict
    train: SimResult | None
    val: SimResult | None
    baseline_val: SimResult | None


def run_family(windows: dict[str, Bars], cfg: Settings, sp: SimParams,
               family: Family, signal_params: dict) -> SimResult:
    """Combined SimResult for one candidate across all symbols' windows."""
    combined = SimResult()
    for symbol, (closes, times) in windows.items():
        result = simulate(
            closes, times, cfg, sp,
            signal_fn=lambda c, p=signal_params: family.signal(c, p),
            symbol=symbol,
        )
        combined.trades.extend(result.trades)
    combined.trades.sort(key=lambda t: t.entry_time)
    return combined


def split_all(bars_by_symbol: dict[str, Bars]) -> tuple[dict[str, Bars], dict[str, Bars]]:
    train_w: dict[str, Bars] = {}
    val_w: dict[str, Bars] = {}
    for symbol, (closes, times) in bars_by_symbol.items():
        train, val = data.split_train_val(closes, times)
        train_w[symbol], val_w[symbol] = train, val
    return train_w, val_w


def search_family(family_name: str, cfg: Settings, sp: SimParams = SimParams(),
                  data_dir: Path = data.DATA_DIR,
                  registry_path: Path = registry.DEFAULT_PATH,
                  candidate_path: Path = CANDIDATE_PATH) -> SearchOutcome:
    family = FAMILIES[family_name]
    bars = {s: data.load_bars("intraday", s, data_dir) for s in cfg.symbols}
    bars = {s: b for s, b in bars.items() if b[0]}
    if not bars:
        return SearchOutcome(False, "no cached bars — run `python -m research fetch`",
                             family_name, {}, {}, None, None, None)
    train_w, val_w = split_all(bars)

    candidates = [
        (sig, clamp_tunables(dict(zip(EXIT_GRID.keys(), exits))))
        for sig in signal_candidates(family)
        for exits in itertools.product(*EXIT_GRID.values())
    ]
    log.info("searching %s: %d candidates", family_name, len(candidates))

    best_train: SimResult | None = None
    best_sig: dict = {}
    best_exit: dict = {}
    for sig_params, exit_params in candidates:
        train_res = run_family(train_w, replace(cfg, **exit_params), sp,
                               family, sig_params)
        registry.log_trial(
            registry_path, family_name, {**sig_params, **exit_params},
            "train", metrics.summarize([t.pnl_pct for t in train_res.trades]),
        )
        if train_res.n < MIN_TRADES:
            continue
        if best_train is None or train_res.expectancy > best_train.expectancy:
            best_train, best_sig, best_exit = train_res, sig_params, exit_params

    if best_train is None:
        return SearchOutcome(False, "no candidate produced enough training trades",
                             family_name, {}, {}, None, None, None)

    # One validation run for the single winner; baseline = the live strategy
    # (current cfg, default signal) on the same validation bars.
    val_res = run_family(val_w, replace(cfg, **best_exit), sp, family, best_sig)
    baseline_val = SimResult()
    for symbol, (closes, times) in val_w.items():
        baseline_val.trades.extend(simulate(closes, times, cfg, sp, symbol=symbol).trades)
    registry.log_trial(registry_path, family_name, {**best_sig, **best_exit},
                       "val", metrics.summarize([t.pnl_pct for t in val_res.trades]))

    outcome = _judge(family_name, best_sig, best_exit, best_train, val_res, baseline_val)
    if outcome.accepted:
        _write_candidate(candidate_path, outcome)
    return outcome


def _judge(family_name: str, sig: dict, exits: dict, train: SimResult,
           val: SimResult, baseline_val: SimResult) -> SearchOutcome:
    if val.n < MIN_TRADES:
        return SearchOutcome(False, f"winner has too few validation trades ({val.n})",
                             family_name, sig, exits, train, val, baseline_val)
    if val.expectancy <= 0:
        return SearchOutcome(False, "winner has non-positive validation expectancy",
                             family_name, sig, exits, train, val, baseline_val)
    if baseline_val.expectancy > 0:
        required = baseline_val.expectancy * IMPROVE_FACTOR
    else:
        required = baseline_val.expectancy + IMPROVE_ABS_MARGIN
    if val.expectancy < required:
        return SearchOutcome(
            False,
            f"improvement too small (val {val.expectancy:.3f} < required {required:.3f})",
            family_name, sig, exits, train, val, baseline_val,
        )
    return SearchOutcome(True, "passed all search guidelines",
                         family_name, sig, exits, train, val, baseline_val)


def _write_candidate(path: Path, o: SearchOutcome) -> None:
    payload = {
        "family": o.family,
        "signal_params": o.signal_params,
        "exit_params": o.exit_params,
        "evidence": {
            "train_trades": o.train.n,
            "train_expectancy": round(o.train.expectancy, 4),
            "val_trades": o.val.n,
            "val_expectancy": round(o.val.expectancy, 4),
            "val_win_rate": round(o.val.win_rate, 4),
            "baseline_val_expectancy": round(o.baseline_val.expectancy, 4),
        },
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    Path(path).write_text(json.dumps(payload, indent=2))
    log.info("wrote %s", path)


def load_candidate(path: Path = CANDIDATE_PATH) -> dict | None:
    file = Path(path)
    if not file.exists():
        return None
    return json.loads(file.read_text())
