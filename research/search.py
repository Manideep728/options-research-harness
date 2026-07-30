"""The mechanical searcher: one family at a time, select on TRAIN only.

Discipline (inherited from bot/tuner.py and non-negotiable):
  - candidates are ranked on train expectancy ONLY; validation is simulated
    for the single winner, never for the losers — so validation data stays
    genuinely unseen by the selection process,
  - every candidate scored is logged to the append-only registry first,
  - the winner must beat the CURRENT live strategy on validation by the
    tuner's improvement margin,
  - the winner must also beat a COIN FLIP on the same validation bars — see
    research/null.py. Beating the incumbent is not the same as beating chance:
    if the incumbent is itself no better than random, clearing it by 50% just
    means being luckier. The null is recorded in the candidate's evidence
    block, so no accepted candidate can be read without it,
  - exits are clamped through bot.config.clamp_tunables, signal params
    through the family's own bounds.

An accepted winner is written to candidate.json — the handoff point for
report / robustness / gate. Nothing here touches the live bot.
"""

import itertools
import json
import logging
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

from bot.config import Settings, clamp_tunables
from bot.simulator import SimParams, SimResult, simulate
from bot.tuner import IMPROVE_ABS_MARGIN, IMPROVE_FACTOR, MIN_TRADES
from research import data, metrics, null, registry
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
    spec: dict | None = None   # set when the family came from a proposal spec
    val_null: null.NullSummary | None = None


def run_family(windows: dict[str, Bars], cfg: Settings, sp: SimParams,
               family: Family, signal_params: dict) -> SimResult:
    """Combined SimResult for one candidate across all symbols' windows."""
    combined = SimResult()
    for symbol, (closes, times) in windows.items():
        result = simulate(
            closes, times, cfg, sp,
            signal_fn=lambda c, t=times, p=signal_params: family.signal(c, t, p),
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


def resolve_family(candidate: dict) -> Family:
    """A candidate.json either names a built-in family or embeds a spec."""
    if candidate.get("spec"):
        from research import blocks
        return blocks.spec_to_family(candidate["spec"])
    return FAMILIES[candidate["family"]]


def _with_exits(signal_candidates_list: list[dict]) -> list[tuple[dict, dict]]:
    return [
        (sig, clamp_tunables(dict(zip(EXIT_GRID.keys(), exits, strict=True))))
        for sig in signal_candidates_list
        for exits in itertools.product(*EXIT_GRID.values())
    ]


def search_family(family_name: str, cfg: Settings, sp: SimParams = SimParams(),
                  data_dir: Path = data.DATA_DIR,
                  registry_path: Path = registry.DEFAULT_PATH,
                  candidate_path: Path = CANDIDATE_PATH) -> SearchOutcome:
    family = FAMILIES[family_name]
    return _search(family, _with_exits(signal_candidates(family)), cfg, sp,
                   data_dir, registry_path, candidate_path)


def search_spec(spec: dict, cfg: Settings, sp: SimParams = SimParams(),
                data_dir: Path = data.DATA_DIR,
                registry_path: Path = registry.DEFAULT_PATH,
                candidate_path: Path = CANDIDATE_PATH) -> SearchOutcome:
    """Search a proposal-spec family through the exact same pipeline."""
    from research import blocks
    errors = blocks.validate_spec(spec)
    if errors:
        return SearchOutcome(False, f"invalid spec: {'; '.join(errors)}",
                             spec.get("name", "?"), {}, {}, None, None, None)
    family = blocks.spec_to_family(spec)
    return _search(family, _with_exits(blocks.spec_candidates(spec)), cfg, sp,
                   data_dir, registry_path, candidate_path, spec=spec)


def _search(family: Family, candidates: list[tuple[dict, dict]], cfg: Settings,
            sp: SimParams, data_dir: Path, registry_path: Path,
            candidate_path: Path, spec: dict | None = None) -> SearchOutcome:
    family_name = family.name
    bars = {s: data.load_bars("intraday", s, data_dir) for s in cfg.symbols}
    bars = {s: b for s, b in bars.items() if b[0]}
    if not bars:
        return SearchOutcome(False, "no cached bars — run `python -m research fetch`",
                             family_name, {}, {}, None, None, None)
    train_w, val_w = split_all(bars)
    # Identity of the exact bars being scored. Without this in the key, a score
    # survives `fetch` moving the split boundaries and gets reused on different
    # data — see research/registry.py.
    train_id = registry.window_id(train_w)
    val_id = registry.window_id(val_w)

    # Registry-aware skip: a candidate already scored on this train window is
    # not re-simulated (wasted work) nor re-logged (a duplicate row would not
    # change trial_count — same key — but bloats the file). We still need its
    # score to pick the winner, so we read it back from the registry. Its
    # score on a fixed window is deterministic, so reusing it is exact.
    prior_scores = registry.trial_scores(registry_path)

    best_expectancy: float | None = None
    best_sig: dict = {}
    best_exit: dict = {}
    reused = 0
    for sig_params, exit_params in candidates:
        params = {**sig_params, **exit_params}
        key = registry.trial_key(family_name, params, "train", train_id)
        cached = prior_scores.get(key)
        if cached is not None:
            reused += 1
            n = int(cached.get("trades", 0))
            expectancy = float(cached.get("expectancy", 0.0))
        else:
            train_res = run_family(train_w, replace(cfg, **exit_params), sp,
                                   family, sig_params)
            registry.log_trial(
                registry_path, family_name, params,
                "train", metrics.summarize([t.pnl_pct for t in train_res.trades]),
                dataset=train_id,
            )
            n, expectancy = train_res.n, train_res.expectancy

        if n < MIN_TRADES:
            continue
        if best_expectancy is None or expectancy > best_expectancy:
            best_expectancy = expectancy
            best_sig, best_exit = sig_params, exit_params

    log.info("searched %s: %d candidates (%d reused from registry, %d simulated)",
             family_name, len(candidates), reused, len(candidates) - reused)

    if best_expectancy is None:
        return SearchOutcome(False, "no candidate produced enough training trades",
                             family_name, {}, {}, None, None, None)

    # The evidence write needs the winner's full SimResult, and the registry
    # only stores summary scores (no trades). Re-simulate the single winner
    # once — one run, not the whole grid — whether or not it was reused above.
    best_train = run_family(train_w, replace(cfg, **best_exit), sp, family, best_sig)

    # One validation run for the single winner; baseline = the live strategy
    # (current cfg, default signal) on the same validation bars.
    val_res = run_family(val_w, replace(cfg, **best_exit), sp, family, best_sig)
    baseline_val = SimResult()
    for symbol, (closes, times) in val_w.items():
        baseline_val.trades.extend(simulate(closes, times, cfg, sp, symbol=symbol).trades)
    registry.log_trial(registry_path, family_name, {**best_sig, **best_exit},
                       "val", metrics.summarize([t.pnl_pct for t in val_res.trades]),
                       dataset=val_id)

    # The coin-flip control on the same validation bars, sized to the winner's
    # own trade count so the comparison is like-for-like.
    val_null = null.null_distribution(val_w, replace(cfg, **best_exit), sp, val_res.n)

    outcome = _judge(family_name, best_sig, best_exit, best_train, val_res,
                     baseline_val, val_null)
    outcome.spec = spec
    if outcome.accepted:
        _write_candidate(candidate_path, outcome)
    return outcome


def _judge(family_name: str, sig: dict, exits: dict, train: SimResult,
           val: SimResult, baseline_val: SimResult,
           val_null: null.NullSummary) -> SearchOutcome:
    def reject(reason: str) -> SearchOutcome:
        return SearchOutcome(False, reason, family_name, sig, exits, train, val,
                             baseline_val, val_null=val_null)

    if val.n < MIN_TRADES:
        return reject(f"winner has too few validation trades ({val.n})")
    if val.expectancy <= 0:
        return reject("winner has non-positive validation expectancy")
    if baseline_val.expectancy > 0:
        required = baseline_val.expectancy * IMPROVE_FACTOR
    else:
        required = baseline_val.expectancy + IMPROVE_ABS_MARGIN
    if val.expectancy < required:
        return reject(
            f"improvement too small (val {val.expectancy:.3f} < required {required:.3f})"
        )
    # Last and strictest: beat chance, not just the incumbent.
    if not val_null.beats(val.expectancy):
        return reject(
            f"does not beat the coin-flip null (val {val.expectancy:.3f} vs "
            f"null p{int(null.NULL_QUANTILE * 100)} {val_null.threshold:.3f} "
            f"over {val_null.seeds} seeds)"
        )
    return SearchOutcome(True, "passed all search guidelines",
                         family_name, sig, exits, train, val, baseline_val,
                         val_null=val_null)


def _write_candidate(path: Path, o: SearchOutcome) -> None:
    # Only accepted outcomes carry results. Writing a candidate without them
    # would emit an evidence block full of nulls that later reads as fact —
    # refuse instead.
    if o.train is None or o.val is None or o.baseline_val is None:
        raise ValueError("cannot write a candidate without train/val/baseline results")
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
            # The coin-flip control travels with the claim. An expectancy
            # without its null is not evidence, it is a number.
            "val_null_seeds": o.val_null.seeds if o.val_null else 0,
            "val_null_mean": round(o.val_null.mean, 4) if o.val_null else None,
            "val_null_threshold": round(o.val_null.threshold, 4) if o.val_null else None,
            "val_null_percentile": (
                round(o.val_null.percentile_of(o.val.expectancy), 4) if o.val_null else None
            ),
        },
        "created_at": datetime.now(UTC).isoformat(),
    }
    if o.spec is not None:
        payload["spec"] = o.spec
    Path(path).write_text(json.dumps(payload, indent=2))
    log.info("wrote %s", path)


def load_candidate(path: Path = CANDIDATE_PATH) -> dict | None:
    file = Path(path)
    if not file.exists():
        return None
    return json.loads(file.read_text())
