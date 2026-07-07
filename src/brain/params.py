"""
Brain parameter surface. Every tunable is defined in src/config.py (the single
env-configurable source); this module groups them into brain-facing structures so
the logic modules import named params from one place and contain no literals.
"""

from __future__ import annotations

from src import config

# ── Conviction weights (status → anchor weight) ───────────────────────────────
CONVICTION_WEIGHTS: dict[str, float] = {
    "preferred": config.BRAIN_CONVICTION_PREFERRED,
    "approved":  config.BRAIN_CONVICTION_APPROVED,
    "hold":      config.BRAIN_CONVICTION_HOLD,
    # do_not_add / written_off / unknown → 0 (structural: no BUY weight)
}

# ── Pillar weights (composite S = Σ w·pillar) ─────────────────────────────────
PILLAR_WEIGHTS: dict[str, float] = {
    "quality":   config.BRAIN_WEIGHT_QUALITY,
    "valuation": config.BRAIN_WEIGHT_VALUATION,
    "trend":     config.BRAIN_WEIGHT_TREND,
    "entry":     config.BRAIN_WEIGHT_ENTRY,
    "revisions": config.BRAIN_WEIGHT_REVISIONS,
}

# ── Decision thresholds ───────────────────────────────────────────────────────
BUY_THRESHOLD   = config.BRAIN_BUY_THRESHOLD
WATCH_THRESHOLD = config.BRAIN_WATCH_THRESHOLD

# ── Sizing ────────────────────────────────────────────────────────────────────
F_GLOBAL          = config.BRAIN_F_GLOBAL
SIZE_MIN_PCT      = config.BRAIN_SIZE_MIN_PCT
SIZE_MAX_PCT      = config.RULE_MAX_POSITION_PCT      # single-name cap (reused)
VOL_REF           = config.BRAIN_VOL_REF
VOL_SCALAR_MIN    = config.BRAIN_VOL_SCALAR_MIN
VOL_SCALAR_MAX    = config.BRAIN_VOL_SCALAR_MAX
MAX_NEW_DEPLOY_PCT = config.BRAIN_MAX_NEW_DEPLOY_PCT
MAX_THEME_PCT     = config.RULE_MAX_THEME_PCT         # theme cap (reused)
THEME_CAP_EXEMPT  = config.RULE_THEME_CAP_EXEMPT      # theme(s) exempt from the cap

# ── Quality pillar ────────────────────────────────────────────────────────────
Q_REV_LOW, Q_REV_HIGH   = config.BRAIN_Q_REV_LOW, config.BRAIN_Q_REV_HIGH
Q_GM_LOW, Q_GM_HIGH     = config.BRAIN_Q_GM_LOW, config.BRAIN_Q_GM_HIGH
Q_DE_GOOD, Q_DE_BAD     = config.BRAIN_Q_DE_GOOD, config.BRAIN_Q_DE_BAD
Q_CR_LOW, Q_CR_HIGH     = config.BRAIN_Q_CR_LOW, config.BRAIN_Q_CR_HIGH
Q_CATASTROPHIC_CAP      = config.BRAIN_Q_CATASTROPHIC_CAP

# ── Valuation pillar ──────────────────────────────────────────────────────────
V_PEG_LOW, V_PEG_HIGH   = config.BRAIN_V_PEG_LOW, config.BRAIN_V_PEG_HIGH
V_PE_LOW, V_PE_HIGH     = config.BRAIN_V_PE_LOW, config.BRAIN_V_PE_HIGH
V_NEUTRAL               = config.BRAIN_V_NEUTRAL

# ── Trend pillar ──────────────────────────────────────────────────────────────
T_REGIME_TERM = config.BRAIN_T_REGIME_TERM
T_MOM_SCALE   = config.BRAIN_T_MOM_SCALE
T_MA50_SCALE  = config.BRAIN_T_MA50_SCALE

# ── Entry-timing pillar ───────────────────────────────────────────────────────
E_PULLBACK_PEAK_LOW  = config.BRAIN_E_PULLBACK_PEAK_LOW
E_PULLBACK_PEAK_HIGH = config.BRAIN_E_PULLBACK_PEAK_HIGH
E_PULLBACK_MAX       = config.BRAIN_E_PULLBACK_MAX
E_MA_PROXIMITY_PCT   = config.BRAIN_E_MA_PROXIMITY_PCT
E_RSI_LOW            = config.BRAIN_E_RSI_LOW
E_RSI_HIGH           = config.BRAIN_E_RSI_HIGH
E_RSI_OVERBOUGHT     = config.BRAIN_E_RSI_OVERBOUGHT
E_RSI_OVERSOLD       = config.BRAIN_E_RSI_OVERSOLD

# ── Revisions pillar (soft only — never forces a sell) ────────────────────────
R_NORM = config.BRAIN_R_NORM

# ── Exit stack ────────────────────────────────────────────────────────────────
EXIT_REV_DECLINE     = config.BRAIN_EXIT_REV_DECLINE
EXIT_GM_COLLAPSE     = config.BRAIN_EXIT_GM_COLLAPSE
TRAIL_ATR_MULT       = config.BRAIN_TRAIL_ATR_MULT
TRIM_GAIN_PCT        = config.BRAIN_TRIM_GAIN_PCT
TRIM_RSI             = config.BRAIN_TRIM_RSI
TRIM_MA_EXT_ATR_MULT = config.BRAIN_TRIM_MA_EXT_ATR_MULT
TRIM_FRACTION        = config.BRAIN_TRIM_FRACTION
