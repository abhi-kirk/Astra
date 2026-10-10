"""
Contextual reasoning layer — the LLM as a bounded decision *input*, not a narrator.

The ≤3-pass judgment layer (docs/contextual_reasoning.md §4) reasons over the enabled Playbook
(judgment + hybrid plays), the deterministic quant evidence (beta decomposition + selloff state,
already computed), and the current signals — then emits **clamped, logged, structured proposals**
via a FORCED tool call (`emit_proposals`). It never emits decisions: the returned proposals are
deltas to inputs the brain already understands (size_multiplier / conviction_nudge / thesis_flag),
which `brain.playbook.apply_proposals` clamps and applies with hard-invariant re-checks.

Determinism boundary = the emitted object. Everything here can be as agentic as it likes; the
pipeline only ever consumes the validated, bounded proposals. Any failure → `[]` (the caller falls
back to the deterministic quant proposals — the fail-open floor).

Mirrors `src/exploration.py`'s LLM+MCP+parse structure and reuses the shared client-side tool loop.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import chevron

from src import config, mcp, mcp_loop
from src.brain import market_context, params
from src.brain.conviction import is_excluded
from src.timeout import run_with_timeout

logger = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).parent.parent / "prompts"

_KINDS = ("size_multiplier", "conviction_nudge", "thesis_flag")

# The forced-emit tool: its input_schema IS the output contract (schema-valid by construction).
_EMIT_TOOL: dict[str, Any] = {
    "name": "emit_proposals",
    "description": (
        "Emit the final list of bounded, play-tagged proposals. Each proposal is a delta to an "
        "input the deterministic brain already understands — never a decision. Emit an empty list "
        "if no play fires (that is a valid, common outcome — do not invent proposals)."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "proposals": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "ticker": {"type": "string"},
                        "play": {"type": "string", "description": "the enabled play this proposal is tagged to"},
                        "kind": {"type": "string", "enum": list(_KINDS)},
                        "value": {
                            "type": "number",
                            "description": (
                                "size_multiplier: lot scale (e.g. 1.4 = +40%); conviction_nudge: signed ΔC "
                                "(e.g. +0.05); thesis_flag: severity 0..1 (≥1 downgrades a buy to watch)."
                            ),
                        },
                        "rationale": {"type": "string", "description": "one-sentence cited reasoning"},
                    },
                    "required": ["ticker", "play", "kind", "value", "rationale"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["proposals"],
        "additionalProperties": False,
    },
}


def _candidate_evidence(signals: list[dict], market_data: dict[str, dict], market_ctx: dict,
                        hybrid_bases: list[dict]) -> list[dict]:
    """Per-candidate quant evidence the model reasons over — the beta decomposition + whether a
    hybrid play's quant arm already armed (a base proposal exists for it)."""
    spy_returns = market_ctx.get("spy_returns")
    base_by_ticker: dict[str, list[dict]] = {}
    for b in hybrid_bases:
        base_by_ticker.setdefault(b["ticker"], []).append(b)

    rows: list[dict] = []
    for sig in signals:
        if sig.get("action") not in ("buy", "watch"):
            continue
        ticker = sig["ticker"]
        md = market_data.get(ticker, {})
        decomp = market_context.beta_decomposition(md.get("returns_recent"), spy_returns)
        rows.append({
            "ticker": ticker,
            "action": sig.get("action"),
            "theme": sig.get("theme"),
            "pct_below_52w_high": md.get("pct_below_52w_high"),
            "beta_decomposition": decomp,
            "armed_plays": [b["play"] for b in base_by_ticker.get(ticker, [])],
        })
    return rows


def _build_prompt(signals: list[dict], market_data: dict[str, dict], market_ctx: dict,
                  plays: list[dict], hybrid_bases: list[dict]) -> str:
    playbook = [
        {"name": p.get("name"), "type": p.get("type"), "weight": p.get("weight"),
         "rationale": p.get("rationale", ""), "evaluates": p.get("evaluates", "")}
        for p in plays if p.get("type") in ("judgment", "hybrid")
    ]
    ctx = mcp.search_context(max_searches=config.contextual.max_searches, servers=mcp.exploration_specs())
    template = (PROMPTS_DIR / "contextual_reasoning.mustache").read_text()
    return chevron.render(template, {
        "playbook_json": json.dumps(playbook, indent=2),
        "selloff_json": json.dumps(market_ctx.get("selloff", {}), indent=2),
        "candidates_json": json.dumps(
            _candidate_evidence(signals, market_data, market_ctx, hybrid_bases), indent=2),
        "size_mult_min": params.CONTEXTUAL_SIZE_MULT_MIN,
        "size_mult_max": params.CONTEXTUAL_SIZE_MULT_MAX,
        "nudge_cap": params.CONTEXTUAL_NUDGE_CAP,
        **ctx,
    })


def _validate(payload: dict, signals: list[dict], plays: list[dict], convictions: dict) -> list[dict]:
    """Turn the LLM's emitted object into clean proposal dicts, rejecting anything malformed —
    unknown/excluded ticker, unknown play, bad kind, non-numeric value. apply_proposals then does
    the numeric clamping + hard-invariant enforcement (this is only the shape gate)."""
    valid_tickers = {s["ticker"] for s in signals}
    valid_plays = {p.get("name") for p in plays if p.get("enabled")}
    out: list[dict] = []
    for pr in payload.get("proposals", []) or []:
        if not isinstance(pr, dict):
            continue
        ticker, play, kind = pr.get("ticker"), pr.get("play"), pr.get("kind")
        value = pr.get("value")
        if not isinstance(ticker, str) or ticker not in valid_tickers \
                or play not in valid_plays or kind not in _KINDS:
            logger.warning("Dropping malformed contextual proposal: %s", pr)
            continue
        if is_excluded(ticker, convictions) is not None:
            logger.warning("Dropping contextual proposal on excluded ticker %s", ticker)
            continue
        if not isinstance(value, (int, float)):
            continue
        out.append({
            "ticker": ticker, "play": play, "kind": kind, "value": round(float(value), 6),
            "rationale": str(pr.get("rationale", ""))[:500], "evidence": {}, "source": "llm",
            "applied": None,
        })
    return out


def reason(signals: list[dict], market_data: dict[str, dict], market_ctx: dict, convictions: dict,
           enabled_plays: list[dict], hybrid_bases: list[dict]) -> list[dict]:
    """Run the ≤3-pass structured reasoning and return validated LLM proposals (source='llm').

    Only judgment/hybrid plays are reasoned here (quant plays are handled deterministically by the
    caller). Returns [] on any failure — the caller then falls back to `hybrid_bases`."""
    if not config.services.grok_api_key:
        logger.info("Contextual layer: no GROK_API_KEY — skipping LLM reasoning")
        return []

    prompt = _build_prompt(signals, market_data, market_ctx, enabled_plays, hybrid_bases)
    specs = mcp.exploration_specs()

    payload, _usage, _tool_log = run_with_timeout(
        lambda: mcp_loop.run_structured_sync(
            prompt, specs, _EMIT_TOOL,
            model=config.contextual.model,
            max_tokens=config.contextual.max_tokens,
            effort=config.contextual.effort,
            tool_timeout=config.tool_loop.advisor_tool_timeout,
            max_rounds=config.contextual.max_rounds,
            failure_limit=config.tool_loop.mcp_tool_failure_limit,
        ),
        config.timeouts.contextual,
        label="Contextual reasoning Grok call",
    )
    proposals = _validate(payload or {}, signals, enabled_plays, convictions)
    logger.info("Contextual layer: %d validated LLM proposal(s)", len(proposals))
    return proposals
