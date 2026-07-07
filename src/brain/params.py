"""
Brain parameter surface. Every tunable is defined in src/config.py (the single
env-configurable source); this module groups them into brain-facing structures so
the logic modules import named params from one place and contain no literals.
"""

from __future__ import annotations

from src import config

_b = config.brain

# ── Conviction weights (status → anchor weight) ───────────────────────────────
CONVICTION_WEIGHTS: dict[str, float] = {
    "preferred": _b.conviction_preferred,
    "approved":  _b.conviction_approved,
    "hold":      _b.conviction_hold,
    # do_not_add / written_off / unknown → 0 (structural: no BUY weight)
}

# ── Pillar weights (composite S = Σ w·pillar) ─────────────────────────────────
PILLAR_WEIGHTS: dict[str, float] = {
    "quality":   _b.weight_quality,
    "valuation": _b.weight_valuation,
    "trend":     _b.weight_trend,
    "entry":     _b.weight_entry,
    "revisions": _b.weight_revisions,
}

# ── Decision thresholds ───────────────────────────────────────────────────────
BUY_THRESHOLD   = _b.buy_threshold
WATCH_THRESHOLD = _b.watch_threshold

# ── Sizing ────────────────────────────────────────────────────────────────────
F_GLOBAL          = _b.f_global
SIZE_MIN_PCT      = _b.size_min_pct
SIZE_MAX_PCT      = config.rules.max_position_pct    # single-name cap (reused)
VOL_REF           = _b.vol_ref
VOL_SCALAR_MIN    = _b.vol_scalar_min
VOL_SCALAR_MAX    = _b.vol_scalar_max
MAX_NEW_DEPLOY_PCT = _b.max_new_deploy_pct
MAX_THEME_PCT     = config.rules.max_theme_pct       # theme cap (reused)
THEME_CAP_EXEMPT  = config.rules.theme_cap_exempt    # theme(s) exempt from the cap

# ── Quality pillar ────────────────────────────────────────────────────────────
Q_REV_LOW, Q_REV_HIGH   = _b.q_rev_low, _b.q_rev_high
Q_GM_LOW, Q_GM_HIGH     = _b.q_gm_low, _b.q_gm_high
Q_DE_GOOD, Q_DE_BAD     = _b.q_de_good, _b.q_de_bad
Q_CR_LOW, Q_CR_HIGH     = _b.q_cr_low, _b.q_cr_high
Q_CATASTROPHIC_CAP      = _b.q_catastrophic_cap

# ── Valuation pillar ──────────────────────────────────────────────────────────
V_PEG_LOW, V_PEG_HIGH   = _b.v_peg_low, _b.v_peg_high
V_PE_LOW, V_PE_HIGH     = _b.v_pe_low, _b.v_pe_high
V_NEUTRAL               = _b.v_neutral

# ── Trend pillar ──────────────────────────────────────────────────────────────
T_REGIME_TERM = _b.t_regime_term
T_MOM_SCALE   = _b.t_mom_scale
T_MA50_SCALE  = _b.t_ma50_scale

# ── Entry-timing pillar ───────────────────────────────────────────────────────
E_PULLBACK_PEAK_LOW  = _b.e_pullback_peak_low
E_PULLBACK_PEAK_HIGH = _b.e_pullback_peak_high
E_PULLBACK_MAX       = _b.e_pullback_max
E_MA_PROXIMITY_PCT   = _b.e_ma_proximity_pct
E_RSI_HIGH           = _b.e_rsi_high
E_RSI_OVERBOUGHT     = _b.e_rsi_overbought
E_RSI_OVERSOLD       = _b.e_rsi_oversold

# ── Revisions pillar (soft only — never forces a sell) ────────────────────────
R_NORM = _b.r_norm

# ── Exit stack ────────────────────────────────────────────────────────────────
EXIT_REV_DECLINE     = _b.exit_rev_decline
EXIT_GM_COLLAPSE     = _b.exit_gm_collapse
TRAIL_ATR_MULT       = _b.trail_atr_mult
TRIM_GAIN_PCT        = _b.trim_gain_pct
TRIM_RSI             = _b.trim_rsi
TRIM_MA_EXT_ATR_MULT = _b.trim_ma_ext_atr_mult
TRIM_FRACTION        = _b.trim_fraction
