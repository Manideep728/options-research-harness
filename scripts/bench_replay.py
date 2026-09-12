"""Throughput benchmark for the replay engine.

Measures how fast `research/replay.py` can drive the REAL `Engine.run_cycle()`
— indicators, universe ranking, risk gates, contract selection — over cached
bars.

Bars are synthesized rather than fetched. That is deliberate and does not
flatter the result: the engine does the same work per bar regardless of whether
the prices are real, and `research/data/` is gitignored, so a benchmark that
required a fetch could not be re-run by anyone reading the repo. What synthetic
prices *do* change is how many entry signals fire, which changes the order
bookkeeping; `--seed` is reported so a run is reproducible, and the trade counts
are printed so you can see whether a run was signal-heavy or signal-light.

Usage:
    .venv/bin/python scripts/bench_replay.py
    .venv/bin/python scripts/bench_replay.py --sessions 250 --symbols 30
"""

from __future__ import annotations

import argparse
import random
import statistics
import sys
import tempfile
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

# Run directly (`python scripts/bench_replay.py`) rather than as a module, so
# the repo root has to go on the path before `bot` and `research` resolve.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.config import Settings
from research import replay
from research.data import in_session

BAR_MINUTES = 15


def session_timestamps(sessions: int) -> list[datetime]:
    """Start-of-bar stamps for `sessions` weekdays, regular hours only.

    Generates a dense 15-minute grid and filters with the same `in_session`
    predicate the loader uses, so the benchmark cannot drift from what the
    engine would actually be handed.
    """
    out: list[datetime] = []
    day = datetime(2024, 1, 2, tzinfo=UTC)  # a Tuesday
    seen_days = 0
    while seen_days < sessions:
        if day.weekday() < 5:
            stamps = [
                day + timedelta(minutes=m)
                for m in range(0, 24 * 60, BAR_MINUTES)
            ]
            in_hours = [t for t in stamps if in_session(t)]
            if in_hours:
                out.extend(in_hours)
                seen_days += 1
        day += timedelta(days=1)
    return out


def random_walk(n: int, rng: random.Random, start: float = 100.0) -> list[float]:
    """A drifting random walk. Volatility is set so 15-minute moves are of a
    realistic order (~0.15%), because the scanner scores on recent volatility
    and a degenerate flat series would rank every symbol at zero."""
    price = start
    out = []
    for _ in range(n):
        price *= 1.0 + rng.gauss(0.0, 0.0015)
        out.append(max(price, 1.0))
    return out


def build_bars(symbols: tuple[str, ...], times: list[datetime],
               rng: random.Random) -> dict[str, tuple[list[float], list[datetime]]]:
    return {s: (random_walk(len(times), rng, 50.0 + rng.random() * 200.0), times)
            for s in symbols}


def scratch_config(symbols: tuple[str, ...]) -> Settings:
    """The engine writes state/journal/control/active files at the paths in
    cfg. Point them at a temp dir or a benchmark would clobber the live bot's
    files — the same precaution `research/cli.py` takes for replay."""
    work = Path(tempfile.mkdtemp(prefix="bench-replay-"))
    return replace(
        Settings(api_key="", secret_key="", symbols=symbols),
        state_file=str(work / "state.json"),
        journal_file=str(work / "trades.csv"),
        control_file=str(work / "control.json"),
        active_file=str(work / "active.json"),
        earnings_file=str(work / "earnings.json"),
    )


def run_once(sessions: int, symbols: tuple[str, ...], seed: int) -> dict[str, float]:
    rng = random.Random(seed)
    times = session_timestamps(sessions)
    bars = build_bars(symbols, times, rng)
    cfg = scratch_config(symbols)

    start = time.perf_counter()
    stats = replay.run_replay(bars, cfg)
    elapsed = time.perf_counter() - start

    rows = len(symbols) * len(times)
    return {
        "elapsed": elapsed,
        "timeline": float(len(times)),
        "cycles": float(stats.cycles),
        "rows": float(rows),
        "buys": float(stats.buys),
        "sells": float(stats.sells),
        "rows_per_sec": rows / elapsed,
        "cycles_per_sec": stats.cycles / elapsed,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sessions", type=int, default=250,
                    help="trading days of 15-minute bars (default: 250, ~1 year)")
    ap.add_argument("--symbols", type=int, default=0,
                    help="how many of the configured universe to use (default: all)")
    ap.add_argument("--repeat", type=int, default=3,
                    help="timed runs; the median is reported (default: 3)")
    ap.add_argument("--seed", type=int, default=1729)
    args = ap.parse_args()

    universe = Settings(api_key="", secret_key="").symbols
    symbols = universe[:args.symbols] if args.symbols else universe

    runs = [run_once(args.sessions, symbols, args.seed + i) for i in range(args.repeat)]
    median = statistics.median(r["elapsed"] for r in runs)
    best = min(runs, key=lambda r: abs(r["elapsed"] - median))

    print(f"\nreplay throughput  (seed {args.seed}, {args.repeat} runs, median reported)")
    print(f"  universe          {len(symbols)} symbols")
    print(f"  timeline          {best['timeline']:,.0f} bars "
          f"({args.sessions} sessions x 15-min, regular hours)")
    print(f"  input rows        {best['rows']:,.0f}  (symbols x timeline)")
    print(f"  engine cycles     {best['cycles']:,.0f}  (in-session steps)")
    print(f"  trades            {best['buys']:,.0f} buys / {best['sells']:,.0f} sells")
    print(f"  wall time         {best['elapsed']:.2f}s")
    print()
    print(f"  rows/sec          {best['rows_per_sec']:,.0f}")
    print(f"  cycles/sec        {best['cycles_per_sec']:,.0f}")
    print()
    print("  A 'cycle' is one full Engine.run_cycle(): clock, order reconcile,")
    print("  exit management, risk gates, and (on the scan interval) ranking the")
    print("  whole universe on EMA/RSI/volatility. Quote rows/sec only if you say")
    print("  it means symbol-bars of input, not engine decisions.")
    spread = max(r["elapsed"] for r in runs) - min(r["elapsed"] for r in runs)
    print(f"\n  run-to-run spread {spread:.2f}s "
          f"({spread / median * 100:.1f}% of median)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
