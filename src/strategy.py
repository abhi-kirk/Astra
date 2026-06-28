"""
Strategy engine: quality filter, technical signals, hard rule enforcement.

Reads convictions.json for theme/ticker guidance and applies the screening
framework defined in CLAUDE.md. Returns structured signals for the agent layer.
"""

from __future__ import annotations

from typing import Any

from src.config import (
    QUALITY_MAX_DEBT_EQUITY,
    QUALITY_MIN_CHECKS_TO_PASS,
    QUALITY_MIN_GROSS_MARGIN,
    QUALITY_MIN_REVENUE_GROWTH,
    RULE_AVERAGING_DOWN_DRAWDOWN,
    RULE_AVERAGING_DOWN_MAX_BUYS,
    RULE_MAX_POSITION_PCT,
    RULE_MAX_THEME_PCT,
    RULE_PROFIT_TAKE_PCT,
    SIZE_APPROVED_PCT,
    SIZE_PREFERRED_PCT,
    TECH_MAX_RSI,
    TECH_MIN_PCT_BELOW_52W_HIGH,
    TECH_SIGNALS_REQUIRED,
)


Signal = dict[str, Any]


def make_signal(**kwargs) -> Signal:
    return dict(**kwargs)


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
    Returns dict with keys: status, notes, theme, do_not_add, hold_only, intent, original_catalyst
    """
    meta = convictions.get("ticker_metadata", {}).get(ticker, {})
    intent = meta.get("intent", "opportunistic")  # default: apply profit-take logic
    original_catalyst = meta.get("original_catalyst")

    def _with_meta(base: dict) -> dict:
        return {**base, "intent": intent, "original_catalyst": original_catalyst}

    # Check individual holdings first
    individual = convictions.get("individual_holdings", {}).get(ticker)
    if individual:
        return _with_meta({
            "status": individual.get("status", "hold"),
            "notes": individual.get("thesis", ""),
            "action_note": individual.get("action", ""),
            "theme": None,
            "do_not_add": individual.get("status") == "do_not_add",
            "hold_only": individual.get("status") == "hold",
        })

    # Check themes
    for theme_name, theme in convictions.get("themes", {}).items():
        if ticker in theme.get("approved", []):
            return _with_meta({
                "status": "approved",
                "notes": theme.get("notes", {}).get(ticker, ""),
                "action_note": "",
                "theme": theme_name,
                "do_not_add": False,
                "hold_only": False,
            })
        if ticker in theme.get("preferred", []):
            return _with_meta({
                "status": "preferred",
                "notes": theme.get("notes", {}).get(ticker, ""),
                "action_note": "",
                "theme": theme_name,
                "do_not_add": False,
                "hold_only": False,
            })
        if ticker in theme.get("hold_only", []):
            return _with_meta({
                "status": "hold_only",
                "notes": theme.get("notes", {}).get(ticker, ""),
                "action_note": "",
                "theme": theme_name,
                "do_not_add": True,
                "hold_only": True,
            })
        if ticker in theme.get("do_not_add", []):
            return _with_meta({
                "status": "do_not_add",
                "notes": theme.get("notes", {}).get(ticker, ""),
                "action_note": "",
                "theme": theme_name,
                "do_not_add": True,
                "hold_only": False,
            })

    return _with_meta({"status": "unknown", "notes": "", "action_note": "", "theme": None,
                        "do_not_add": False, "hold_only": False})


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
        if drawdown_pct > RULE_AVERAGING_DOWN_DRAWDOWN * 100 and num_buys >= RULE_AVERAGING_DOWN_MAX_BUYS:
            return (
                f"AVERAGING DOWN CAP: position is {drawdown_pct:.0f}% below avg cost "
                f"with {num_buys} buys already. Re-approve thesis before adding."
            )

    # Rule 4: Max single-name position size
    total_value = portfolio_summary.get("total_value", 0)
    position_value = position.get("shares", 0) * current_price
    if total_value and (position_value / total_value) > RULE_MAX_POSITION_PCT:
        return f"POSITION LIMIT: already >{RULE_MAX_POSITION_PCT:.0%} of portfolio (${position_value:,.0f})"

    # Rule 5: Theme concentration (speculative themes only — core_tech is exempt)
    theme = guidance.get("theme")
    if theme and theme != "core_tech":
        theme_pct = portfolio_summary.get("theme_allocations", {}).get(theme, 0)
        if theme_pct > RULE_MAX_THEME_PCT:
            return f"THEME LIMIT: {theme} theme already at {theme_pct:.0%} of portfolio (max {RULE_MAX_THEME_PCT:.0%})"

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
        if rev_growth > QUALITY_MIN_REVENUE_GROWTH:
            passed.append(f"Revenue growth {rev_growth:.0%} YoY ✓")
        elif rev_growth > 0:
            flags.append(f"Revenue growth weak: {rev_growth:.0%} YoY")
        else:
            flags.append(f"Revenue declining: {rev_growth:.0%} YoY")
    else:
        flags.append("Revenue growth data unavailable")

    gross_margin = market_data.get("gross_margins")
    if gross_margin is not None:
        if gross_margin > QUALITY_MIN_GROSS_MARGIN:
            passed.append(f"Gross margin {gross_margin:.0%} ✓")
        elif gross_margin > 0:
            flags.append(f"Gross margin low: {gross_margin:.0%}")
        else:
            flags.append(f"Negative gross margin: {gross_margin:.0%}")
    else:
        flags.append("Gross margin data unavailable")

    de_ratio = market_data.get("debt_to_equity")
    if de_ratio is not None:
        if de_ratio < QUALITY_MAX_DEBT_EQUITY:
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
    quality_pass = len(passed) >= QUALITY_MIN_CHECKS_TO_PASS and not catastrophic

    return quality_pass, passed, flags


# ---------------------------------------------------------------------------
# Technical signal
# ---------------------------------------------------------------------------

def technical_signal(market_data: dict) -> tuple[bool, list[str]]:
    """
    Returns (signal: bool, reasons: list).
    Entry signal: >{TECH_MIN_PCT_BELOW_52W_HIGH}% below 52w high AND RSI < {TECH_MAX_RSI}.
    """
    reasons = []
    signals_met = 0

    pct_below = market_data.get("pct_below_52w_high")
    if pct_below is not None:
        if pct_below >= TECH_MIN_PCT_BELOW_52W_HIGH:
            reasons.append(f"{pct_below:.1f}% below 52w high — dip signal ✓")
            signals_met += 1
        else:
            reasons.append(f"Only {pct_below:.1f}% below 52w high — no dip")

    rsi = market_data.get("rsi_14")
    if rsi is not None:
        if rsi < TECH_MAX_RSI:
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

    return signals_met >= TECH_SIGNALS_REQUIRED, reasons


# ---------------------------------------------------------------------------
# Profit-take check
# ---------------------------------------------------------------------------

def check_profit_take(ticker: str, position: dict, market_data: dict, guidance: dict) -> Signal | None:
    """
    Returns a sell signal based on intent:
    - thesis_hold: exempt — sell only on thesis invalidation, not price appreciation
    - opportunistic: fire at RULE_PROFIT_TAKE_PCT with original catalyst context
    - written_off: no profit-take signal (already blocked from buying; opportunity cost
      nudge is handled via intent field passed to the advisor note prompt)
    """
    intent = guidance.get("intent", "opportunistic")
    if intent in ("thesis_hold", "written_off"):
        return None

    avg_cost = position.get("avg_cost", 0)
    current_price = market_data.get("current_price", 0)
    if not avg_cost or not current_price:
        return None

    gain_pct = (current_price - avg_cost) / avg_cost * 100
    if gain_pct >= RULE_PROFIT_TAKE_PCT * 100:
        reasons = [f"Up {gain_pct:.0f}% from avg cost (${avg_cost:.2f} → ${current_price:.2f})"]
        risk_flags = ["Profit-take trigger: consider trimming or selling position"]
        catalyst = guidance.get("original_catalyst")
        if intent == "opportunistic" and catalyst:
            risk_flags.append(f"Original catalyst: {catalyst}")
        return make_signal(
            ticker=ticker, action="sell",
            conviction_match=True, quality_pass=True, technical_pass=True,
            hard_rule_block=None, reasons=reasons, risk_flags=risk_flags,
            suggested_position_pct=None,
            intent=intent, original_catalyst=catalyst,
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
    """Run all checks for a single position. Returns a Signal dict."""
    guidance = get_ticker_guidance(ticker, convictions)
    intent = guidance.get("intent", "opportunistic")
    original_catalyst = guidance.get("original_catalyst")

    block = check_hard_rules(ticker, position, market_data, convictions, portfolio_summary)
    if block:
        risk_flags = []
        if intent == "written_off":
            risk_flags.append("Written-off position — consider redeploying capital into higher-conviction names")
        return make_signal(
            ticker=ticker, action="blocked",
            conviction_match=False, quality_pass=False, technical_pass=False,
            hard_rule_block=block, reasons=[block], risk_flags=risk_flags,
            suggested_position_pct=None,
            intent=intent, original_catalyst=original_catalyst,
        )

    profit_signal = check_profit_take(ticker, position, market_data, guidance)
    if profit_signal:
        return profit_signal

    conviction_match = guidance["status"] in ("approved", "preferred", "hold")

    if guidance["hold_only"]:
        return make_signal(
            ticker=ticker, action="hold",
            conviction_match=conviction_match, quality_pass=False, technical_pass=False,
            hard_rule_block=None,
            reasons=[f"Hold-only: {guidance['notes'][:120]}"],
            risk_flags=[], suggested_position_pct=None,
            intent=intent, original_catalyst=original_catalyst,
        )

    quality_pass, quality_reasons, risk_flags = quality_filter(ticker, market_data)
    tech_pass, tech_reasons = technical_signal(market_data)
    all_reasons = quality_reasons + tech_reasons

    if conviction_match and quality_pass and tech_pass:
        action = "buy"
        size = SIZE_PREFERRED_PCT if guidance["status"] == "preferred" else SIZE_APPROVED_PCT
    elif conviction_match and (quality_pass or tech_pass):
        action = "watch"
        size = None
    else:
        action = "hold"
        size = None

    return make_signal(
        ticker=ticker, action=action,
        conviction_match=conviction_match, quality_pass=quality_pass, technical_pass=tech_pass,
        hard_rule_block=None, reasons=all_reasons, risk_flags=risk_flags,
        suggested_position_pct=size,
        intent=intent, original_catalyst=original_catalyst,
    )


def screen_all_positions(
    portfolio: dict[str, dict],
    market_data: dict[str, dict],
    convictions: dict,
    full_portfolio: dict[str, dict] | None = None,
) -> list[Signal]:
    """Screen all positions. Returns signals sorted by action priority."""
    sizing_portfolio = full_portfolio if full_portfolio else portfolio
    total_value = sum(
        pos.get("shares", 0) * market_data.get(t, {}).get("current_price", 0)
        for t, pos in sizing_portfolio.items()
    )

    theme_allocations: dict[str, float] = {}
    for ticker, pos in sizing_portfolio.items():
        theme = get_ticker_guidance(ticker, convictions).get("theme")
        if theme and total_value:
            val = pos.get("shares", 0) * market_data.get(ticker, {}).get("current_price", 0)
            theme_allocations[theme] = theme_allocations.get(theme, 0) + val / total_value

    portfolio_summary = {"total_value": total_value, "theme_allocations": theme_allocations}
    signals = [
        screen_position(ticker, pos, market_data.get(ticker, {"error": "no_data"}), convictions, portfolio_summary)
        for ticker, pos in portfolio.items()
    ]

    priority = {"buy": 0, "sell": 1, "watch": 2, "hold": 3, "blocked": 4}
    return sorted(signals, key=lambda s: priority.get(s["action"], 5))
