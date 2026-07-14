"""
Hard rules — non-negotiable strategy blocks (TSLA exclusion, do-not-add, averaging-down,
position/theme limits) enforced by the brain before any scoring, on both tracks. The
Autotrader mirrors the brain's decisions, so it does not re-run these. strategy.py
re-exports them.
"""

from __future__ import annotations

from src import config
from src.brain.conviction import get_ticker_guidance, is_excluded


def check_hard_rules(
    ticker: str,
    position: dict,
    market_data: dict,
    convictions: dict,
    portfolio_summary: dict,
) -> str | None:
    """Return a block reason string if a hard rule is violated, else None."""
    excl = is_excluded(ticker, convictions)
    if excl:
        return f"HARD EXCLUSION: {excl}"

    guidance = get_ticker_guidance(ticker, convictions)

    if guidance["do_not_add"]:
        return f"DO NOT ADD: {guidance['notes']}"

    avg_cost = position.get("avg_cost", 0)
    current_price = market_data.get("current_price", 0)
    num_buys = position.get("num_buys", 0)
    if avg_cost and current_price and avg_cost > 0:
        drawdown_pct = (avg_cost - current_price) / avg_cost * 100
        if drawdown_pct > config.rules.averaging_down_drawdown * 100 and num_buys >= config.rules.averaging_down_max_buys:
            return (
                f"AVERAGING DOWN CAP: position is {drawdown_pct:.0f}% below avg cost "
                f"with {num_buys} buys already. Re-approve thesis before adding."
            )

    total_value = portfolio_summary.get("total_value", 0)
    position_value = position.get("shares", 0) * current_price
    if total_value and (position_value / total_value) > config.rules.max_position_pct:
        return f"POSITION LIMIT: already >{config.rules.max_position_pct:.0%} of portfolio (${position_value:,.0f})"

    theme = guidance.get("theme")
    if theme and theme != config.rules.theme_cap_exempt:
        theme_pct = portfolio_summary.get("theme_allocations", {}).get(theme, 0)
        if theme_pct > config.rules.max_theme_pct:
            return f"THEME LIMIT: {theme} theme already at {theme_pct:.0%} of portfolio (max {config.rules.max_theme_pct:.0%})"

    return None


def compute_portfolio_summary(
    portfolio: dict[str, dict],
    market_data: dict[str, dict],
    convictions: dict,
) -> dict:
    """Total value + per-theme allocation fractions. Used by the screener and by
    Autotrader's guardrails so both apply identical concentration limits."""
    total_value = sum(
        pos.get("shares", 0) * market_data.get(t, {}).get("current_price", 0)
        for t, pos in portfolio.items()
    )
    theme_allocations: dict[str, float] = {}
    for ticker, pos in portfolio.items():
        theme = get_ticker_guidance(ticker, convictions).get("theme")
        if theme and total_value:
            val = pos.get("shares", 0) * market_data.get(ticker, {}).get("current_price", 0)
            theme_allocations[theme] = theme_allocations.get(theme, 0) + val / total_value
    return {"total_value": total_value, "theme_allocations": theme_allocations}
