"""
Trend-regime classification. Lets entry logic adapt — buy pullbacks in a rising trend,
demand deeper capitulation in a genuine downtrend.

Uses only scalar fields in market_data (price, ma_50, ma_200, ma_200_slope_pct) — no
series, so it is cheap and entry-date-independent.

A name trading *below* its 200-MA is not automatically a downtrend: if the dip is
shallow and the 200-MA is still rising, it is a PULLBACK in an intact long-term uptrend
(a buyable dip, not a broken trend). The 200-MA slope is the primary "still rising"
signal; `ma_50 >= ma_200` (golden-cross structure) is the fallback when slope is unknown.
"""

from __future__ import annotations

from src.brain import params

UPTREND = "uptrend"
PULLBACK = "pullback"
DOWNTREND = "downtrend"
NEUTRAL = "neutral"


def _long_term_trend_intact(market_data: dict, price: float, ma_200: float, ma_50: float | None) -> bool:
    """Below the 200-MA, decide whether the long-term uptrend is still intact (a pullback)
    or genuinely broken (a downtrend). Requires a *shallow* dip AND a *rising* 200-MA."""
    below_pct = (ma_200 - price) / ma_200 * 100
    if below_pct > params.REGIME_PULLBACK_MAX_BELOW_MA200_PCT:
        return False  # too deep below the 200-MA to still call it a pullback
    slope = market_data.get("ma_200_slope_pct")
    if slope is not None:
        return slope >= params.REGIME_SLOPE_MIN_PCT
    # No slope available — fall back to golden-cross structure as the rising-trend proxy.
    return ma_50 is not None and ma_50 >= ma_200


def classify(market_data: dict) -> str:
    price = market_data.get("current_price")
    ma_50 = market_data.get("ma_50")
    ma_200 = market_data.get("ma_200")
    if price is None:
        return NEUTRAL

    if ma_200 is None:
        # Insufficient history for a long-term trend — fall back to the 50-MA.
        if ma_50 is None:
            return NEUTRAL
        return UPTREND if price > ma_50 else DOWNTREND

    if price < ma_200:
        # Below the long-term line: a shallow dip in a still-rising trend is a pullback,
        # not a downtrend, so entry logic keeps buying the dip rather than demanding capitulation.
        return PULLBACK if _long_term_trend_intact(market_data, price, ma_200, ma_50) else DOWNTREND
    # price >= ma_200: an uptrend only if the shorter MA confirms rising structure.
    if ma_50 is not None and ma_50 >= ma_200:
        return UPTREND
    return NEUTRAL


def is_uptrend(market_data: dict) -> bool:
    return classify(market_data) == UPTREND
