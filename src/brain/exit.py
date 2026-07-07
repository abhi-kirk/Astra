"""
Exit stack — four layers, first match wins. Intent taxonomy (from convictions) gates
which layers apply:

    thesis_hold   → layer 1 only  (sell only if the thesis objectively breaks)
    opportunistic → layers 1–3    (thesis, trailing stop, parabolic trim)
    written_off   → layer 4       (opportunity-cost redeploy nudge)

Layer 1 force-sells only on OBJECTIVE fundamental breakdown. A manual conviction
downgrade (do_not_add / written_off) is a strong nudge, not an auto-liquidation —
consistent with ASTRA's "flag, don't auto-sell" stance.

Layer 2 is a Chandelier exit: stop = recent_swing_high − k·ATR (rolling-window high,
so it is stateless and entry-date-independent), confirmed by price below the 50-MA.
A winner at new highs with an intact trend never trips it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from src.brain import params

# close_reason values (drive the paper ledger + Autotrader mirror)
THESIS_INVALIDATION = "thesis_invalidation"
TRAILING_STOP = "trailing_stop"
PARABOLIC_TRIM = "parabolic_trim"


@dataclass
class ExitSignal:
    action: str | None = None            # "sell" | "trim" | None
    close_reason: str | None = None
    trim_fraction: float | None = None   # set for "trim"
    reasons: list[str] = field(default_factory=list)
    risk_flags: list[str] = field(default_factory=list)


def _thesis_broken(md: dict) -> str | None:
    """Layer 1 fires ONLY on objective fundamental breakdown. Analyst revisions are a
    SOFT signal (the R pillar) and never force a sell on their own — a near-term EPS
    trim is not a broken multi-year thesis for a conviction hold."""
    rev = md.get("revenue_growth_yoy")
    if rev is not None and rev < params.EXIT_REV_DECLINE:
        return f"Revenue declining {rev:.0%} YoY — thesis broken"
    gm = md.get("gross_margins")
    if gm is not None and gm < params.EXIT_GM_COLLAPSE:
        return f"Gross margin collapsed to {gm:.0%} — thesis broken"
    return None


def _trailing_hit(md: dict) -> str | None:
    price = md.get("current_price")
    atr = md.get("atr_14")
    swing = md.get("recent_swing_high")
    ma_50 = md.get("ma_50")
    if not (price and atr and swing and ma_50 and atr > 0):
        return None
    stop = swing - params.TRAIL_ATR_MULT * atr
    if price < stop and price < ma_50:
        return (f"Trend break: ${price:.2f} < Chandelier stop ${stop:.2f} "
                f"(high ${swing:.2f} − {params.TRAIL_ATR_MULT:.0f}×ATR) and below 50-MA")
    return None


def _parabolic(position: dict, md: dict) -> str | None:
    avg_cost = position.get("avg_cost", 0)
    price = md.get("current_price")
    rsi = md.get("rsi_14")
    atr = md.get("atr_14")
    vs_ma50 = md.get("price_vs_ma50_pct")
    if not (avg_cost and price and rsi is not None and atr and vs_ma50 is not None and price > 0):
        return None
    gain = (price - avg_cost) / avg_cost
    atr_pct = atr / price * 100.0
    extended = vs_ma50 > params.TRIM_MA_EXT_ATR_MULT * atr_pct
    if gain >= params.TRIM_GAIN_PCT and rsi > params.TRIM_RSI and extended:
        return (f"Parabolic: +{gain:.0%} gain, RSI {rsi:.0f}, "
                f"{vs_ma50:.0f}% above 50-MA (>{params.TRIM_MA_EXT_ATR_MULT:.0f}×ATR)")
    return None


def evaluate(position: dict, md: dict, guidance: dict) -> ExitSignal:
    """Return an ExitSignal; action is None when no exit fires."""
    intent = guidance.get("intent", "opportunistic")

    # Layer 1 — thesis invalidation (all holding intents)
    thesis = _thesis_broken(md)
    if thesis:
        return ExitSignal(action="sell", close_reason=THESIS_INVALIDATION, reasons=[thesis])

    if intent == "thesis_hold":
        return ExitSignal()  # thesis intact → hold, ignore price action

    if intent == "written_off":
        # Layer 4 — nudge only; never an auto-dump
        return ExitSignal(risk_flags=[
            "Written-off position — consider redeploying capital into higher-conviction names"
        ])

    # opportunistic → Layer 2 then Layer 3
    trail = _trailing_hit(md)
    if trail:
        return ExitSignal(action="sell", close_reason=TRAILING_STOP, reasons=[trail])

    para = _parabolic(position, md)
    if para:
        return ExitSignal(
            action="trim", close_reason=PARABOLIC_TRIM, trim_fraction=params.TRIM_FRACTION,
            reasons=[para],
            risk_flags=[f"Parabolic trim: trimming ~{params.TRIM_FRACTION:.0%}, keeping a runner"],
        )

    return ExitSignal()
