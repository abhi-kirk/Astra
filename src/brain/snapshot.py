"""
Decision-features snapshot — the ML training substrate.

The brain (factors → entry → sizing → score) computes a rich numeric picture per ticker
every run, then `src/brain/score.py` collapses it to a final action + one score + free-text
reasons before anything is persisted. This module rebuilds that numeric picture *for logging
only*: it recomputes the pillar floats, composite S, conviction weight C, regime, and sizing
intermediates from the same `market_data`/`guidance` the decision used. It never touches the
decision path — pure functions in, one flat dict out — so trading behavior is unchanged and
the log captures the full "why" behind every BUY/SELL/HOLD/WATCH/BLOCKED.

One `build_snapshot` row is written per screened ticker per run (no dedup, incl. hold/blocked)
into the `decision_features` table via `memory.log_decision_features`.
"""

from __future__ import annotations

from typing import Any

from src.brain import conviction, entry, factors, regime, sizing


def _round(x: float | None, n: int = 6) -> float | None:
    return round(x, n) if isinstance(x, (int, float)) else None


def build_snapshot(
    ticker: str,
    md: dict,
    guidance: dict,
    signal: dict,
    held: bool | None = None,
) -> dict[str, Any]:
    """Build one decision_features row from the inputs that drove `signal`.

    `md` is the ticker's market_data; `guidance` is get_ticker_guidance(); `signal` is the
    brain's final Signal dict. Recomputes numeric internals — safe/pure. When market data is
    missing (no price), the scored fields are None but the row is still recorded.
    """
    row: dict[str, Any] = {
        "ticker": ticker,
        "action": signal.get("action"),
        "held": held,
        "source": signal.get("source", "position_screen"),
        "suggested_position_pct": _round(signal.get("suggested_position_pct")),
        "hard_rule_block": signal.get("hard_rule_block"),
        "close_reason": signal.get("close_reason"),
        "trim_fraction": _round(signal.get("trim_fraction")),
        "score_buy": None,
        "composite": None,
        "conviction_weight": None,
        "regime": None,
        "target_weight_raw": None,
        "vol_scalar": None,
        "features": {k: v for k, v in md.items() if k != "fetched_at"} if md else {},
        "scores": None,
    }

    # No price → cannot score; record the bare row (still a useful negative example).
    if not md or md.get("current_price") is None:
        return row

    # Per-pillar raw Factor(value, note) — recomputed directly so signed trend/revisions
    # survive (compute_composite clamps trend's negative part out of the weighted average).
    pillars = {
        "quality": factors.quality(md),
        "valuation": factors.valuation(md),
        "trend": factors.trend(md),
        "entry": factors.entry(md),
        "revisions": factors.revisions(md),
    }
    comp = entry.compute_composite(md)
    c = conviction.conviction_weight(guidance)
    score_buy = c * comp.score

    row["composite"] = _round(comp.score)
    row["conviction_weight"] = _round(c)
    row["score_buy"] = _round(score_buy)
    row["regime"] = regime.classify(md)
    row["vol_scalar"] = _round(sizing.vol_scalar(md))
    row["target_weight_raw"] = _round(sizing.target_weight(score_buy, md)) if score_buy > 0 else 0.0
    row["scores"] = {
        name: {"value": _round(f.value), "note": f.note} for name, f in pillars.items()
    }
    return row
