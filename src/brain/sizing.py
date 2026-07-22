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
from src.brain.normalize import clamp, smooth


def market_cycle_multiplier(market_drawdown_pct: float | None) -> float:
    """Boost every buy's lot when the broad market (SPY) is in a drawdown — buy into the cycle dip.
    `market_drawdown_pct` = how far SPY sits below its 52-week high (%). 1.0 at the highs, ramping
    to MARKET_OVERLAY_MAX once the market is ≥ MARKET_OVERLAY_FULL_DD_PCT down. No-op off-flag."""
    if not params.CONVICTION_PRIMARY or not market_drawdown_pct or market_drawdown_pct <= 0:
        return 1.0
    frac = smooth(market_drawdown_pct, 0.0, params.MARKET_OVERLAY_FULL_DD_PCT)
    return 1.0 + (params.MARKET_OVERLAY_MAX - 1.0) * frac


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


def timing_multiplier(md: dict) -> float:
    """Stage-2 pacing factor (conviction-primary only): map the deal signal M∈[0,1] to a lot
    multiplier, pivoting on ×1.0 at neutral (M=0.5). A discount (M>0.5) ramps up toward
    SIZE_TIMING_MAX (buy the dip bigger); a name near its highs (M<0.5) ramps down toward
    SIZE_TIMING_MIN. Piecewise so neutral/no-data always means "no change." Public so the
    decision-features snapshot can log it. Returns 1.0 (no effect) when conviction-primary is off."""
    if not params.CONVICTION_PRIMARY:
        return 1.0
    from src.brain import entry  # local import: entry never imports sizing (avoids any cycle)
    m = entry.compute_timing(md)
    if m >= 0.5:
        return 1.0 + (params.SIZE_TIMING_MAX - 1.0) * (m - 0.5) / 0.5
    return params.SIZE_TIMING_MIN + (1.0 - params.SIZE_TIMING_MIN) * m / 0.5


def target_weight(score_buy: float, md: dict) -> float:
    """Raw per-name target as a fraction of the full portfolio (pre-allocation).

    Under conviction-primary, score_buy is OwnScore = C·H (the *desired* stake); the timing
    multiplier then paces how much of it to deploy this run — it can shrink or grow the lot but
    never zeroes a candidate (the buy/watch/hold gate already fired upstream)."""
    w = params.F_GLOBAL * score_buy * vol_scalar(md) * timing_multiplier(md)
    if w <= 0:
        return 0.0
    return clamp(w, params.SIZE_MIN_PCT, params.SIZE_MAX_PCT)


def _to_watch(sig: dict) -> None:
    sig["action"] = "watch"
    sig["suggested_position_pct"] = None
    sig["reasons"] = list(sig.get("reasons", [])) + [
        "Deferred to watch — allocation caps leave no room this run"
    ]


def allocate(buy_signals: list[dict], portfolio_summary: dict, market_scalar: float = 1.0) -> None:
    """
    Mutate `buy_signals` in place: finalize `suggested_position_pct` under caps, or
    downgrade a signal to WATCH if it cannot fit a minimum position.

    Each signal must carry: ticker, theme (or None), score_buy, suggested_position_pct
    (the raw target_weight). Ordering by score is done here.

    Under conviction-primary a *reservation* pass runs first: every gated buy is guaranteed a
    minimum starter lot (in score order, caps permitting) BEFORE any name fills toward its full
    target — so the highest-OwnScore names (usually the biggest, most-profitable megacaps) can't
    consume the whole per-run deploy budget and crowd a genuine conviction out to watch.
    """
    ordered = sorted(buy_signals, key=lambda s: s.get("score_buy", 0.0), reverse=True)
    # market_scalar boosts every target when the broad market is in a drawdown (cycle-dip buying).
    raw_target = {id(s): (s.get("suggested_position_pct") or 0.0) * market_scalar for s in ordered}
    theme_used: dict[str, float] = dict(portfolio_summary.get("theme_allocations", {}))
    committed: dict[int, float] = {}
    total_new = 0.0

    def _theme_remaining(theme: str | None) -> float:
        capped = theme is not None and theme != params.THEME_CAP_EXEMPT
        return (params.MAX_THEME_PCT - theme_used.get(theme, 0.0)) if capped and theme is not None else float("inf")

    def _commit(sig: dict, amount: float) -> None:
        """Set this signal's lot to an ABSOLUTE `amount`, tracking the delta against any prior
        commit so total/theme budgets stay correct across the reserve + top-up passes."""
        nonlocal total_new
        delta = amount - committed.get(id(sig), 0.0)
        sig["suggested_position_pct"] = round(amount, 4)
        committed[id(sig)] = amount
        total_new += delta
        theme = sig.get("theme")
        if theme is not None and theme != params.THEME_CAP_EXEMPT:
            theme_used[theme] = theme_used.get(theme, 0.0) + delta

    if params.CONVICTION_PRIMARY:
        # Phase 1 — reserve a floor lot for as many buys as fit (score order); rest → watch.
        reserved: list[dict] = []
        for sig in ordered:
            floor = params.SIZE_MIN_PCT
            if floor <= _theme_remaining(sig.get("theme")) and floor <= params.MAX_NEW_DEPLOY_PCT - total_new:
                _commit(sig, floor)
                reserved.append(sig)
            else:
                _to_watch(sig)
        # Phase 2 — top up the reserved buys toward their raw target, strongest first.
        for sig in reserved:
            current = committed[id(sig)]
            headroom = min(params.SIZE_MAX_PCT - current, _theme_remaining(sig.get("theme")),
                           params.MAX_NEW_DEPLOY_PCT - total_new)
            add = max(0.0, min(raw_target[id(sig)] - current, headroom))
            if add > 0:
                _commit(sig, current + add)
        return

    for sig in ordered:
        theme_remaining = _theme_remaining(sig.get("theme"))
        total_remaining = params.MAX_NEW_DEPLOY_PCT - total_new
        allowed = min(raw_target[id(sig)], params.SIZE_MAX_PCT, theme_remaining, total_remaining)

        if allowed < params.SIZE_MIN_PCT:
            _to_watch(sig)
            continue
        _commit(sig, allowed)
