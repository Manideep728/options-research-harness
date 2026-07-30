"""Research loop CLI: python -m research <fetch|search|report|null|replay|robustness|gate>

The intended cycle:
    fetch  -> cache bars (once, and again when the holdout must roll forward)
    search -> select-on-train winner for one family or spec, judged on validation
    report -> failure analysis of the winner's TRAIN trades (human reads this)
    propose -> Claude turns the report into the next spec (--offline: prompt only)
    null   -> what a coin flip earns on the same bars (the sanity check)
    replay -> run the REAL engine over cached bars, with every risk cap active
    robustness -> SimParams perturbation + daily regime check vs the null
    gate   -> the burn-once holdout verdict (refuses a burned window)
"""

import argparse
import json
import logging
import statistics as stats_mod
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

from bot.config import Settings
from bot.simulator import SimParams, SimResult, simulate
from research import (
    data,
    failure_report,
    gate,
    null,
    proposer,
    registry,
    replay,
    robustness,
    search,
)
from research.families import FAMILIES

log = logging.getLogger("research")

REPORT_PATH = Path(__file__).resolve().parent / "report.txt"


def _print_result(title: str, result: SimResult | None) -> None:
    if result is None or result.n == 0:
        print(f"{title}: no trades")
        return
    print(f"{title}: trades={result.n} expectancy={result.expectancy:+.2%} "
          f"win_rate={result.win_rate:.1%} profit_factor={result.profit_factor:.2f}")


def _require_candidate(path: Path) -> dict:
    candidate = search.load_candidate(path)
    if candidate is None:
        print(f"no candidate at {path} — run `python -m research search` first "
              "(only an ACCEPTED search winner is written there)")
        raise SystemExit(1)
    return candidate


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)-7s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(prog="python -m research", description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=data.DATA_DIR)
    parser.add_argument("--registry", type=Path, default=registry.DEFAULT_PATH)
    parser.add_argument("--candidate", type=Path, default=search.CANDIDATE_PATH)
    sub = parser.add_subparsers(dest="command", required=True)

    p_fetch = sub.add_parser("fetch", help="download + cache bars, split holdout")
    p_fetch.add_argument("--days", type=int, default=365)
    p_fetch.add_argument("--daily-years", type=int, default=5)
    p_fetch.add_argument("--symbols", type=str, default="",
                         help="comma-separated subset (default: full universe)")

    p_search = sub.add_parser("search", help="search one family or spec, select on train")
    group = p_search.add_mutually_exclusive_group(required=True)
    group.add_argument("--family", choices=sorted(FAMILIES))
    group.add_argument("--spec", type=Path, help="path to a proposal spec JSON")

    sub.add_parser("report", help="failure analysis of the candidate (train trades)")
    p_propose = sub.add_parser(
        "propose", help="Claude proposes the next spec from the failure report")
    p_propose.add_argument("--offline", action="store_true",
                           help="write the prompt to a file instead of calling the API")
    p_propose.add_argument("--report-file", type=Path, default=REPORT_PATH)
    p_replay = sub.add_parser(
        "replay", help="run the REAL engine over cached bars (all risk caps active)")
    p_replay.add_argument("--window", choices=("train", "val"), default="val")
    p_replay.add_argument("--work-dir", type=Path, default=None,
                          help="where the engine writes state/journal/shortlist "
                               "(default: a temp dir). NEVER point this at the repo "
                               "root — it would overwrite the live bot's state.")
    p_replay.add_argument("--compare", action="store_true",
                          help="also run the unconstrained simulator for contrast")

    p_null = sub.add_parser(
        "null", help="coin-flip control on the same bars as the candidate")
    p_null.add_argument("--window", choices=("train", "val", "daily"), default="val")
    p_null.add_argument("--seeds", type=int, default=null.DEFAULT_SEEDS)
    p_null.add_argument("--trades", type=int, default=0,
                        help="size the null to this trade count instead of the "
                             "candidate's — lets you measure a window the "
                             "candidate produces no trades on")
    sub.add_parser("robustness", help="perturbation + daily regime checks")
    sub.add_parser("gate", help="burn-once holdout verdict")

    args = parser.parse_args(argv)
    cfg = Settings.load()

    if args.command == "fetch":
        if not cfg.api_key or not cfg.secret_key:
            print("Alpaca keys required in .env for fetching")
            return 1
        if args.symbols:
            cfg = replace(cfg, symbols=tuple(s.strip().upper()
                                             for s in args.symbols.split(",") if s.strip()))
        data.fetch_all(cfg, intraday_days=args.days, daily_years=args.daily_years,
                       data_dir=args.data_dir)
        return 0

    if args.command == "search":
        if args.spec:
            spec = json.loads(Path(args.spec).read_text(encoding="utf-8"))
            outcome = search.search_spec(spec, cfg, data_dir=args.data_dir,
                                         registry_path=args.registry,
                                         candidate_path=args.candidate)
        else:
            outcome = search.search_family(args.family, cfg, data_dir=args.data_dir,
                                           registry_path=args.registry,
                                           candidate_path=args.candidate)
        print(f"\nsearch {outcome.family}: {'ACCEPTED' if outcome.accepted else 'REJECTED'} "
              f"— {outcome.reason}")
        if outcome.train is not None:
            print(f"winner: signal={outcome.signal_params} exits={outcome.exit_params}")
            _print_result("train", outcome.train)
            _print_result("validation", outcome.val)
            _print_result("baseline (live strategy) on validation", outcome.baseline_val)
        if outcome.val_null is not None and outcome.val_null.seeds and outcome.val is not None:
            n = outcome.val_null
            print(f"coin-flip null on validation: {n.seeds} seeds, mean={n.mean:+.2%}, "
                  f"p{int(null.NULL_QUANTILE * 100)}={n.threshold:+.2%} — candidate at the "
                  f"{n.percentile_of(outcome.val.expectancy):.1%} percentile")
        print(f"registry now holds {registry.trial_count(args.registry)} unique trials")
        return 0

    if args.command == "propose":
        result = proposer.propose(args.report_file, registry_path=args.registry,
                                  offline=args.offline)
        if result is None:
            return 1
        if "offline_prompt" in result:
            print(f"prompt written to {result['offline_prompt']} — feed it to any LLM,"
                  f"\nsave the spec JSON, then run: python -m research search --spec <file>")
            return 0
        print(f"\nproposal: {result['name']} — {result['hypothesis']}")
        print(f"saved to {result['_path']}")
        print(f"next: python -m research search --spec {result['_path']}")
        return 0

    if args.command == "replay":
        bars = {s: data.load_bars("intraday", s, args.data_dir) for s in cfg.symbols}
        bars = {s: b for s, b in bars.items() if b[0]}
        if not bars:
            print("no cached bars — run `python -m research fetch`")
            return 1
        train_w, val_w = search.split_all(bars)
        windows = train_w if args.window == "train" else val_w

        # The engine writes state.json / trades.csv / active.json / control.json
        # at the paths in cfg. Redirect them, or a replay would clobber the live
        # bot's files.
        work = Path(args.work_dir) if args.work_dir else Path(tempfile.mkdtemp(
            prefix="replay-"))
        work.mkdir(parents=True, exist_ok=True)
        run_cfg = replace(
            cfg,
            state_file=str(work / "state.json"),
            journal_file=str(work / "trades.csv"),
            control_file=str(work / "control.json"),
            active_file=str(work / "active.json"),
            earnings_file=str(work / "earnings.json"),
        )
        print(f"replaying the live engine over the {args.window} window "
              f"({len(windows)} symbols); engine state in {work}")
        replay_stats = replay.run_replay(windows, run_cfg)
        returns = replay_stats.realized_returns
        print(f"\ncycles={replay_stats.cycles} buys={replay_stats.buys} "
              f"sells={replay_stats.sells} cancels={replay_stats.cancels}")
        if not returns:
            print("no completed round trips — the engine's caps may have blocked "
                  "every entry, which is itself a result")
            return 0
        print(f"round trips={len(returns)} expectancy={stats_mod.fmean(returns):+.2%} "
              f"win_rate={sum(1 for r in returns if r > 0) / len(returns):.1%}")

        if args.compare:
            sim: list[float] = []
            for symbol, (closes, times) in windows.items():
                sim += [t.pnl_pct for t in
                        simulate(closes, times, cfg, SimParams(), symbol=symbol).trades]
            if sim:
                print("\nthe same bars, through the UNCONSTRAINED simulator:")
                print(f"  simulate()   trades={len(sim):<5} "
                      f"expectancy={stats_mod.fmean(sim):+.2%}")
                print(f"  real engine  trades={len(returns):<5} "
                      f"expectancy={stats_mod.fmean(returns):+.2%}")
                print(f"\nThe engine took {len(returns) / len(sim):.1%} of the "
                      "simulator's trades. Everything simulate() reports is about a "
                      "system that trades far more often than the live bot: it has no "
                      "max_positions, no max_trades_per_day, no cooldown, no "
                      "shortlist, no session window and no liquidity gate.")
        return 0

    candidate = _require_candidate(args.candidate)
    family = search.resolve_family(candidate)
    sig, exits = candidate["signal_params"], candidate.get("exit_params", {})
    run_cfg = replace(cfg, **exits)

    if args.command == "report":
        bars = {s: data.load_bars("intraday", s, args.data_dir) for s in cfg.symbols}
        bars = {s: b for s, b in bars.items() if b[0]}
        train_w, _ = search.split_all(bars)
        train_result = search.run_family(train_w, run_cfg, SimParams(), family, sig)
        text = failure_report.build_report(
            train_result.trades, train_w,
            ema_fast=int(sig.get("ema_fast", cfg.ema_fast)),
            ema_slow=int(sig.get("ema_slow", cfg.ema_slow)),
        )
        print(text)
        REPORT_PATH.write_text(text, encoding="utf-8")
        print(f"\nsaved to {REPORT_PATH}")
        return 0

    if args.command == "null":
        if args.window == "daily":
            windows = {s: data.load_bars("daily", s, args.data_dir) for s in cfg.symbols}
        else:
            bars = {s: data.load_bars("intraday", s, args.data_dir) for s in cfg.symbols}
            bars = {s: b for s, b in bars.items() if b[0]}
            train_w, val_w = search.split_all(bars)
            windows = train_w if args.window == "train" else val_w
        windows = {s: b for s, b in windows.items() if b[0]}
        candidate_result = search.run_family(windows, run_cfg, SimParams(), family, sig)
        target = args.trades or candidate_result.n
        summary = null.null_distribution(windows, run_cfg, SimParams(),
                                         target, seeds=args.seeds)
        print(f"\n{args.window} window — candidate vs coin flip")
        _print_result("candidate", candidate_result)
        if summary.seeds == 0:
            print("null: no trades on any seed — nothing to compare against."
                  + ("" if args.trades else " Pass --trades N to size the null "
                     "directly when the candidate produces no trades here."))
            return 1
        print(f"null: {summary.seeds} seeds, fire_rate={summary.fire_rate:.5f}, "
              f"mean trades={summary.mean_trades:.0f}")
        print(f"  mean expectancy      {summary.mean:+.2%}")
        print(f"  p{int(null.NULL_QUANTILE * 100)} threshold        "
              f"{summary.threshold:+.2%}")
        beats = summary.beats(candidate_result.expectancy)
        print(f"  candidate percentile {summary.percentile_of(candidate_result.expectancy):.1%}"
              f"  -> {'BEATS' if beats else 'DOES NOT BEAT'} the null")
        # A negative null mean is expected and healthy: it is the roundtrip
        # cost plus theta, which is exactly what a signal-free strategy pays.
        # A POSITIVE null mean is the pathology — it means the P&L model hands
        # out money for taking risk, with no forecast involved.
        if summary.mean > 0.02:
            print(f"\nWARNING: a coin flip EARNS {summary.mean:+.2%} per trade here. A "
                  "signal-free strategy should pay the spread, not collect it, so the P&L "
                  "model is not valid on these bars — treat every number computed on them "
                  "as unusable, not merely optimistic.")
        return 0 if beats else 1

    if args.command == "robustness":
        bars = {s: data.load_bars("intraday", s, args.data_dir) for s in cfg.symbols}
        bars = {s: b for s, b in bars.items() if b[0]}
        _, val_w = search.split_all(bars)
        p_ok, p_rows = robustness.check_perturbations(val_w, run_cfg, family, sig)
        print("perturbation grid (validation window):")
        for row in p_rows:
            print(f"  {row.label:<32} n={row.trades:<4} expectancy={row.expectancy:+.2%}")
        r_ok, r_rows = robustness.daily_regime_check(run_cfg, family, sig,
                                                     data_dir=args.data_dir)
        print("daily regime folds (each must beat its own coin-flip null):")
        for row in r_rows:
            bar = ("not countable" if row.null_threshold is None
                   else f"null p95={row.null_threshold:+.2%} (null n={row.null_trades:.0f})")
            print(f"  {row.label:<8} n={row.trades:<4} expectancy={row.expectancy:+.2%}"
                  f"   {bar}")
        print(f"\nperturbation: {'PASS' if p_ok else 'FAIL'}   "
              f"regime: {'PASS' if r_ok else 'FAIL'}")
        return 0 if (p_ok and r_ok) else 1

    if args.command == "gate":
        gate_outcome = gate.run_gate(candidate, cfg, data_dir=args.data_dir,
                                     registry_path=args.registry)
        if gate_outcome.refused:
            print(f"REFUSED: {gate_outcome.reason}")
            return 1
        print(f"\ngate: {'PASSED' if gate_outcome.passed else 'FAILED'} — {gate_outcome.reason}")
        _print_result("holdout", gate_outcome.result)
        if gate_outcome.deflated_sharpe is not None:
            print(f"deflated Sharpe confidence: {gate_outcome.deflated_sharpe:.3f}")
        print(f"holdout window {gate_outcome.window_id} is now burned.")
        if gate_outcome.passed:
            print("\nNext step is HUMAN judgment, not auto-deploy: paper-trade this "
                  "candidate for several weeks before considering promotion.")
            if candidate["family"] == "baseline":
                print("(baseline family: params map onto tuned_params.json directly)")
            else:
                print("(non-baseline family: live deployment needs a code change)")
        return 0 if gate_outcome.passed else 1

    return 1


if __name__ == "__main__":
    sys.exit(main())
