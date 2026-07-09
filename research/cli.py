"""Research loop CLI: python -m research <fetch|search|report|robustness|gate>

The intended cycle:
    fetch  -> cache bars (once, and again when the holdout must roll forward)
    search -> select-on-train winner for one family, judged on validation
    report -> failure analysis of the winner's TRAIN trades (human reads this)
    robustness -> SimParams perturbation + daily regime sign-check
    gate   -> the burn-once holdout verdict (refuses a burned window)
"""

import argparse
import logging
import sys
from dataclasses import replace
from pathlib import Path

from bot.config import Settings
from bot.simulator import SimParams, SimResult

from research import data, failure_report, gate, registry, robustness, search
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

    p_search = sub.add_parser("search", help="search one family, select on train")
    p_search.add_argument("--family", choices=sorted(FAMILIES), required=True)

    sub.add_parser("report", help="failure analysis of the candidate (train trades)")
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
        outcome = search.search_family(args.family, cfg, data_dir=args.data_dir,
                                       registry_path=args.registry,
                                       candidate_path=args.candidate)
        print(f"\nsearch {args.family}: {'ACCEPTED' if outcome.accepted else 'REJECTED'} "
              f"— {outcome.reason}")
        if outcome.train is not None:
            print(f"winner: signal={outcome.signal_params} exits={outcome.exit_params}")
            _print_result("train", outcome.train)
            _print_result("validation", outcome.val)
            _print_result("baseline (live strategy) on validation", outcome.baseline_val)
        print(f"registry now holds {registry.trial_count(args.registry)} unique trials")
        return 0

    candidate = _require_candidate(args.candidate)
    family = FAMILIES[candidate["family"]]
    sig, exits = candidate["signal_params"], candidate.get("exit_params", {})
    run_cfg = replace(cfg, **exits)

    if args.command == "report":
        bars = {s: data.load_bars("intraday", s, args.data_dir) for s in cfg.symbols}
        bars = {s: b for s, b in bars.items() if b[0]}
        train_w, _ = search.split_all(bars)
        result = search.run_family(train_w, run_cfg, SimParams(), family, sig)
        text = failure_report.build_report(
            result.trades, train_w,
            ema_fast=int(sig.get("ema_fast", cfg.ema_fast)),
            ema_slow=int(sig.get("ema_slow", cfg.ema_slow)),
        )
        print(text)
        REPORT_PATH.write_text(text, encoding="utf-8")
        print(f"\nsaved to {REPORT_PATH}")
        return 0

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
        print("daily regime folds:")
        for row in r_rows:
            print(f"  {row.label:<8} n={row.trades:<4} expectancy={row.expectancy:+.2%}")
        print(f"\nperturbation: {'PASS' if p_ok else 'FAIL'}   "
              f"regime: {'PASS' if r_ok else 'FAIL'}")
        return 0 if (p_ok and r_ok) else 1

    if args.command == "gate":
        outcome = gate.run_gate(candidate, cfg, data_dir=args.data_dir,
                                registry_path=args.registry)
        if outcome.refused:
            print(f"REFUSED: {outcome.reason}")
            return 1
        print(f"\ngate: {'PASSED' if outcome.passed else 'FAILED'} — {outcome.reason}")
        _print_result("holdout", outcome.result)
        if outcome.deflated_sharpe is not None:
            print(f"deflated Sharpe confidence: {outcome.deflated_sharpe:.3f}")
        print(f"holdout window {outcome.window_id} is now burned.")
        if outcome.passed:
            print("\nNext step is HUMAN judgment, not auto-deploy: paper-trade this "
                  "candidate for several weeks before considering promotion.")
            if candidate["family"] == "baseline":
                print("(baseline family: params map onto tuned_params.json directly)")
            else:
                print("(non-baseline family: live deployment needs a code change)")
        return 0 if outcome.passed else 1

    return 1


if __name__ == "__main__":
    sys.exit(main())
