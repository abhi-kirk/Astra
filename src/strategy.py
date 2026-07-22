"""
Strategy engine — thin adapter over the modular brain (src/brain/). Decision logic
lives in src/brain/. This module:
  • re-exports the brain's conviction + hard-rule surface so agent_guardrails,
    agent_executor, and tests import these names from here;
  • runs the per-portfolio screen and the cross-position sizing allocation;
  • provides quality_filter / technical_signal, which the exploration module uses to
    screen new candidate names (a separate concern from position scoring).

See src/brain/README.md for the scoring method and every tunable.
"""

from __future__ import annotations

from typing import Any

from src import config
from src.brain import sizing
from src.brain.conviction import get_ticker_guidance, is_excluded  # noqa: F401 (re-export)
from src.brain.hard_rules import check_hard_rules, compute_portfolio_summary  # noqa: F401 (re-export)
from src.brain.score import screen_position  # noqa: F401 (re-export)

Signal = dict[str, Any]


def make_signal(**kwargs) -> Signal:
    return dict(**kwargs)


# ---------------------------------------------------------------------------
# Candidate screening helpers (used by src/exploration.py for NEW names)
# ---------------------------------------------------------------------------

def quality_filter(ticker: str, market_data: dict) -> tuple[bool, list[str], list[str]]:
    """
    Returns (pass, reasons_passed, risk_flags). Applies the 5-point quality checklist
    to vet new exploration candidates; position scoring uses brain.factors.quality.
    """
    passed = []
    flags = []

    rev_growth = market_data.get("revenue_growth_yoy")
    if rev_growth is not None:
        if rev_growth > config.quality.min_revenue_growth:
            passed.append(f"Revenue growth {rev_growth:.0%} YoY ✓")
        elif rev_growth > 0:
            flags.append(f"Revenue growth weak: {rev_growth:.0%} YoY")
        else:
            flags.append(f"Revenue declining: {rev_growth:.0%} YoY")
    else:
        flags.append("Revenue growth data unavailable")

    gross_margin = market_data.get("gross_margins")
    if gross_margin is not None:
        if gross_margin > config.quality.min_gross_margin:
            passed.append(f"Gross margin {gross_margin:.0%} ✓")
        elif gross_margin > 0:
            flags.append(f"Gross margin low: {gross_margin:.0%}")
        else:
            flags.append(f"Negative gross margin: {gross_margin:.0%}")
    else:
        flags.append("Gross margin data unavailable")

    de_ratio = market_data.get("debt_to_equity")
    if de_ratio is not None:
        if de_ratio < config.quality.max_debt_equity:
            passed.append(f"Debt/equity {de_ratio:.0f} manageable ✓")
        else:
            flags.append(f"High debt/equity: {de_ratio:.0f}")
    else:
        flags.append("Debt/equity data unavailable")

    fcf = market_data.get("free_cashflow")
    if fcf is not None:
        if fcf > 0:
            passed.append(f"Positive free cash flow (${fcf/1e6:.0f}M) ✓")
        else:
            flags.append(f"Negative free cash flow (${fcf/1e6:.0f}M)")
    else:
        flags.append("Free cash flow data unavailable")

    catastrophic = any(
        "Negative gross margin" in f or "Revenue declining" in f
        for f in flags
        if "unavailable" not in f
    )
    quality_pass = len(passed) >= config.quality.min_checks_to_pass and not catastrophic

    return quality_pass, passed, flags


def technical_signal(market_data: dict) -> tuple[bool, list[str]]:
    """
    Returns (signal, reasons). Dip-entry check for exploration candidate vetting;
    position entry timing uses brain.factors.entry (ATR-based, regime-aware).
    """
    reasons = []
    signals_met = 0

    pct_below = market_data.get("pct_below_52w_high")
    if pct_below is not None:
        if pct_below >= config.tech.min_pct_below_52w_high:
            reasons.append(f"{pct_below:.1f}% below 52w high — dip signal ✓")
            signals_met += 1
        else:
            reasons.append(f"Only {pct_below:.1f}% below 52w high — no dip")

    rsi = market_data.get("rsi_14")
    if rsi is not None:
        if rsi < config.tech.max_rsi:
            reasons.append(f"RSI {rsi:.1f} — oversold ✓")
            signals_met += 1
        elif rsi < 50:
            reasons.append(f"RSI {rsi:.1f} — neutral")
        else:
            reasons.append(f"RSI {rsi:.1f} — not oversold")

    vs_ma50 = market_data.get("price_vs_ma50_pct")
    if vs_ma50 is not None:
        if vs_ma50 < -10:
            reasons.append(f"{abs(vs_ma50):.1f}% below 50-day MA ✓")
        else:
            reasons.append(f"Price {'+' if vs_ma50 >= 0 else ''}{vs_ma50:.1f}% vs 50-day MA")

    return signals_met >= config.tech.signals_required, reasons


# ---------------------------------------------------------------------------
# Portfolio screen + cross-position sizing
# ---------------------------------------------------------------------------

def screen_all_positions(
    portfolio: dict[str, dict],
    market_data: dict[str, dict],
    convictions: dict,
    full_portfolio: dict[str, dict] | None = None,
    market_drawdown_pct: float | None = None,
) -> list[Signal]:
    """Screen all positions, then allocate sizes across BUYs under portfolio caps.

    `market_drawdown_pct` (SPY % below its 52wk high) drives the conviction-primary market-cycle
    overlay — buys size up a little when the broad market is down. None / off-flag → no effect."""
    sizing_portfolio = full_portfolio if full_portfolio else portfolio
    portfolio_summary = compute_portfolio_summary(sizing_portfolio, market_data, convictions)
    signals = [
        screen_position(ticker, pos, market_data.get(ticker, {"error": "no_data"}), convictions, portfolio_summary)
        for ticker, pos in portfolio.items()
    ]

    # Iterative constrained allocation across simultaneous BUYs (may downgrade some to watch).
    buys = [s for s in signals if s["action"] == "buy"]
    if buys:
        sizing.allocate(buys, portfolio_summary, sizing.market_cycle_multiplier(market_drawdown_pct))

    priority = {"buy": 0, "sell": 1, "trim": 1, "watch": 2, "hold": 3, "blocked": 4}
    return sorted(signals, key=lambda s: priority.get(s["action"], 5))
