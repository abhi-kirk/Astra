"""
Trend-regime classification. Lets entry logic adapt — buy pullbacks in an uptrend,
demand deeper capitulation in a downtrend.

Uses only scalar fields in market_data (price, ma_50, ma_200) — no series, so it is
cheap and entry-date-independent. `ma_50 >= ma_200` is the up-structure (golden-cross)
proxy for a *rising* long-term trend without needing the MA slope series.
"""

from __future__ import annotations

UPTREND = "uptrend"
DOWNTREND = "downtrend"
NEUTRAL = "neutral"


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
        return DOWNTREND
    # price >= ma_200: an uptrend only if the shorter MA confirms rising structure.
    if ma_50 is not None and ma_50 >= ma_200:
        return UPTREND
    return NEUTRAL


def is_uptrend(market_data: dict) -> bool:
    return classify(market_data) == UPTREND
