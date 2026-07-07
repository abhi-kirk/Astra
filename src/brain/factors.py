"""
Factor pillars — each is a pure function of `market_data` returning a score plus a
human-readable note. Missing inputs are DROPPED from a pillar's internal average and
never penalized, so thin-coverage names (small caps, ADRs) degrade gracefully.

Scales:
  quality Q, valuation V, entry E ∈ [0, 1]
  trend T, revisions R           ∈ [-1, 1]   (signed)
"""

from __future__ import annotations

from typing import NamedTuple

from src.brain import params, regime
from src.brain.normalize import clamp, mean_defined, smooth, squash, tent


class Factor(NamedTuple):
    value: float | None   # None ⇒ undeterminable, drop from composite
    note: str


# ── Quality ───────────────────────────────────────────────────────────────────
def quality(md: dict) -> Factor:
    subs: list[float | None] = []

    rev = md.get("revenue_growth_yoy")
    subs.append(smooth(rev, params.Q_REV_LOW, params.Q_REV_HIGH) if rev is not None else None)

    gm = md.get("gross_margins")
    subs.append(smooth(gm, params.Q_GM_LOW, params.Q_GM_HIGH) if gm is not None else None)

    de = md.get("debt_to_equity")
    # descending ramp: DE_GOOD → 1, DE_BAD → 0
    subs.append(smooth(de, params.Q_DE_BAD, params.Q_DE_GOOD) if de is not None else None)

    cr = md.get("current_ratio")
    subs.append(smooth(cr, params.Q_CR_LOW, params.Q_CR_HIGH) if cr is not None else None)

    fcf = md.get("free_cashflow")
    subs.append((1.0 if fcf > 0 else 0.0) if fcf is not None else None)

    q = mean_defined(subs)
    if q is None:
        return Factor(None, "quality: no data")

    catastrophic = (rev is not None and rev < params.EXIT_REV_DECLINE) or (gm is not None and gm < 0)
    if catastrophic:
        q = min(q, params.Q_CATASTROPHIC_CAP)
        return Factor(q, f"quality {q:.2f} (catastrophic-capped)")
    return Factor(q, f"quality {q:.2f}")


# ── Valuation (best-effort; missing → neutral) ────────────────────────────────
def valuation(md: dict) -> Factor:
    subs: list[float | None] = []

    peg = md.get("peg_ratio")
    if peg is not None and peg > 0:
        subs.append(1.0 - smooth(peg, params.V_PEG_LOW, params.V_PEG_HIGH))

    fpe = md.get("forward_pe")
    if fpe is not None and fpe > 0:
        subs.append(1.0 - smooth(fpe, params.V_PE_LOW, params.V_PE_HIGH))

    v = mean_defined(subs)
    if v is None:
        return Factor(params.V_NEUTRAL, f"valuation {params.V_NEUTRAL:.2f} (neutral, no data)")
    return Factor(v, f"valuation {v:.2f}")


# ── Trend (regime + momentum) ─────────────────────────────────────────────────
def trend(md: dict) -> Factor:
    if md.get("current_price") is None:
        return Factor(None, "trend: no price")

    reg = regime.classify(md)
    terms: list[float] = []
    if reg == regime.UPTREND:
        terms.append(params.T_REGIME_TERM)
    elif reg == regime.DOWNTREND:
        terms.append(-params.T_REGIME_TERM)
    else:
        terms.append(0.0)

    mom = md.get("mom_12_1")
    if mom is not None:
        terms.append(squash(mom, params.T_MOM_SCALE))

    vs_ma50 = md.get("price_vs_ma50_pct")
    if vs_ma50 is not None:
        terms.append(squash(vs_ma50, params.T_MA50_SCALE))

    t = clamp(sum(terms) / len(terms), -1.0, 1.0)
    return Factor(t, f"trend {t:+.2f} ({reg})")


# ── Entry timing (pullback in ATR units + proximity + RSI zone) ───────────────
def entry(md: dict) -> Factor:
    price = md.get("current_price")
    if price is None:
        return Factor(None, "entry: no price")

    reg = regime.classify(md)
    subs: list[float | None] = []

    atr = md.get("atr_14")
    swing = md.get("recent_swing_high")
    if atr and swing and atr > 0:
        depth = (swing - price) / atr  # how many ATRs below the recent high
        subs.append(tent(depth, params.E_PULLBACK_PEAK_LOW, params.E_PULLBACK_PEAK_HIGH,
                          params.E_PULLBACK_MAX))
    else:
        subs.append(None)

    vs_ma50 = md.get("price_vs_ma50_pct")
    if vs_ma50 is not None:
        # proximity to the 50-MA (pullback to dynamic support): near → 1, far → 0
        subs.append(1.0 - smooth(abs(vs_ma50), 0.0, params.E_MA_PROXIMITY_PCT))
    else:
        subs.append(None)

    rsi = md.get("rsi_14")
    if rsi is not None:
        if reg == regime.DOWNTREND:
            # reward deep oversold (classic capitulation dip-buy)
            subs.append(smooth(rsi, params.E_RSI_HIGH, params.E_RSI_OVERSOLD))
        else:
            # healthy pullback: full score at/below the band, fade toward overbought
            if rsi <= params.E_RSI_HIGH:
                subs.append(1.0)
            else:
                subs.append(smooth(rsi, params.E_RSI_OVERBOUGHT, params.E_RSI_HIGH))
    else:
        subs.append(None)

    e = mean_defined(subs)
    if e is None:
        return Factor(None, "entry: no data")
    return Factor(e, f"entry {e:.2f}")


# ── Revisions (soft, best-effort) ─────────────────────────────────────────────
def _net_revisions(md: dict) -> int | None:
    """Net upward-minus-downward analyst revisions (30d). None if no coverage."""
    rev = md.get("revisions")
    if not rev:
        return None
    up = rev.get("up") or 0
    down = rev.get("down") or 0
    if up == 0 and down == 0:
        return None
    return up - down


def revisions(md: dict) -> Factor:
    net = _net_revisions(md)
    if net is None:
        return Factor(None, "revisions: no coverage")
    r = clamp(net / params.R_NORM, -1.0, 1.0)
    return Factor(r, f"revisions {r:+.2f} (net {net:+d})")
