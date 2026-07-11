"""The LLM proposer: turns failure evidence into the next spec to test.

Claude reads the failure report, the registry's history of what has already
been tried, and the block vocabulary — then proposes ONE new spec (JSON,
schema-enforced via structured outputs). The spec is validated and clamped
by research.blocks before anything runs; an invalid proposal gets exactly
one retry with the validation errors attached, then the attempt fails.

Guardrails that stay outside the LLM's reach: it cannot write code (specs
only combine existing blocks), cannot exceed PARAM_BOUNDS or MAX_CANDIDATES,
and cannot touch the gate — searching, robustness, and the burn-once holdout
verdict remain separate, human-invoked steps.

No API credentials? `--offline` writes the exact prompt to a file so any
LLM (or you) can produce the spec by hand.
"""

import json
import logging
from pathlib import Path

from research import blocks, registry

log = logging.getLogger("research.proposer")

MODEL = "claude-opus-4-8"
PROPOSALS_DIR = Path(__file__).resolve().parent / "proposals"
PROMPT_PATH = Path(__file__).resolve().parent / "proposal_prompt.txt"

# Structured-outputs schema: the API guarantees the response parses to this
# shape; research.blocks.validate_spec then checks the semantics.
SPEC_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "hypothesis": {"type": "string"},
        "trend": {"type": "string", "enum": list(blocks.TRENDS)},
        "trigger": {"type": "string", "enum": list(blocks.TRIGGERS)},
        "filters": {"type": "array",
                    "items": {"type": "string", "enum": list(blocks.FILTERS)}},
        "grid": {
            "type": "object",
            "properties": {
                **{param: {"type": "array", "items": {"type": "number"}}
                   for param in blocks.PARAM_BOUNDS},
                "entry_hours": {"type": "array",
                                "items": {"type": "array",
                                          "items": {"type": "integer"}}},
            },
            "additionalProperties": False,
        },
    },
    "required": ["name", "hypothesis", "trend", "trigger", "filters", "grid"],
    "additionalProperties": False,
}


def build_prompt(report_text: str, registry_path: Path) -> str:
    bounds = "\n".join(f"  {k}: [{lo}, {hi}]" for k, (lo, hi) in blocks.PARAM_BOUNDS.items())
    return f"""You are the strategy proposer in a guarded quantitative research loop
for an options paper-trading bot (long calls/puts on 15-minute bars).

Propose ONE new strategy family as a spec, motivated by the failure evidence
below. You are not writing code: a spec combines fixed building blocks.

Blocks:
- trend (pick one): {", ".join(blocks.TRENDS)}
- trigger (pick one): {", ".join(blocks.TRIGGERS)}
- filters (pick any, or none): {", ".join(blocks.FILTERS)}
  (max/min_entry_vol compare the rolling stdev of the last {blocks.VOL_LOOKBACK}
  bar returns at entry; entry_hours restricts entries to listed UTC hours)

Every block's params need a grid list. Required params per block:
{json.dumps({k: v for k, v in blocks.REQUIRED_PARAMS.items() if v}, indent=2)}

Bounds (values outside are clamped):
{bounds}

Rules:
- ONE spec only. Keep the grid small and targeted (< {blocks.MAX_CANDIDATES}
  combos): 2-3 values per param, not a sweep of everything. Each extra
  candidate raises the deflated-Sharpe bar for the whole research program.
- The hypothesis must cite specific evidence from the failure report, not a
  generic idea. Do not tune params to fix individual report buckets — that
  is curve fitting; propose a structural rule the evidence supports.
- ema_slow - ema_fast must exceed 3 for every grid combination.
- calls_only and puts_only are mutually exclusive.

What has been tried already ({registry.trial_count(registry_path)} unique trials):
{_registry_summary(registry_path)}

=== FAILURE REPORT (train trades of the last searched winner) ===
{report_text}
=== END REPORT ===

Respond with the spec JSON only."""


def _registry_summary(registry_path: Path) -> str:
    best: dict[str, float] = {}
    for e in registry.entries(registry_path):
        if e.get("kind") != "trial":
            continue
        family = e.get("family", "?")
        exp = e.get("scores", {}).get("expectancy")
        if exp is not None:
            best[family] = max(best.get(family, float("-inf")), float(exp))
    if not best:
        return "  (nothing yet)"
    return "\n".join(f"  {name}: best train expectancy {exp:+.2%}"
                     for name, exp in sorted(best.items()))


def propose(report_path: Path, registry_path: Path = registry.DEFAULT_PATH,
            proposals_dir: Path = PROPOSALS_DIR, offline: bool = False,
            prompt_path: Path = PROMPT_PATH, client=None) -> dict | None:
    """Returns the validated spec (also saved to proposals_dir), the prompt
    path in offline mode (as {"offline_prompt": path}), or None on failure."""
    report_file = Path(report_path)
    if not report_file.exists():
        log.error("no failure report at %s — run `python -m research report` first",
                  report_path)
        return None
    prompt = build_prompt(report_file.read_text(encoding="utf-8"), registry_path)

    if offline:
        Path(prompt_path).write_text(prompt, encoding="utf-8")
        log.info("offline mode: prompt written to %s", prompt_path)
        return {"offline_prompt": str(prompt_path)}

    if client is None:
        try:
            import anthropic
            client = anthropic.Anthropic()
        except Exception as exc:
            log.error("could not create an Anthropic client (%s) — set "
                      "ANTHROPIC_API_KEY in .env or use `propose --offline`", exc)
            return None

    messages = [{"role": "user", "content": prompt}]
    for attempt in range(2):  # one retry, with validation errors attached
        try:
            response = client.messages.create(
                model=MODEL,
                max_tokens=16000,
                thinking={"type": "adaptive"},
                output_config={"format": {"type": "json_schema", "schema": SPEC_SCHEMA}},
                messages=messages,
            )
        except Exception as exc:
            log.error("Claude API call failed (%s) — check credentials or use "
                      "`propose --offline`", exc)
            return None
        if response.stop_reason == "refusal":
            log.error("proposal request was refused")
            return None
        text = next((b.text for b in response.content if b.type == "text"), "")
        spec = json.loads(text)
        errors = blocks.validate_spec(spec)
        if not errors:
            return save_spec(spec, proposals_dir)
        log.warning("attempt %d: invalid spec (%s)", attempt + 1, "; ".join(errors))
        messages += [
            {"role": "assistant", "content": text},
            {"role": "user", "content":
                "That spec failed validation:\n- " + "\n- ".join(errors)
                + "\nFix these problems and respond with the corrected spec JSON only."},
        ]
    log.error("proposer failed to produce a valid spec after retry")
    return None


def save_spec(spec: dict, proposals_dir: Path = PROPOSALS_DIR) -> dict:
    """Persist a validated spec; search reads it via `search --spec <path>`."""
    proposals_dir = Path(proposals_dir)
    proposals_dir.mkdir(parents=True, exist_ok=True)
    safe_name = "".join(c if c.isalnum() or c in "-_" else "-" for c in spec["name"])
    path = proposals_dir / f"{safe_name}.json"
    path.write_text(json.dumps(spec, indent=2), encoding="utf-8")
    log.info("saved proposal to %s", path)
    spec["_path"] = str(path)
    return spec
