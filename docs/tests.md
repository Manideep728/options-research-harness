# Tests and quality gates

> Part of [options-research-harness](../README.md).

```powershell
.venv\Scripts\python -m pytest tests -q   # 380 tests
.venv\Scripts\python -m ruff check .      # lint
.venv\Scripts\python -m mypy              # type check
cd web; npm run lint; npm run build       # frontend
```

CI runs all five on every push and pull request.

The tests worth reading first are the ones that pin the findings in the
[postmortem](postmortem.md) and the [variance risk premium
arc](variance-risk-premium.md):

- `test_intra_session_barrier_ignores_overshoot_size` — two very different
  overshoots past the same stop must book the same loss. The old code booked
  −0.51 and −1.00 where it now books −0.25 twice.
- `test_regime_guard_rejects_a_coin_flip` — a zero-skill signal must not pass the
  multi-regime check. This is the regression test for the whole audit.
- `test_tail_check_fails_a_book_that_looks_fine_on_average` — 200 wins at +5% and
  one −900% loss. Expectancy says the book is profitable; the tail check fails it.
- `test_vrp_spread_stands_down_in_the_crash_that_iv_rank_leans_into` — the two
  short-premium entry rules must disagree on the case that separates them.
- `test_max_loss_is_a_true_bound_at_every_cost_level` — parametrized over four
  cost levels and both structures. A spread that loses more than its stated
  bound is not defined-risk, and the first version lost 102% of it.
- `test_get_closes_never_returns_a_bar_at_or_after_now` — the replay causality
  contract, checked at every step instead of once.
- `test_positions_reprice_through_the_shared_option_model` — replay must not grow
  a second P&L model.
- `test_daily_bars_are_never_session_filtered` — the session filter would delete
  the entire daily cache if it were applied to 00:00 ET daily timestamps.
- `test_in_session_follows_dst_not_a_fixed_utc_offset` — 13:30 UTC is 09:30 EDT
  in July and 08:30 EST in January; a hard-coded window is wrong half the year.
- `test_same_params_on_a_different_dataset_is_a_different_trial` — the stale
  score-reuse bug.
- `test_mutating_route_rejected_without_csrf_header` — parametrized across every
  mutating dashboard route. I removed the guard and confirmed the suite fails.

The rest cover indicator math (including a known Wilder RSI value), signal
triggers, contract filters, every risk gate, journal P&L matching (incl.
partial-fill FIFO), the simulator's equivalence with the live signal at every
bar prefix, tuner guardrails (clamping, non-tunable risk caps, thin-evidence
rejection, train-then-validate selection), scanner ranking, the earnings
blackout, shortlist persistence, the dashboard snapshot, full engine cycles
against a fake broker (entries, exits, order reconciliation, stale-order
cancels, max-hold via journal, two-tier ranking, cooldown, blackout), the
engine PID lock, the atomic file writer, and the research loop (holdout
splits/embargo, deflated Sharpe, burn-once gate, select-on-train search,
registry-aware skip).
