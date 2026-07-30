"""The burn-once out-of-sample gate — the final exam, taken exactly once.

This is the only module allowed to read the holdout data. Every attempt is
recorded in the registry keyed to the holdout window's date range; a second
attempt against the same window is REFUSED, pass or fail. That refusal is
the whole point: the moment holdout data can influence the next iteration,
it stops being out-of-sample and its verdict means nothing.

Fail branch (decided in advance, when no result was on the line): the
research line is discarded. The next gate attempt needs a holdout window
that has rolled forward — re-fetch after >= 1 month of new market data.

A pass prints the evidence for MANUAL review. Nothing auto-deploys.
"""

import logging
from dataclasses import dataclass
from pathlib import Path

from bot.config import Settings
from bot.simulator import SimParams, SimResult
from bot.tuner import MIN_TRADES
from research import data, metrics, registry
from research.search import resolve_family, run_family

log = logging.getLogger("research.gate")

DSR_CONFIDENCE = 0.95  # required P(not just the luckiest of N tries)


@dataclass
class GateOutcome:
    refused: bool          # window already burned — no verdict rendered
    passed: bool
    reason: str
    window_id: str
    result: SimResult | None
    deflated_sharpe: float | None


def run_gate(candidate: dict, cfg: Settings, sp: SimParams = SimParams(),
             data_dir: Path = data.DATA_DIR,
             registry_path: Path = registry.DEFAULT_PATH) -> GateOutcome:
    family = resolve_family(candidate)
    signal_params = candidate["signal_params"]
    exit_params = candidate.get("exit_params", {})

    holdout = {s: data.load_holdout_bars(s, data_dir) for s in cfg.symbols}
    holdout = {s: b for s, b in holdout.items() if b[0]}
    if not holdout:
        return GateOutcome(False, False, "no holdout data cached", "", None, None)

    window_id = registry.window_id(holdout)
    if registry.gate_burned(registry_path, window_id):
        return GateOutcome(
            True, False,
            f"holdout window {window_id} is already burned — this data has been "
            "seen and cannot judge again. Re-fetch after new data accrues.",
            window_id, None, None,
        )

    from dataclasses import replace
    result = run_family(holdout, replace(cfg, **exit_params), sp, family, signal_params)
    returns = [t.pnl_pct for t in result.trades]
    dsr = metrics.deflated_sharpe(returns, registry.trial_sharpes(registry_path))

    if result.n < MIN_TRADES:
        passed, reason = False, f"too few holdout trades ({result.n} < {MIN_TRADES})"
    elif result.expectancy <= 0:
        passed, reason = False, f"holdout expectancy {result.expectancy:+.3f} <= 0"
    elif dsr < DSR_CONFIDENCE:
        passed, reason = False, (
            f"deflated Sharpe confidence {dsr:.3f} < {DSR_CONFIDENCE} — "
            "indistinguishable from the luckiest of "
            f"{registry.trial_count(registry_path)} trials"
        )
    else:
        passed, reason = True, "passed the out-of-sample gate"

    scores = metrics.summarize(returns)
    scores["deflated_sharpe"] = round(dsr, 4)
    registry.log_gate(registry_path, candidate["family"],
                      {**signal_params, **exit_params}, window_id, scores, passed)
    log.info("gate %s: %s (window %s now burned)",
             "PASSED" if passed else "FAILED", reason, window_id)
    return GateOutcome(False, passed, reason, window_id, result, dsr)
