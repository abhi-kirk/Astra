"""
Orchestrator — turn one position into a Signal, preserving the screen_position
contract that agent.py / memory / the executor / the dashboard depend on.

Precedence: hard-rule block → exit stack (sell/trim) → entry decision (buy/watch/hold).
Buys carry a RAW target weight; screen_all_positions runs sizing.allocate() afterwards
to finalize sizes under portfolio caps.
"""

from __future__ import annotations

from typing import Any

from src.brain import entry as entry_mod
from src.brain import exit as exit_mod
from src.brain import sizing
from src.brain.conviction import get_ticker_guidance
from src.brain.hard_rules import check_hard_rules

Signal = dict[str, Any]


def _make_signal(**kwargs) -> Signal:
    base: Signal = {
        "ticker": None, "action": "hold",
        "conviction_match": False, "quality_pass": False, "technical_pass": False,
        "hard_rule_block": None, "reasons": [], "risk_flags": [],
        "suggested_position_pct": None,
        "intent": "opportunistic", "original_catalyst": None,
        "theme": None, "score_buy": None, "trim_fraction": None, "close_reason": None,
    }
    base.update(kwargs)
    return base


def screen_position(
    ticker: str,
    position: dict,
    market_data: dict,
    convictions: dict,
    portfolio_summary: dict,
) -> Signal:
    guidance = get_ticker_guidance(ticker, convictions)
    intent = guidance.get("intent", "opportunistic")
    catalyst = guidance.get("original_catalyst")
    theme = guidance.get("theme")

    common = dict(ticker=ticker, intent=intent, original_catalyst=catalyst, theme=theme,
                  conviction_match=guidance["status"] in ("approved", "preferred", "hold"))

    # 0) No market data → hold (cannot score)
    if not market_data or market_data.get("error") or not market_data.get("current_price"):
        return _make_signal(action="hold", reasons=["No market data available"], **common)

    # 1) Hard-rule block
    block = check_hard_rules(ticker, position, market_data, convictions, portfolio_summary)
    if block:
        risk_flags = []
        if intent == "written_off":
            risk_flags.append("Written-off position — consider redeploying capital into higher-conviction names")
        return _make_signal(action="blocked", hard_rule_block=block, reasons=[block],
                            risk_flags=risk_flags, **common)

    # 2) Exit stack (held positions only)
    if position.get("shares"):
        ex = exit_mod.evaluate(position, market_data, guidance)
        if ex.action in ("sell", "trim"):
            return _make_signal(
                action=ex.action, reasons=ex.reasons, risk_flags=ex.risk_flags,
                trim_fraction=ex.trim_fraction, close_reason=ex.close_reason,
                quality_pass=True, technical_pass=True, **common,
            )
        exit_risk_flags = ex.risk_flags  # e.g. written-off nudge; carry into the decision below
    else:
        exit_risk_flags = []

    # 3) Entry decision
    decision = entry_mod.decide(market_data, guidance)
    comp = decision.composite
    reasons = list(comp.notes)
    quality_pass = comp.pillars.get("quality", 0.0) >= entry_mod.params.BUY_THRESHOLD
    technical_pass = comp.pillars.get("entry", 0.0) >= entry_mod.params.BUY_THRESHOLD

    if decision.action == "buy":
        raw_target = sizing.target_weight(decision.score_buy, market_data)
        reasons.insert(0, f"Composite {comp.score:.2f} × conviction {decision.conviction_weight:.2f} "
                          f"= {decision.score_buy:.2f} ≥ buy threshold")
        return _make_signal(
            action="buy", reasons=reasons, risk_flags=exit_risk_flags,
            suggested_position_pct=raw_target, score_buy=round(decision.score_buy, 4),
            quality_pass=quality_pass, technical_pass=technical_pass, **common,
        )

    if decision.action == "watch":
        reasons.insert(0, f"Score {decision.score_buy:.2f} — below buy, above watch")
        return _make_signal(action="watch", reasons=reasons, risk_flags=exit_risk_flags,
                            score_buy=round(decision.score_buy, 4),
                            quality_pass=quality_pass, technical_pass=technical_pass, **common)

    # hold
    hold_reason = "Hold-only: no new buys" if guidance.get("hold_only") else f"Score {decision.score_buy:.2f} — hold"
    return _make_signal(action="hold", reasons=[hold_reason], risk_flags=exit_risk_flags,
                        score_buy=round(decision.score_buy, 4), **common)
