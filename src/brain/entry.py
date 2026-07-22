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
from src.brain.normalize import clamp, smooth


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


def _discount(md: dict) -> float | None:
    """How big a *deal* the price is right now — % below the 52-week high, ramped to [0,1]:
    0 at the highs, 1 once ≥ DISCOUNT_FULL_PCT below. This is the aggressive-dip / cheap-for-itself
    signal (price-based, so it works for fast growers whose P/E is meaningless)."""
    dd = md.get("pct_below_52w_high")
    if dd is None:
        return None
    return smooth(dd, 0.0, params.DISCOUNT_FULL_PCT)


def compute_timing(md: dict) -> float:
    """M ∈ [0,1] — the deal/timing signal for Stage-2 sizing (conviction-primary).

    AGGRESSIVE-DIP policy: M rewards a *discount* so a healthy conviction sizes UP as it falls and
    DOWN near its highs (buy weakness, not strength). Mean of the available deal signals — the
    price discount (below 52wk high), valuation (cheap-for-itself; neutral 0.5 for growers with no
    meaningful multiple), and entry-timing (pullback to support). Trend and analyst revisions are
    deliberately excluded: they're the procyclical/opinion signals that would fight dip-buying.
    Falling-knife risk is handled elsewhere — the H gate blocks broken businesses and the hard
    averaging-down cap bounds repeat adds. No data → neutral. It never gates, only sizes.
    """
    subs: list[float | None] = [
        _discount(md),
        factors.valuation(md).value,
        factors.entry(md).value,
    ]
    present = [v for v in subs if v is not None]
    if not present:
        return params.THESIS_NEUTRAL
    return clamp(sum(present) / len(present), 0.0, 1.0)


class Decision(NamedTuple):
    action: str              # "buy" | "watch" | "hold"
    score_buy: float         # C · S
    composite: Composite
    conviction_weight: float


def gate_score(md: dict, comp: Composite, c: float) -> float:
    """The score the buy/watch/hold gate thresholds on.

    Conviction-primary (docs/conviction_primary.md): OwnScore = C·H — conviction × thesis-health
    only, so the market never votes on *whether* to own. H is the quality/fundamentals pillar
    (with its catastrophic cap = the thesis-break veto); a name with no fundamentals (ETFs/ADRs)
    falls back to a neutral H so conviction carries the gate. Legacy: C·S (the market composite).
    """
    if params.CONVICTION_PRIMARY:
        h = factors.quality(md).value
        if h is None:
            h = params.THESIS_NEUTRAL
        return c * h
    return c * comp.score


def decide(md: dict, guidance: dict) -> Decision:
    comp = compute_composite(md)
    c = conviction.conviction_weight(guidance)
    score_buy = gate_score(md, comp, c)

    # Conviction-primary uses a higher (selective) bar on C·H than the legacy C·S bar.
    buy_thr = params.CP_BUY_THRESHOLD if params.CONVICTION_PRIMARY else params.BUY_THRESHOLD
    watch_thr = params.CP_WATCH_THRESHOLD if params.CONVICTION_PRIMARY else params.WATCH_THRESHOLD

    if conviction.can_buy(guidance) and score_buy >= buy_thr:
        action = "buy"
    elif conviction.can_buy(guidance) and score_buy >= watch_thr:
        action = "watch"
    else:
        action = "hold"

    return Decision(action=action, score_buy=score_buy, composite=comp, conviction_weight=c)
