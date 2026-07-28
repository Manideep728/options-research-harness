"""Backtest / self-tune CLI.

  python backtest.py                 # replay current params over history
  python backtest.py --days 180     # longer window
  python backtest.py --tune         # grid search + walk-forward validation;
                                    # writes tuned_params.json ONLY if all
                                    # hard guidelines pass (see bot/tuner.py)
"""

import argparse
import logging
import sys
from datetime import UTC, datetime, timedelta
from typing import cast

from alpaca.data.enums import DataFeed
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.models import BarSet
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

from bot.config import Settings
from bot.simulator import SimResult, simulate
from bot.tuner import tune, write_tuned_params

logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(name)s: %(message)s")
log = logging.getLogger("backtest")


def fetch_bars(cfg: Settings, days: int) -> dict[str, tuple[list[float], list]]:
    client = StockHistoricalDataClient(cfg.api_key, cfg.secret_key)
    start = datetime.now(UTC) - timedelta(days=days)
    out: dict[str, tuple[list[float], list]] = {}
    for sym in cfg.symbols:
        bars = cast(BarSet, client.get_stock_bars(
            StockBarsRequest(
                symbol_or_symbols=sym,
                timeframe=TimeFrame(cfg.bar_timeframe_minutes, TimeFrameUnit.Minute),
                start=start,
                feed=DataFeed.IEX,
            )
        )).data.get(sym, [])
        closes = [float(b.close) for b in bars]
        times = [b.timestamp for b in bars]
        log.info("%s: %d bars (%s -> %s)", sym, len(closes),
                 times[0].date() if times else "-", times[-1].date() if times else "-")
        out[sym] = (closes, times)
    return out


def print_report(title: str, result: SimResult) -> None:
    print(f"\n=== {title} ===")
    if result.n == 0:
        print("no trades")
        return
    exits: dict[str, int] = {}
    for t in result.trades:
        exits[t.reason] = exits.get(t.reason, 0) + 1
    print(f"trades:        {result.n}")
    print(f"win rate:      {result.win_rate:.1%}")
    print(f"expectancy:    {result.expectancy:+.2%} of premium per trade")
    print(f"profit factor: {result.profit_factor:.2f}")
    print(f"max drawdown:  {result.max_drawdown:.2f} premium units")
    print(f"exits:         {exits}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=120, help="history window (default 120)")
    parser.add_argument("--tune", action="store_true", help="run guarded self-tuning")
    args = parser.parse_args()

    cfg = Settings.load()
    if not cfg.api_key or not cfg.secret_key:
        log.error("Alpaca keys required in .env (historical data needs auth)")
        return 1

    bars = fetch_bars(cfg, args.days)
    total_bars = sum(len(c) for c, _ in bars.values())
    if total_bars == 0:
        log.error("no bars returned; check keys / connectivity")
        return 1

    for sym, (closes, times) in bars.items():
        print_report(f"{sym} — current params", simulate(closes, times, cfg))
    combined = SimResult()
    for closes, times in bars.values():
        combined.trades.extend(simulate(closes, times, cfg).trades)
    combined.trades.sort(key=lambda t: t.entry_time)
    print_report("ALL SYMBOLS — current params", combined)

    if not args.tune:
        return 0

    print("\nrunning guarded tune (grid + walk-forward validation)...")
    outcome = tune(bars, cfg)
    print(f"\ntune outcome: {'ACCEPTED' if outcome.accepted else 'REJECTED'} — {outcome.reason}")
    if outcome.best_val is not None:
        print(f"candidate params: {outcome.params}")
        print_report("candidate — validation window", outcome.best_val)
        print_report("current — validation window", outcome.current_val)
    if outcome.accepted:
        write_tuned_params(cfg.tuned_params_file, outcome)
        print(f"\nwrote {cfg.tuned_params_file} — restart the bot to apply.")
    else:
        print("\ncurrent parameters stand (guidelines not met).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
