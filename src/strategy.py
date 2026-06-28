"""
Strategy engine: quality filter, technical signals, hard rule enforcement.

Reads convictions.json for theme/ticker guidance and applies the screening
framework defined in CLAUDE.md. Returns structured signals for the agent layer.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict


@dataclass
class Signal:
    ticker: str
    action: str              # "buy", "sell", "hold", "review", "blocked"
    conviction_match: bool
    quality_pass: bool
    technical_pass: bool
    hard_rule_block: str | None   # None if no block, else reason string
    reasons: list[str]
    risk_flags: list[str]
    suggested_position_pct: float | None  # % of portfolio, None if no action

    def to_dict(self):
        return asdict(self)


# ---------------------------------------------------------------------------
# Conviction helpers
# ---------------------------------------------------------------------------

def is_excluded(ticker: str, convictions: dict) -> str | None:
    """Returns exclusion reason if ticker is hard-excluded, else None."""
    for excl in convictions.get("exclusions", []):
        if excl["ticker"] == ticker:
            return excl["reason"]
    return None


def get_ticker_guidance(ticker: str, convictions: dict) -> dict:
    """
    Return the most specific guidance for a ticker — individual holding overrides theme.
    Returns dict with keys: status, notes, theme, do_not_add, hold_only
    """
    # Check individual holdings first
    individual = convictions.get("individual_holdings", {}).get(ticker)
    if individual:
        return {
            "status": individual.get("status", "hold"),
            "notes": individual.get("thesis", ""),
            "action_note": individual.get("action", ""),
            "theme": None,
            "do_not_add": individual.get("status") == "do_not_add",
            "hold_only": individual.get("status") == "hold",
        }

    # Check themes
    for theme_name, theme in convictions.get("themes", {}).items():
        if ticker in theme.get("approved", []):
            return {
                "status": "approved",
                "notes": theme.get("notes", {}).get(ticker, ""),
                "action_note": "",
                "theme": theme_name,
                "do_not_add": False,
                "hold_only": False,
            }
        if ticker in theme.get("preferred", []):
            return {
                "status": "preferred",
                "notes": theme.get("notes", {}).get(ticker, ""),
                "action_note": "",
                "theme": theme_name,
                "do_not_add": False,
                "hold_only": False,
            }
        if ticker in theme.get("hold_only", []):
            return {
                "status": "hold_only",
                "notes": theme.get("notes", {}).get(ticker, ""),
                "action_note": "",
                "theme": theme_name,
                "do_not_add": True,
                "hold_only": True,
            }
        if ticker in theme.get("do_not_add", []):
            return {
                "status": "do_not_add",
                "notes": theme.get("notes", {}).get(ticker, ""),
                "action_note": "",
                "theme": theme_name,
                "do_not_add": True,
                "hold_only": False,
            }

    return {"status": "unknown", "notes": "", "action_note": "", "theme": None,
            "do_not_add": False, "hold_only": False}


# ---------------------------------------------------------------------------
# Hard rules
# ---------------------------------------------------------------------------

def check_hard_rules(
    ticker: str,
    position: dict,
    market_data: dict,
    convictions: dict,
    portfolio_summary: dict,
) -> str | None:
    """
    Returns a block reason string if a hard rule is violated, else None.
    portfolio_summary: {total_value, theme_allocations: {theme: pct}}
    """
    # Rule 1: Hard exclusions (TSLA etc.)
    excl = is_excluded(ticker, convictions)
    if excl:
        return f"HARD EXCLUSION: {excl}"

    guidance = get_ticker_guidance(ticker, convictions)

    # Rule 2: Do-not-add
    if guidance["do_not_add"]:
        return f"DO NOT ADD: {guidance['notes']}"

    # Rule 3: Averaging-down cap
    avg_cost = position.get("avg_cost", 0)
    current_price = market_data.get("current_price", 0)
    num_buys = position.get("num_buys", 0)
    if avg_cost and current_price and avg_cost > 0:
        drawdown_pct = (avg_cost - current_price) / avg_cost * 100
        if drawdown_pct > 35 and num_buys >= 3:
            return (
                f"AVERAGING DOWN CAP: position is {drawdown_pct:.0f}% below avg cost "
                f"with {num_buys} buys already. Re-approve thesis before adding."
            )

    # Rule 4: Max single-name position size
    total_value = portfolio_summary.get("total_value", 0)
    position_value = position.get("shares", 0) * current_price
    if total_value and (position_value / total_value) > 0.10:
        return f"POSITION LIMIT: already >{10:.0f}% of portfolio (${position_value:,.0f})"

    # Rule 5: Theme concentration
    theme = guidance.get("theme")
    if theme:
        theme_pct = portfolio_summary.get("theme_allocations", {}).get(theme, 0)
        if theme_pct > 0.15:
            return f"THEME LIMIT: {theme} theme already at {theme_pct:.0%} of portfolio (max 15%)"

    return None


# ---------------------------------------------------------------------------
# Quality filter
# ---------------------------------------------------------------------------

def quality_filter(ticker: str, market_data: dict) -> tuple[bool, list[str], list[str]]:
    """
    Returns (pass: bool, reasons_passed: list, risk_flags: list).
    Applies the 5-point quality checklist from CLAUDE.md.
    """
    passed = []
    flags = []

    rev_growth = market_data.get("revenue_growth_yoy")
    if rev_growth is not None:
        if rev_growth > 0.10:
            passed.append(f"Revenue growth {rev_growth:.0%} YoY ✓")
        elif rev_growth > 0:
            flags.append(f"Revenue growth weak: {rev_growth:.0%} YoY")
        else:
            flags.append(f"Revenue declining: {rev_growth:.0%} YoY")
    else:
        flags.append("Revenue growth data unavailable")

    gross_margin = market_data.get("gross_margins")
    if gross_margin is not None:
        if gross_margin > 0.30:
            passed.append(f"Gross margin {gross_margin:.0%} ✓")
        elif gross_margin > 0:
            flags.append(f"Gross margin low: {gross_margin:.0%}")
        else:
            flags.append(f"Negative gross margin: {gross_margin:.0%}")
    else:
        flags.append("Gross margin data unavailable")

    de_ratio = market_data.get("debt_to_equity")
    if de_ratio is not None:
        if de_ratio < 150:
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

    # Pass if at least 2 hard positives and no catastrophic flags
    catastrophic = any(
        "Negative gross margin" in f or "Revenue declining" in f
        for f in flags
        if "unavailable" not in f
    )
    quality_pass = len(passed) >= 2 and not catastrophic

    return quality_pass, passed, flags


# ---------------------------------------------------------------------------
# Technical signal
# ---------------------------------------------------------------------------

def technical_signal(market_data: dict) -> tuple[bool, list[str]]:
    """
    Returns (signal: bool, reasons: list).
    Entry signal: >15% below 52w high AND RSI < 40.
    """
    reasons = []
    signals_met = 0

    pct_below = market_data.get("pct_below_52w_high")
    if pct_below is not None:
        if pct_below >= 15:
            reasons.append(f"{pct_below:.1f}% below 52w high — dip signal ✓")
            signals_met += 1
        else:
            reasons.append(f"Only {pct_below:.1f}% below 52w high — no dip")

    rsi = market_data.get("rsi_14")
    if rsi is not None:
        if rsi < 40:
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

    return signals_met >= 2, reasons


# ---------------------------------------------------------------------------
# Profit-take check
# ---------------------------------------------------------------------------

def check_profit_take(ticker: str, position: dict, market_data: dict) -> Signal | None:
    """Returns a 'review' signal if position is up >60% from avg cost."""
    avg_cost = position.get("avg_cost", 0)
    current_price = market_data.get("current_price", 0)
    if not avg_cost or not current_price:
        return None

    gain_pct = (current_price - avg_cost) / avg_cost * 100
    if gain_pct >= 60:
        return Signal(
            ticker=ticker,
            action="review",
            conviction_match=True,
            quality_pass=True,
            technical_pass=True,
            hard_rule_block=None,
            reasons=[f"Up {gain_pct:.0f}% from avg cost (${avg_cost:.2f} → ${current_price:.2f})"],
            risk_flags=["Profit-take trigger: consider trimming position"],
            suggested_position_pct=None,
        )
    return None


# ---------------------------------------------------------------------------
# Main screener
# ---------------------------------------------------------------------------

def screen_position(
    ticker: str,
    position: dict,
    market_data: dict,
    convictions: dict,
    portfolio_summary: dict,
) -> Signal:
    """Run all checks for a single position. Returns a Signal."""

    # Hard rules first
    block = check_hard_rules(ticker, position, market_data, convictions, portfolio_summary)
    if block:
        return Signal(
            ticker=ticker,
            action="blocked",
            conviction_match=False,
            quality_pass=False,
            technical_pass=False,
            hard_rule_block=block,
            reasons=[block],
            risk_flags=[],
            suggested_position_pct=None,
        )

    # Profit-take check on existing position
    profit_signal = check_profit_take(ticker, position, market_data)
    if profit_signal:
        return profit_signal

    guidance = get_ticker_guidance(ticker, convictions)
    conviction_match = guidance["status"] in ("approved", "preferred", "hold")

    # If hold-only, no buy signal possible
    if guidance["hold_only"]:
        return Signal(
            ticker=ticker,
            action="hold",
            conviction_match=conviction_match,
            quality_pass=False,
            technical_pass=False,
            hard_rule_block=None,
            reasons=[f"Hold-only: {guidance['notes'][:120]}"],
            risk_flags=[],
            suggested_position_pct=None,
        )

    quality_pass, quality_reasons, risk_flags = quality_filter(ticker, market_data)
    tech_pass, tech_reasons = technical_signal(market_data)

    all_reasons = quality_reasons + tech_reasons

    if conviction_match and quality_pass and tech_pass:
        action = "buy"
        # Size by conviction level
        status = guidance["status"]
        size = 0.06 if status == "preferred" else 0.04
        suggested_pct = size
    elif conviction_match and (quality_pass or tech_pass):
        action = "watch"
        suggested_pct = None
    else:
        action = "hold"
        suggested_pct = None

    return Signal(
        ticker=ticker,
        action=action,
        conviction_match=conviction_match,
        quality_pass=quality_pass,
        technical_pass=tech_pass,
        hard_rule_block=None,
        reasons=all_reasons,
        risk_flags=risk_flags,
        suggested_position_pct=suggested_pct,
    )


def screen_all_positions(
    portfolio: dict[str, dict],
    market_data: dict[str, dict],
    convictions: dict,
    full_portfolio: dict[str, dict] | None = None,
) -> list[Signal]:
    """
    Screen positions in portfolio. Returns signals sorted by action priority.
    full_portfolio: if provided, used for total_value and theme allocation
    calculations (pass when screening a single ticker but want correct sizing).
    """
    sizing_portfolio = full_portfolio if full_portfolio else portfolio
    total_value = sum(
        pos.get("shares", 0) * market_data.get(t, {}).get("current_price", 0)
        for t, pos in sizing_portfolio.items()
    )

    # Rough theme allocations (always from full sizing portfolio)
    theme_allocations: dict[str, float] = {}
    for ticker, pos in sizing_portfolio.items():
        guidance = get_ticker_guidance(ticker, convictions)
        theme = guidance.get("theme")
        if theme and total_value:
            val = pos.get("shares", 0) * market_data.get(ticker, {}).get("current_price", 0)
            theme_allocations[theme] = theme_allocations.get(theme, 0) + val / total_value

    portfolio_summary = {"total_value": total_value, "theme_allocations": theme_allocations}

    signals = []
    for ticker, position in portfolio.items():
        mdata = market_data.get(ticker, {"ticker": ticker, "error": "no_data"})
        sig = screen_position(ticker, position, mdata, convictions, portfolio_summary)
        signals.append(sig)

    priority = {"buy": 0, "review": 1, "watch": 2, "hold": 3, "blocked": 4}
    return sorted(signals, key=lambda s: priority.get(s.action, 5))
