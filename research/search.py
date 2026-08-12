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
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

from bot.config import Settings, clamp_tunables
from bot.simulator import SimParams, SimResult, simulate
from bot.tuner import IMPROVE_ABS_MARGIN, IMPROVE_FACTOR, MIN_TRADES
from research import data, metrics, null, registry
from research.families import (
    EXIT_GRID,
    FAMILIES,
    Family,
    clamp_params,
    signal_candidates,
)

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


# A window is either the (closes, times) pair most callers still hold, or full
# bars. Mapping rather than dict in the signatures below: dict is invariant, so
# a dict[str, Bars] would not satisfy a dict of the union.
Window = Bars | data.OhlcBars


def as_ohlc(window: Window) -> data.OhlcBars:
    """Accept either a (closes, times) pair or full bars.

    Most callers still hold the pair, and a pair carries no intra-bar range —
    so it is wrapped with has_intrabar False and the barrier test falls back to
    the close. That degrades honestly; it does not pretend to a range it lacks.
    """
    if isinstance(window, data.OhlcBars):
        return window
    closes, times = window
    return data.ohlc_from_closes(closes, times)


def run_family(windows: Mapping[str, Window], cfg: Settings,
               sp: SimParams, family: Family, signal_params: dict,
               ivs: Mapping[str, list[float]] | None = None) -> SimResult:
    """Combined SimResult for one candidate across all symbols' windows.

    The high, low and open columns are passed through when the caller supplies
    full bars, so a barrier touched inside a bar exits AT the barrier. Without
    them every exit books the full close-to-close move, and because gains are
    unbounded while losses floor at -100%, that asymmetry alone made a coin
    flip earn +0.34% per trade on daily bars where the honest figure is -8.01%.

    `ivs` prices each symbol at its own implied volatility. A single constant
    across a universe underprices whatever realizes more than it, and a random
    signal collects that pricing error as if it were skill.
    """
    combined = SimResult()
    for symbol, window in windows.items():
        bars = as_ohlc(window)
        series = None if ivs is None else ivs.get(symbol)

        def signal_fn(c, t=bars.times, p=signal_params, s=series):
            # A family that declares needs_iv gets the volatility series too.
            # It must handle s being None — a symbol with no volatility index
            # has no rank, and inventing one would report a different strategy
            # under this family's name.
            return (family.signal(c, t, p, ivs=s) if family.needs_iv
                    else family.signal(c, t, p))

        result = simulate(
            bars.closes, bars.times, cfg, sp, signal_fn=signal_fn, symbol=symbol,
            highs=bars.highs, lows=bars.lows, opens=bars.opens, ivs=series,
        )
        combined.trades.extend(result.trades)
    combined.trades.sort(key=lambda t: t.entry_time)
    return combined


def _score(result: SimResult) -> dict:
    """Registry-ready scores, with the Sharpe clustered by entry bar.

    Trades that fired on the same bar across a correlated universe are one
    market move; counting them as independent overstates the confidence that
    feeds expected_max_sharpe and, through it, every deflated Sharpe.
    """
    return metrics.summarize([t.pnl_pct for t in result.trades],
                             [t.entry_time for t in result.trades])


def split_all(bars_by_symbol: dict[str, Bars]) -> tuple[dict[str, Bars], dict[str, Bars]]:
    """(train, validation) at ONE shared calendar cutoff across every symbol.

    Delegates to data.split_all rather than cutting each symbol at a fraction of
    its own bar count — see data.union_cutoff for why that mattered.
    """
    return data.split_all(bars_by_symbol, data.TRAIN_FRACTION)


WindowSet = tuple[dict[str, Window], dict[str, Window],
                  dict[str, list[float]], dict[str, list[float]]]


def load_windows(family: Family, cfg: Settings,
                 data_dir: Path = data.DATA_DIR) -> WindowSet | None:
    """(train, validation, train IVs, validation IVs) for this family.

    A short-premium family runs on SPY/QQQ/IWM daily, because those are the
    only symbols with a real implied volatility history. Pricing a sold option
    at a volatility the market never quoted is the error that made a coin flip
    look profitable, so the family declares the dataset it can be measured on
    rather than inheriting whichever one the searcher happened to load.

    The implied volatility series is rebuilt from each split window's own
    timestamps, so it cannot slip out of step with the bars it prices.
    """
    if family.dataset == "vrp":
        symbols = list(data.VOL_INDEX_UNDERLYING.values())
        bars: dict[str, data.OhlcBars] = {
            s: data.load_vrp_ohlc(s, data_dir) for s in symbols}
        bars = {s: b for s, b in bars.items() if len(b)}
        if not bars:
            return None
        train, val = data.split_all_ohlc(bars, data.TRAIN_FRACTION)
    else:
        loaded = {s: data.load_ohlc("intraday", s, data_dir) for s in cfg.symbols}
        loaded = {s: b for s, b in loaded.items() if len(b)}
        if not loaded:
            return None
        train, val = data.split_all_ohlc(loaded, data.TRAIN_FRACTION)

    def ivs_for(w: dict[str, data.OhlcBars]) -> dict[str, list[float]]:
        out = {}
        for symbol, window in w.items():
            series = data.implied_vol_series(symbol, window.times, data_dir)
            if series is not None:
                out[symbol] = series
        return out

    return (dict(train), dict(val), ivs_for(train), ivs_for(val))


def resolve_family(candidate: dict) -> Family:
    """A candidate.json either names a built-in family or embeds a spec."""
    if candidate.get("spec"):
        from research import blocks
        return blocks.spec_to_family(candidate["spec"])
    return FAMILIES[candidate["family"]]


def _with_exits(signal_candidates_list: list[dict],
                family: Family | None = None) -> list[tuple[dict, dict]]:
    """Pair every signal candidate with every exit candidate.

    A family may bring its own exit grid, in which case it is clamped against
    its own bounds. clamp_tunables exists to keep exits on the LIVE bot's
    rails, and a credit spread never runs on those rails — its best possible
    outcome is below the live take-profit floor, so clamping it there would
    replace every target with one that can never trigger.
    """
    grid = (family.exit_grid if family and family.exit_grid else EXIT_GRID)
    bounds = family.exit_bounds if family else None
    out = []
    for sig in signal_candidates_list:
        for exits in itertools.product(*grid.values()):
            raw = dict(zip(grid.keys(), exits, strict=True))
            clamped = (clamp_params(raw, bounds) if bounds else clamp_tunables(raw))
            out.append((sig, clamped))
    return out


def search_family(family_name: str, cfg: Settings, sp: SimParams = SimParams(),
                  data_dir: Path = data.DATA_DIR,
                  registry_path: Path = registry.DEFAULT_PATH,
                  candidate_path: Path = CANDIDATE_PATH) -> SearchOutcome:
    family = FAMILIES[family_name]
    return _search(family, _with_exits(signal_candidates(family), family), cfg, sp,
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
    windows = load_windows(family, cfg, data_dir)
    if windows is None:
        return SearchOutcome(False, "no cached bars — run `python -m research fetch`",
                             family_name, {}, {}, None, None, None)
    train_w, val_w, train_iv, val_iv = windows
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
                                   family, sig_params, ivs=train_iv)
            registry.log_trial(
                registry_path, family_name, params,
                "train", _score(train_res),
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
    best_train = run_family(train_w, replace(cfg, **best_exit), sp, family, best_sig,
                            ivs=train_iv)

    # One validation run for the single winner, then the incumbent it has to
    # beat on the same bars.
    val_res = run_family(val_w, replace(cfg, **best_exit), sp, family, best_sig,
                         ivs=val_iv)
    baseline_val = _baseline_on(family, val_w, val_iv, cfg, sp)
    registry.log_trial(registry_path, family_name, {**best_sig, **best_exit},
                       "val", _score(val_res),
                       dataset=val_id)

    # The coin-flip control on the same validation bars, sized to the winner's
    # own trade count so the comparison is like-for-like.
    # The control trades the SAME structure as the candidate. A coin flip
    # buying options loses about 10% per trade, so benchmarking a premium
    # seller against it would pass anything that sells.
    val_null = null.null_distribution(
        val_w, replace(cfg, **best_exit), sp, val_res.n, ivs=val_iv,
        actions=null.CREDIT_ACTIONS if family.credit else null.LONG_ACTIONS)

    outcome = _judge(family_name, best_sig, best_exit, best_train, val_res,
                     baseline_val, val_null)
    outcome.spec = spec
    if outcome.accepted:
        _write_candidate(candidate_path, outcome)
    return outcome


def _baseline_on(family: Family, windows: Mapping[str, Window],
                 ivs: Mapping[str, list[float]], cfg: Settings,
                 sp: SimParams) -> SimResult:
    """The incumbent this family must beat on these bars.

    By default that is the live EMA and RSI strategy. A short-premium family
    names another family instead, because comparing a sold spread against a
    bought call answers no useful question — the honest incumbent is the same
    trade with the timing rule removed, i.e. collecting the premium blindly.
    """
    if family.baseline_family:
        incumbent = FAMILIES[family.baseline_family]
        params = signal_candidates(incumbent)[0]
        return run_family(windows, cfg, sp, incumbent, params, ivs=ivs)
    combined = SimResult()
    for symbol, window in windows.items():
        bars = as_ohlc(window)
        combined.trades.extend(
            simulate(bars.closes, bars.times, cfg, sp, symbol=symbol,
                     highs=bars.highs, lows=bars.lows, opens=bars.opens).trades)
    return combined


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
