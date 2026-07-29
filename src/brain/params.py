"""
Brain parameter surface. Every tunable is defined in src/config.py (the single source of
values); this module groups them into brain-facing names so the logic modules import from
one place and contain no literals.

Resolution is LIVE, not frozen at import. Each name is looked up on `config.brain` at access
time via the module `__getattr__` below, so a value changed after import — by the Supabase
override layer at run start (src/settings.py), or by a test monkeypatching a config field —
is seen immediately. Binding these as module constants, as this file used to, meant an
override applied after the first `import` of any brain module was silently ignored.

Names map to `config.brain` fields by lowercasing (F_GLOBAL → config.brain.f_global).
`_ALIASES` covers the handful sourced from another config section or computed.

The `TYPE_CHECKING` block below declares each name's type so pyright still type-checks every
call site — the dynamic lookup is a runtime detail only.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable

from src import config

if TYPE_CHECKING:
    # ── Conviction weights ────────────────────────────────────────────────────
    CONVICTION_WEIGHTS: dict[str, float]        # legacy bucket path (preferred/approved/hold)
    CONVICTION_THEME_WEIGHTS: dict[str, float]  # conviction-primary: theme label → anchor weight C
    # ── Pillar weights (composite S = Σ w·pillar) ─────────────────────────────
    PILLAR_WEIGHTS: dict[str, float]
    # ── Decision thresholds ───────────────────────────────────────────────────
    BUY_THRESHOLD: float
    WATCH_THRESHOLD: float
    # ── Conviction-primary gate (OwnScore = C·H vs. the market composite C·S) ──
    CONVICTION_PRIMARY: bool
    THESIS_NEUTRAL: float
    CP_BUY_THRESHOLD: float
    CP_WATCH_THRESHOLD: float
    # ── Sizing ────────────────────────────────────────────────────────────────
    F_GLOBAL: float
    SIZE_MIN_PCT: float
    SIZE_MAX_PCT: float          # single-name cap (config.rules.max_position_pct)
    VOL_REF: float
    VOL_SCALAR_MIN: float
    VOL_SCALAR_MAX: float
    MAX_NEW_DEPLOY_PCT: float
    MAX_THEME_PCT: float         # theme cap (config.rules.max_theme_pct)
    THEME_CAP_EXEMPT: str        # theme(s) exempt from the cap (config.rules)
    SIZE_TIMING_MIN: float       # Stage-2 timing multiplier bounds
    SIZE_TIMING_MAX: float
    DISCOUNT_FULL_PCT: float     # % below 52wk high = full discount → biggest lot
    MARKET_OVERLAY_MAX: float    # market-cycle lot boost bounds
    MARKET_OVERLAY_FULL_DD_PCT: float
    # ── Contextual reasoning layer ────────────────────────────────────────────
    CONTEXTUAL_ENABLED: bool
    BETA_WINDOW_DAYS: int
    BETA_DROP_WINDOW_DAYS: int
    BETA_MIN_R2: float
    SELLOFF_NDAY: int
    SELLOFF_PANIC_RETURN_PCT: float
    SELLOFF_BREADTH_MIN_PCT: float
    CONTEXTUAL_SIZE_MULT_MIN: float
    CONTEXTUAL_SIZE_MULT_MAX: float
    CONTEXTUAL_NUDGE_CAP: float
    # ── Quality pillar ────────────────────────────────────────────────────────
    Q_REV_LOW: float
    Q_REV_HIGH: float
    Q_GM_LOW: float
    Q_GM_HIGH: float
    Q_DE_GOOD: float
    Q_DE_BAD: float
    Q_CR_LOW: float
    Q_CR_HIGH: float
    Q_CATASTROPHIC_CAP: float
    # ── Valuation pillar ──────────────────────────────────────────────────────
    V_PEG_LOW: float
    V_PEG_HIGH: float
    V_PE_LOW: float
    V_PE_HIGH: float
    V_NEUTRAL: float
    # ── Trend pillar ──────────────────────────────────────────────────────────
    T_REGIME_TERM: float
    T_REGIME_PULLBACK: float
    T_MOM_SCALE: float
    T_MA50_SCALE: float
    # ── Regime classification ─────────────────────────────────────────────────
    REGIME_PULLBACK_MAX_BELOW_MA200_PCT: float
    REGIME_SLOPE_MIN_PCT: float
    # ── Entry-timing pillar ───────────────────────────────────────────────────
    E_PULLBACK_PEAK_LOW: float
    E_PULLBACK_PEAK_HIGH: float
    E_PULLBACK_MAX: float
    E_MA_PROXIMITY_PCT: float
    E_RSI_HIGH: float
    E_RSI_OVERBOUGHT: float
    E_RSI_OVERSOLD: float
    # ── Revisions pillar (soft only — never forces a sell) ────────────────────
    R_NORM: float
    # ── Exit stack ────────────────────────────────────────────────────────────
    EXIT_REV_DECLINE: float
    EXIT_GM_COLLAPSE: float
    TRAIL_ATR_MULT: float
    TRIM_GAIN_PCT: float
    TRIM_RSI: float
    TRIM_MA_EXT_ATR_MULT: float
    TRIM_FRACTION: float


def _conviction_weights() -> dict[str, float]:
    """Legacy bucket path. do_not_add / written_off / unknown → 0 (structural: no BUY weight)."""
    _b = config.brain
    return {
        "preferred": _b.conviction_preferred,
        "approved": _b.conviction_approved,
        "hold": _b.conviction_hold,
    }


def _conviction_theme_weights() -> dict[str, float]:
    """Conviction-primary: theme conviction label → anchor weight C (industry-level)."""
    _b = config.brain
    return {
        "very_high": _b.conviction_very_high,
        "high": _b.conviction_high,
        "medium": _b.conviction_medium,
        "low": _b.conviction_low,
    }


def _pillar_weights() -> dict[str, float]:
    _b = config.brain
    return {
        "quality": _b.weight_quality,
        "valuation": _b.weight_valuation,
        "trend": _b.weight_trend,
        "entry": _b.weight_entry,
        "revisions": _b.weight_revisions,
    }


# Names not sourced from `config.brain.<lowercase>`: another config section, or computed.
_ALIASES: dict[str, Callable[[], Any]] = {
    "CONVICTION_WEIGHTS": _conviction_weights,
    "CONVICTION_THEME_WEIGHTS": _conviction_theme_weights,
    "PILLAR_WEIGHTS": _pillar_weights,
    "SIZE_MAX_PCT": lambda: config.rules.max_position_pct,
    "MAX_THEME_PCT": lambda: config.rules.max_theme_pct,
    "THEME_CAP_EXEMPT": lambda: config.rules.theme_cap_exempt,
}


def __getattr__(name: str) -> Any:
    """Resolve a brain parameter against the live config (PEP 562 module __getattr__)."""
    alias = _ALIASES.get(name)
    if alias is not None:
        return alias()
    if name.isupper():
        try:
            return getattr(config.brain, name.lower())
        except AttributeError:
            pass
    raise AttributeError(f"module 'src.brain.params' has no attribute {name!r}")
