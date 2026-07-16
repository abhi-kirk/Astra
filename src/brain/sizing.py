"""
Sizing — conviction-weighted, volatility-scaled, fractional-Kelly-capped.

    vol_scalar = clip(VOL_REF / atr_pct, MIN, MAX)     (higher-vol names → smaller)
    target_w   = clip(F_GLOBAL · Score_buy · vol_scalar, SIZE_MIN, SIZE_MAX)

Then an iterative constrained allocation across all simultaneous BUYs: greedily fill
in score order under the single-name, per-theme, and total-new-deploy caps. Higher-
conviction names fill first; leftover budget flows to the rest (water-fill). A BUY that
can't fit a minimum position is downgraded to WATCH rather than sized to dust.

target_w is a fraction of the FULL portfolio — the advisor recommendation Abhi executes
by hand. The $1k Autotrader clamps to its own limits downstream and never sees this.
"""

from __future__ import annotations

from src.brain import params
from src.brain.normalize import clamp


def vol_scalar(md: dict) -> float:
    """Volatility scalar — higher-vol names size smaller. Public so the decision-features
    snapshot (src/brain/snapshot.py) can log it without recomputing the formula."""
    atr = md.get("atr_14")
    price = md.get("current_price")
    if not atr or not price or price <= 0:
        return 1.0
    atr_pct = atr / price
    if atr_pct <= 0:
        return 1.0
    return clamp(params.VOL_REF / atr_pct, params.VOL_SCALAR_MIN, params.VOL_SCALAR_MAX)


def target_weight(score_buy: float, md: dict) -> float:
    """Raw per-name target as a fraction of the full portfolio (pre-allocation)."""
    w = params.F_GLOBAL * score_buy * vol_scalar(md)
    if w <= 0:
        return 0.0
    return clamp(w, params.SIZE_MIN_PCT, params.SIZE_MAX_PCT)


def allocate(buy_signals: list[dict], portfolio_summary: dict) -> None:
    """
    Mutate `buy_signals` in place: finalize `suggested_position_pct` under caps, or
    downgrade a signal to WATCH if it cannot fit a minimum position.

    Each signal must carry: ticker, theme (or None), score_buy, suggested_position_pct
    (the raw target_weight). Ordering by score is done here.
    """
    theme_used: dict[str, float] = dict(portfolio_summary.get("theme_allocations", {}))
    total_new = 0.0

    for sig in sorted(buy_signals, key=lambda s: s.get("score_buy", 0.0), reverse=True):
        target = sig.get("suggested_position_pct") or 0.0
        theme = sig.get("theme")
        capped_by_theme = theme is not None and theme != params.THEME_CAP_EXEMPT

        theme_remaining = (
            params.MAX_THEME_PCT - theme_used.get(theme, 0.0)
            if capped_by_theme and theme is not None else float("inf")
        )
        total_remaining = params.MAX_NEW_DEPLOY_PCT - total_new

        allowed = min(target, params.SIZE_MAX_PCT, theme_remaining, total_remaining)

        if allowed < params.SIZE_MIN_PCT:
            # Can't fit a meaningful position this run — defer to watch.
            sig["action"] = "watch"
            sig["suggested_position_pct"] = None
            sig["reasons"] = list(sig.get("reasons", [])) + [
                "Deferred to watch — allocation caps leave no room this run"
            ]
            continue

        sig["suggested_position_pct"] = round(allowed, 4)
        total_new += allowed
        if capped_by_theme and theme is not None:
            theme_used[theme] = theme_used.get(theme, 0.0) + allowed
