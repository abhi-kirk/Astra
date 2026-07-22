"""
Conviction — the anchor of the brain. Conviction is both a GATE (a name must be in
`convictions` to be actionable) and a WEIGHT multiplier `C` on the composite score
and on sizing. This is what makes ASTRA a systematization of Abhi's convictions rather
than a generic quant screen.

`get_ticker_guidance` / `is_excluded` are the canonical implementations (strategy.py
re-exports them for backward compatibility with agent_guardrails and the executor).
"""

from __future__ import annotations

from src.brain import params


def is_excluded(ticker: str, convictions: dict) -> str | None:
    """Returns exclusion reason if ticker is hard-excluded (e.g. TSLA), else None."""
    for excl in convictions.get("exclusions", []):
        if excl["ticker"] == ticker:
            return excl["reason"]
    return None


def get_ticker_guidance(ticker: str, convictions: dict) -> dict:
    """
    Most-specific guidance for a ticker — individual holding overrides theme.
    Keys: status, notes, theme, do_not_add, hold_only, intent, original_catalyst.
    """
    meta = convictions.get("ticker_metadata", {}).get(ticker, {})
    intent = meta.get("intent", "opportunistic")  # default: apply profit-take logic
    original_catalyst = meta.get("original_catalyst")

    def _with_meta(base: dict) -> dict:
        return {**base, "intent": intent, "original_catalyst": original_catalyst}

    individual = convictions.get("individual_holdings", {}).get(ticker)
    if individual:
        return _with_meta({
            "status": individual.get("status", "hold"),
            "notes": individual.get("thesis", ""),
            "action_note": individual.get("action", ""),
            "theme": None,
            "theme_conviction": None,
            "do_not_add": individual.get("status") == "do_not_add",
            "hold_only": individual.get("status") == "hold",
        })

    for theme_name, theme in convictions.get("themes", {}).items():
        for bucket, status, hold_only, do_not_add in (
            ("preferred",  "preferred",  False, False),
            ("approved",   "approved",   False, False),
            ("hold_only",  "hold_only",  True,  True),
            ("do_not_add", "do_not_add", False, True),
        ):
            if ticker in theme.get(bucket, []):
                return _with_meta({
                    "status": status,
                    "notes": theme.get("notes", {}).get(ticker, ""),
                    "action_note": "",
                    "theme": theme_name,
                    "theme_conviction": theme.get("conviction"),  # industry-level label → C under conviction-primary
                    "do_not_add": do_not_add,
                    "hold_only": hold_only,
                })

    return _with_meta({"status": "unknown", "notes": "", "action_note": "", "theme": None,
                       "theme_conviction": None, "do_not_add": False, "hold_only": False})


def conviction_weight(guidance: dict) -> float:
    """
    Conviction weight C ∈ [0,1] — the anchor in the buy gate and in sizing.

    Conviction-primary (docs/conviction_primary.md): C comes from the *theme* conviction label
    (very_high/high/medium/low — industry-level), NOT the preferred/approved bucket; H does the
    per-name differentiation the buckets used to. Legacy: preferred > approved > hold buckets.
    Either way, do_not_add / unknown → 0 and hold(-only) → the hold tier (gated from BUYs anyway).
    """
    status = guidance.get("status", "unknown")
    if status in ("do_not_add", "written_off", "unknown"):
        return 0.0
    if status in ("hold", "hold_only"):
        return params.CONVICTION_WEIGHTS["hold"]

    if params.CONVICTION_PRIMARY:
        label = guidance.get("theme_conviction") or "medium"
        # Missing/odd label → treat as mid-tier rather than silently zeroing a real conviction.
        return params.CONVICTION_THEME_WEIGHTS.get(label, params.CONVICTION_THEME_WEIGHTS["medium"])

    if status == "preferred":
        return params.CONVICTION_WEIGHTS["preferred"]
    if status == "approved":
        return params.CONVICTION_WEIGHTS["approved"]
    return 0.0


def can_buy(guidance: dict) -> bool:
    """A name is buyable only if it carries positive conviction and is not hold-only /
    do-not-add. (hold-only conviction still scores >0 but is gated out of BUYs.)"""
    return (
        conviction_weight(guidance) > 0
        and not guidance.get("hold_only")
        and not guidance.get("do_not_add")
    )


def buyable_tickers(convictions: dict) -> list[str]:
    """Every conviction name eligible for a fresh BUY — the daily screening universe beyond
    currently-held positions. Walks the preferred/approved theme buckets + individual holdings,
    then keeps only names that pass `can_buy` and are not hard-excluded (e.g. TSLA). This is
    what lets the brain surface a new entry on a name Abhi likes but doesn't yet hold."""
    candidates: set[str] = set()
    for theme in convictions.get("themes", {}).values():
        candidates.update(theme.get("preferred", []))
        candidates.update(theme.get("approved", []))
    candidates.update(convictions.get("individual_holdings", {}).keys())

    return sorted(
        t for t in candidates
        if not is_excluded(t, convictions) and can_buy(get_ticker_guidance(t, convictions))
    )
