"""
Entry — build the conviction-weighted composite and decide BUY / WATCH / HOLD.

    S         = Σ w·pillar / Σ w      (renormalized over *available* pillars)
    Score_buy = C · S                 (C = conviction weight)

Trend contributes only its positive part (max(T,0)) so a downtrend removes the
tailwind and raises the effective bar; revisions may subtract. Decisions are
thresholds on Score_buy — no absolute price/RSI cutoffs.
"""

from __future__ import annotations

from typing import NamedTuple

from src.brain import conviction, factors, params


class Composite(NamedTuple):
    score: float               # S ∈ roughly [-wR, 1]
    pillars: dict[str, float]  # available pillar contribution values
    notes: list[str]


def compute_composite(md: dict) -> Composite:
    raw = {
        "quality":   factors.quality(md),
        "valuation": factors.valuation(md),
        "trend":     factors.trend(md),
        "entry":     factors.entry(md),
        "revisions": factors.revisions(md),
    }

    contrib: dict[str, float] = {}
    notes: list[str] = []
    num = 0.0
    den = 0.0
    for name, f in raw.items():
        notes.append(f.note)
        if f.value is None:
            continue
        # trend only helps buys via its positive part
        c = max(f.value, 0.0) if name == "trend" else f.value
        contrib[name] = c
        w = params.PILLAR_WEIGHTS[name]
        num += w * c
        den += w

    score = (num / den) if den else 0.0
    return Composite(score=score, pillars=contrib, notes=notes)


class Decision(NamedTuple):
    action: str              # "buy" | "watch" | "hold"
    score_buy: float         # C · S
    composite: Composite
    conviction_weight: float


def decide(md: dict, guidance: dict) -> Decision:
    comp = compute_composite(md)
    c = conviction.conviction_weight(guidance)
    score_buy = c * comp.score

    if conviction.can_buy(guidance) and score_buy >= params.BUY_THRESHOLD:
        action = "buy"
    elif conviction.can_buy(guidance) and score_buy >= params.WATCH_THRESHOLD:
        action = "watch"
    else:
        action = "hold"

    return Decision(action=action, score_buy=score_buy, composite=comp, conviction_weight=c)
