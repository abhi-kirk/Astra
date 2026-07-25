"""
Playbook — strategies as first-class editable data, and the deterministic applier that turns
their bounded proposals into changes on the signal set.

The Playbook (docs/contextual_reasoning.md) mirrors convictions: a list of "plays", each a
declarative record {name, enabled, type, weight, rationale, evaluates, params}, add/remove/
reweight-able at will. `type` governs how a play is evaluated:

  • quant   — a registered code evaluator (deterministic, backtestable). macro_dip's quant arm.
  • judgment— evaluated purely by the LLM against its `evaluates` prose (no code — add a play row).
  • hybrid  — a quant trigger arms it (computes a base proposal + evidence), the LLM confirms/
              vetoes/scales it. If the LLM is unavailable the quant base stands (fail-open floor).

The layer NEVER emits decisions. It emits `Proposal` deltas — size_multiplier / conviction_nudge /
thesis_flag — which `apply_proposals` clamps and applies, re-checking every hard invariant in code
regardless of what a play (or the LLM) asked for. This module is pure/deterministic; the LLM lives
in `src/contextual.py`.
"""

from __future__ import annotations

import logging
from typing import Any

from src.brain import conviction, entry, market_context, params, sizing
from src.brain.hard_rules import compute_portfolio_summary
from src.brain.normalize import clamp

logger = logging.getLogger(__name__)

Proposal = dict[str, Any]
KINDS = ("size_multiplier", "conviction_nudge", "thesis_flag")


def load_plays() -> dict:
    """The live Playbook (single JSONB row). Empty/missing → no plays (never fatal)."""
    from src.data_layer import load_plays as _load
    return _load()


def _proposal(ticker: str, play: str, kind: str, value: float, rationale: str,
              evidence: dict | None, source: str) -> Proposal:
    return {
        "ticker": ticker, "play": play, "kind": kind, "value": round(float(value), 6),
        "rationale": rationale, "evidence": evidence or {}, "source": source,
        "applied": None,   # set by apply_proposals
    }


# ── Quant evaluators (registered by play name) ────────────────────────────────

def _eval_macro_dip(play: dict, signals: list[dict], market_data: dict[str, dict],
                    market_ctx: dict) -> list[Proposal]:
    """macro_dip quant arm: in a genuine market-wide selloff, size UP a healthy name whose drop is
    almost entirely market-beta (low idiosyncratic residual) — buy quality at a beta-driven discount.
    Emits a size_multiplier for buy/watch candidates and a small conviction_nudge for watch names
    (to help a near-threshold conviction cross into a buy on a clean, deep macro dip)."""
    p = play.get("params", {})
    weight = clamp(float(play.get("weight", 1.0)), 0.0, 1.0)
    beta_min = float(p.get("beta_min", 0.8))
    residual_max = float(p.get("residual_max_pct", 5.0))
    min_drop = float(p.get("min_drop_pct", 8.0))
    min_r2 = float(p.get("min_r2", params.BETA_MIN_R2))
    size_mult_max = float(p.get("size_mult_max", params.CONTEXTUAL_SIZE_MULT_MAX))
    nudge_max = float(p.get("nudge_max", params.CONTEXTUAL_NUDGE_CAP))

    selloff = market_ctx.get("selloff", {})
    if not selloff.get("is_panic"):
        return []   # the play only arms in a genuine broad selloff

    spy_returns = market_ctx.get("spy_returns")
    out: list[Proposal] = []
    for sig in signals:
        if sig.get("action") not in ("buy", "watch") or sig.get("source") == "exploration":
            continue
        ticker = sig["ticker"]
        md = market_data.get(ticker, {})
        decomp = market_context.beta_decomposition(md.get("returns_recent"), spy_returns)
        if not decomp:
            continue
        is_macro_dip = (
            decomp["name_move_pct"] <= -min_drop
            and decomp["beta"] >= beta_min
            and decomp["r2"] >= min_r2                    # trust the read only if SPY explains enough
            and abs(decomp["idiosyncratic_residual_pct"]) <= residual_max
        )
        if not is_macro_dip:
            continue
        evidence = {**decomp, "selloff": selloff}
        rationale = (
            f"{-decomp['name_move_pct']:.0f}% drop is {decomp['macro_fraction']:.0%} market-beta "
            f"(β={decomp['beta']:.1f}, residual {decomp['idiosyncratic_residual_pct']:+.1f}%) into a "
            f"broad selloff (SPY {selloff.get('spy_nday_return_pct')}% / "
            f"{selloff.get('breadth_pct_down')}% of names down) — beta-driven discount, thesis intact."
        )
        mult = 1.0 + (size_mult_max - 1.0) * weight
        out.append(_proposal(ticker, "macro_dip", "size_multiplier", mult, rationale, evidence, "quant"))
        # A small upward nudge only for a WATCH name (candidate to promote), scaled by conviction.
        if sig.get("action") == "watch":
            out.append(_proposal(ticker, "macro_dip", "conviction_nudge", nudge_max * weight,
                                 rationale, evidence, "quant"))
    return out


_QUANT_EVALUATORS = {
    "macro_dip": _eval_macro_dip,
}


# ── Evaluation (two producers → one proposals list) ───────────────────────────

def evaluate(signals: list[dict], market_data: dict[str, dict], market_ctx: dict,
             convictions: dict, use_ai: bool = False, plays: dict | None = None) -> list[Proposal]:
    """Walk the ENABLED Playbook and collect proposals. Quant + hybrid plays run their
    deterministic evaluator; if `use_ai`, judgment + hybrid plays are reasoned by the LLM
    (`contextual.reason`), which sees the quant evidence + the hybrids' base proposals and returns
    the final set for those plays. If the LLM fails, hybrids fall back to their quant base."""
    plays = plays if plays is not None else load_plays()
    enabled = [p for p in plays.get("plays", []) if p.get("enabled")]
    if not enabled:
        return []

    proposals: list[Proposal] = []
    hybrid_bases: list[Proposal] = []
    for play in enabled:
        ptype = play.get("type")
        if ptype in ("quant", "hybrid"):
            evaluator = _QUANT_EVALUATORS.get(play.get("name", ""))
            base = evaluator(play, signals, market_data, market_ctx) if evaluator else []
            (proposals if ptype == "quant" else hybrid_bases).extend(base)

    needs_llm = use_ai and any(p.get("type") in ("judgment", "hybrid") for p in enabled)
    if needs_llm:
        try:
            from src import contextual
            proposals.extend(
                contextual.reason(signals, market_data, market_ctx, convictions, enabled, hybrid_bases)
            )
        except Exception:
            logger.error("Contextual LLM layer failed — hybrid plays fall back to their quant base",
                         exc_info=True)
            proposals.extend(hybrid_bases)
    else:
        proposals.extend(hybrid_bases)
    return proposals


# ── Application (deterministic, invariant-guarded) ────────────────────────────

def apply_proposals(signals: list[dict], proposals: list[Proposal], portfolio: dict[str, dict],
                    market_data: dict[str, dict], convictions: dict,
                    market_scalar: float = 1.0) -> None:
    """Mutate `signals` in place from the bounded proposals, then re-run sizing under all caps.

    Hard invariants re-checked in code regardless of any proposal: excluded names (TSLA) are never
    touched; a conviction_nudge can only promote a name that still passes `can_buy` and has no
    hard-rule block; the H catastrophic veto is inherent (a nudge to C can't lift a near-zero C·H
    over the bar); size_multiplier is clamped to [MIN,MAX]; nudge to ±NUDGE_CAP; and `sizing.allocate`
    re-imposes the single-name / theme / deploy-budget caps. Each proposal's `applied` is stamped.
    """
    by_ticker = {s["ticker"]: s for s in signals}

    # Aggregate valid proposals per ticker/kind (multiplicative for size, additive for nudge),
    # rejecting anything that fails a hard invariant up front (applied=False).
    size_mult: dict[str, float] = {}
    nudge: dict[str, float] = {}
    flags: dict[str, list[Proposal]] = {}
    for pr in proposals:
        ticker = str(pr.get("ticker") or "")
        kind = str(pr.get("kind") or "")
        sig = by_ticker.get(ticker)
        if not ticker or sig is None or kind not in KINDS or conviction.is_excluded(ticker, convictions):
            pr["applied"] = False
            continue
        if kind == "size_multiplier":
            size_mult[ticker] = size_mult.get(ticker, 1.0) * float(pr["value"])
            pr["applied"] = True
        elif kind == "conviction_nudge":
            # A nudge may only act on a buyable, un-blocked name (never manufacture a buy from a
            # do_not_add / hold_only / hard-blocked / non-conviction name).
            guidance = conviction.get_ticker_guidance(ticker, convictions)
            if sig.get("hard_rule_block") or not conviction.can_buy(guidance):
                pr["applied"] = False
                continue
            nudge[ticker] = nudge.get(ticker, 0.0) + float(pr["value"])
            pr["applied"] = True
        elif kind == "thesis_flag":
            flags.setdefault(ticker, []).append(pr)
            pr["applied"] = True

    if not (size_mult or nudge or flags):
        return

    # 1) thesis_flag — TIGHTEN only: add a risk flag; a strong flag (value ≥ 1) downgrades a buy to
    #    watch (never loosens, never promotes).
    for ticker, prs in flags.items():
        sig = by_ticker[ticker]
        for pr in prs:
            sig.setdefault("risk_flags", []).append(f"[{pr['play']}] {pr.get('rationale', 'thesis risk flag')}")
        if any(float(p.get("value") or 0.0) >= 1.0 for p in prs) and sig.get("action") == "buy":
            sig["action"] = "watch"
            sig["suggested_position_pct"] = None

    # 2) conviction_nudge — re-gate the affected names with a clamped ΔC, potentially flipping
    #    watch↔buy. Reuses the exact gate math (entry.gate_score + the CP_* thresholds).
    for ticker, delta in nudge.items():
        sig = by_ticker[ticker]
        md = market_data.get(ticker, {})
        guidance = conviction.get_ticker_guidance(ticker, convictions)
        c = conviction.conviction_weight(guidance)
        comp = entry.compute_composite(md)
        clamped = clamp(delta, -params.CONTEXTUAL_NUDGE_CAP, params.CONTEXTUAL_NUDGE_CAP)
        new_c = clamp(c + clamped, 0.0, 1.0)
        new_score = entry.gate_score(md, comp, new_c)
        prev = sig.get("action")
        if new_score >= params.CP_BUY_THRESHOLD:
            sig["action"] = "buy"
        elif new_score >= params.CP_WATCH_THRESHOLD:
            sig["action"] = "watch" if prev in ("buy", "watch") else prev
        # else: leave as-is (a nudge never demotes a buy below watch or touches a hold)
        if sig["action"] in ("buy", "watch"):
            sig["score_buy"] = new_score
        if sig["action"] != prev:
            sig.setdefault("reasons", []).append(
                f"Conviction nudged {clamped:+.2f} → {prev}→{sig['action']} (contextual)"
            )

    # 3) size_multiplier + full re-allocation. Rebuild the sized buy set (position-screen buys with
    #    a score), fold each name's effective scalar into its RAW target (macro play scalar if the
    #    play spoke for it, else the uniform market overlay — no double count), then allocate once.
    buys = [
        s for s in signals
        if s.get("action") == "buy" and s.get("score_buy") is not None
        and s.get("source") != "exploration"
    ]
    for s in buys:
        md = market_data.get(s["ticker"], {})
        raw = sizing.target_weight(s["score_buy"], md)
        scalar = size_mult.get(s["ticker"], market_scalar)
        scalar = clamp(scalar, params.CONTEXTUAL_SIZE_MULT_MIN, params.CONTEXTUAL_SIZE_MULT_MAX)
        s["suggested_position_pct"] = raw * scalar
    # Any name demoted out of `buy` by a flag/nudge drops its sizing.
    for s in signals:
        if s.get("action") != "buy" and s.get("source") != "exploration":
            if s.get("suggested_position_pct") is not None and s not in buys:
                s["suggested_position_pct"] = None

    portfolio_summary = compute_portfolio_summary(portfolio, market_data, convictions)
    sizing.allocate(buys, portfolio_summary, market_scalar=1.0)  # per-name scaling already folded in
