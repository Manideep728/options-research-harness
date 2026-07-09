# Deferred comprehension questions (Vibe Mode log)

Questions I would have asked at each gate. Answer them whenever; we can grade
them in a future session.

1. **data.py / embargo** — Suppose we removed the embargo and holdout started
   at the exact cut index. Walk through concretely how information from the
   research data would end up influencing the gate's verdict: which
   computation is the culprit, and roughly how many bars are affected?

2. **simulator signal_fn** — `simulate()` now takes an optional `signal_fn`.
   Why does defaulting it to `None` (falling back to `signal_series`) keep all
   existing tests green? And what specific dishonesty becomes possible if a
   family's signal function reads `closes[i+1:]` when deciding the signal for
   bar `i`?

3. **registry.py** — Why must the trial registry be append-only, and why does
   `trial_count()` count unique (family, params, window) keys instead of
   raw lines?

4. **metrics.py** — The deflated Sharpe ratio compares your Sharpe against the
   Sharpe you'd expect the *best of N random tries* to have. What happens to
   the DSR of the same fixed returns as the registry's trial count grows, and
   why is that exactly the behavior we want?

5. **families.py** — Every family clamps its params into bounds, like
   `TUNABLE_BOUNDS`. The search is mechanical and only generates grid values
   that are already inside bounds — so what future failure mode do the clamps
   actually protect against?

6. **search.py** — Validation is simulated only for the single train-winner,
   never for the other ~240 candidates. What could a future maintainer do
   wrong if val scores for *all* candidates were sitting in the registry?

7. **failure_report.py** — The failure report deliberately uses only TRAIN
   trades, never validation trades. What leak does that prevent, given that a
   human reads the report and then designs the next family?

8. **robustness.py** — The perturbation check re-runs the winner with theta
   halved and doubled. Describe one concrete "strategy" the search could find
   that looks profitable only because theta is a constant 5%/day in the
   simulator — and how the perturbation exposes it.

9. **gate.py** — The holdout window is marked burned even when the gate
   PASSES. Why burn it on success too?

10. **pipeline** — In one sentence each: which component prevents (a) testing
    the same idea twice and pretending it's new evidence, (b) the
    iterate-until-lucky trap, (c) the simulator being gamed, (d) peeking at
    the final exam?
