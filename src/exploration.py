"""
Exploration engine: weekly discovery of new tickers within conviction themes.

Completely separate from the daily exploitation pipeline (agent.py).
Entry point: `python -m src.exploration` (run by exploration.yml, Fridays 9pm ET).

Bridges to the exploitation pipeline (kept minimal):
  - data_layer.load_convictions()        — shared source of truth
  - memory.get_latest_portfolio_snapshot() — to exclude current holdings
  - mcp.exploration_specs() / mcp_loop.run_agentic_sync() — shared client-side tool loop
  - strategy.quality_filter() / strategy.technical_signal() — reused in daily screening
  - memory.*_exploration_* helpers       — Supabase CRUD for exploration_candidates
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import chevron

from src import mcp, mcp_loop
from src.logger import timer

logger = logging.getLogger(__name__)
from src.config import (
    ADVISOR_MAX_TOOL_ROUNDS,
    ADVISOR_TOOL_TIMEOUT,
    ANTHROPIC_API_KEY,
    EXPLORATION_EFFORT,
    EXPLORATION_MAX_AV_CALLS,
    EXPLORATION_MAX_FMP_CALLS,
    EXPLORATION_MAX_SEARCHES,
    EXPLORATION_MAX_TOKENS,
    EXPLORATION_MODEL,
    EXPLORATION_TIMEOUT,
    MCP_TOOL_FAILURE_LIMIT,
)
from src.timeout import run_with_timeout

PROMPTS_DIR = Path(__file__).parent.parent / "prompts"


# ---------------------------------------------------------------------------
# Pure helpers (testable without I/O)
# ---------------------------------------------------------------------------

EXPLORATION_EXCLUDED_THEMES: set[str] = set()  # all high/very_high themes are eligible


def build_theme_queries(convictions: dict) -> dict[str, str]:
    """Return {theme_key: tavily_search_query} for high-conviction, non-excluded themes."""
    queries = {}
    for key, theme in convictions.get("themes", {}).items():
        if key in EXPLORATION_EXCLUDED_THEMES:
            continue
        conviction = theme.get("conviction", "medium")
        if conviction not in ("high", "very_high"):
            continue
        label = key.replace("_", " ")
        queries[key] = f"best emerging {label} stocks not widely held 2025 2026 under 10 billion market cap"
    return queries


def parse_candidates(text: str) -> list[dict]:
    """
    Extract the JSON array from Claude's response text.
    Returns [] if no valid JSON array is found.
    """
    match = re.search(r"```json\s*(.*?)\s*```", text, re.DOTALL)
    if not match:
        # Fallback: try a bare JSON array
        match = re.search(r"\[\s*\{.*?\}\s*\]", text, re.DOTALL)
    if not match:
        return []
    try:
        data = json.loads(match.group(1) if "```" in (match.group(0) or "") else match.group(0))
        return data if isinstance(data, list) else []
    except json.JSONDecodeError:
        return []


def filter_known_tickers(
    candidates: list[dict],
    portfolio_tickers: set[str],
    exclusion_tickers: set[str],
    already_tracked: set[str],
) -> list[dict]:
    """Remove candidates that are already held, excluded, or tracked."""
    blocked = portfolio_tickers | exclusion_tickers | already_tracked
    return [c for c in candidates if c.get("ticker", "").upper() not in blocked]


# ---------------------------------------------------------------------------
# Theme detail builder
# ---------------------------------------------------------------------------

def _build_themes_detail(convictions: dict) -> str:
    """Format active themes + thesis for the exploration prompt."""
    lines = []
    for key, theme in convictions.get("themes", {}).items():
        conviction = theme.get("conviction", "medium")
        thesis = theme.get("thesis", "No thesis documented.")
        approved = theme.get("approved", [])
        do_not_add = theme.get("do_not_add", [])
        lines.append(
            f"Theme: {key} (conviction: {conviction})\n"
            f"  Thesis: {thesis}\n"
            f"  Already approved (in portfolio): {', '.join(approved) or 'none'}\n"
            f"  Do NOT add: {', '.join(do_not_add) or 'none'}"
        )
    return "\n\n".join(lines)


# ---------------------------------------------------------------------------
# Claude API call
# ---------------------------------------------------------------------------

def call_claude_exploration(
    convictions: dict,
    portfolio_tickers: set[str],
    exclusion_tickers: set[str],
    already_tracked: set[str],
) -> str:
    """
    Call Claude with the exploration prompt + all MCP tools.
    Returns the raw text response (parse_candidates extracts the JSON).
    """
    if not ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY not set")

    themes_detail   = _build_themes_detail(convictions)
    theme_keys      = list(convictions.get("themes", {}).keys())
    existing_csv    = ", ".join(sorted(portfolio_tickers)) or "none"
    exclusions_csv  = ", ".join(sorted(exclusion_tickers)) or "none"
    known_csv       = ", ".join(sorted(already_tracked))   or "none"

    specs      = mcp.exploration_specs()
    ctx        = mcp.search_context(max_searches=EXPLORATION_MAX_SEARCHES, servers=specs)
    template   = (PROMPTS_DIR / "exploration_candidates.mustache").read_text()
    prompt     = chevron.render(template, {
        "themes_detail":       themes_detail,
        "theme_keys_csv":      ", ".join(theme_keys),
        "existing_tickers_csv": existing_csv,
        "exclusions_csv":      exclusions_csv,
        "known_tickers_csv":   known_csv,
        "max_searches":        EXPLORATION_MAX_SEARCHES,
        "max_total_searches":  EXPLORATION_MAX_SEARCHES * max(len(theme_keys), 1),
        "max_av_calls":        EXPLORATION_MAX_AV_CALLS,
        "max_fmp_calls":       EXPLORATION_MAX_FMP_CALLS,
        **ctx,
    })

    text, _usage, _tool_log = run_with_timeout(
        lambda: mcp_loop.run_agentic_sync(
            prompt, specs,
            model=EXPLORATION_MODEL,
            max_tokens=EXPLORATION_MAX_TOKENS,
            effort=EXPLORATION_EFFORT,
            tool_timeout=ADVISOR_TOOL_TIMEOUT,
            max_rounds=ADVISOR_MAX_TOOL_ROUNDS,
            failure_limit=MCP_TOOL_FAILURE_LIMIT,
        ),
        EXPLORATION_TIMEOUT,
        label="Exploration Claude call",
    )
    return text or ""


# ---------------------------------------------------------------------------
# Graduation detection (called from robinhood.py after portfolio sync)
# ---------------------------------------------------------------------------

def check_graduations(portfolio_tickers: set[str]) -> None:
    """
    Mark on_radar / paper_trading candidates as graduated when they appear
    in the real Robinhood portfolio. Called by sync_portfolio_to_supabase().
    """
    from src.db import get_client
    from src.db import rows as db_rows
    from src.memory import update_exploration_status

    try:
        data = db_rows(
            get_client().table("exploration_candidates")
            .select("ticker, status")
            .in_("status", ["on_radar", "paper_trading"])
            .execute().data
        )
        for row in data:
            if row["ticker"] in portfolio_tickers:
                update_exploration_status(row["ticker"], "graduated")
                logger.info(f"Exploration candidate {row['ticker']} graduated → now in real portfolio")
    except Exception:
        logger.error("Graduation check failed", exc_info=True)


# ---------------------------------------------------------------------------
# Daily bridge: screen on_radar candidates during the regular agent run
# ---------------------------------------------------------------------------

def screen_and_paper_trade_candidates(run_date: str) -> None:
    """
    Called by agent.py at the end of each daily run.
    Screens on_radar candidates with quality + technical signal.
    Fires a paper trade and promotes to paper_trading if both pass.
    """
    from src import memory
    from src.data_layer import get_market_data_bulk
    from src.strategy import quality_filter, technical_signal

    candidates = memory.get_on_radar_candidates()
    if not candidates:
        return

    tickers = [c["ticker"] for c in candidates]
    logger.info(f"Screening {len(tickers)} on-radar candidate(s): {', '.join(tickers)}")

    market_data = get_market_data_bulk(tickers)

    for candidate in candidates:
        ticker = candidate["ticker"]
        mdata  = market_data.get(ticker, {})

        if "error" in mdata:
            logger.warning(f"{ticker}: no market data — skipping")
            continue

        quality_pass, quality_reasons, risk_flags = quality_filter(ticker, mdata)
        tech_pass, tech_reasons                   = technical_signal(mdata)

        price = mdata.get("current_price")

        if quality_pass and tech_pass and price:
            logger.info(f"{ticker}: BUY signal → paper trading")
            memory.log_paper_trade(
                ticker=ticker,
                price=price,
                run_date=run_date,
                signal_data={
                    "action": "buy",
                    "source": "exploration",
                    "source_theme": candidate.get("source_theme"),
                    "reasons": quality_reasons + tech_reasons,
                    "risk_flags": risk_flags,
                },
            )
            memory.update_exploration_status(ticker, "paper_trading")
        else:
            status_parts = []
            if not quality_pass:
                status_parts.append("quality fail")
            if not tech_pass:
                status_parts.append("no technical signal")
            logger.info(f"{ticker}: hold ({', '.join(status_parts)})")


# ---------------------------------------------------------------------------
# Weekly exploration entry point
# ---------------------------------------------------------------------------

def run() -> None:
    run_start = time.perf_counter()
    run_date  = datetime.now(timezone.utc).isoformat()
    logger.info("=" * 60)
    logger.info(f"ASTRA EXPLORATION — weekly discovery run  |  {run_date[:10]}")
    logger.info("=" * 60)

    from src import memory
    from src.data_layer import load_convictions

    convictions       = load_convictions()
    exclusion_tickers = {e["ticker"] for e in convictions.get("exclusions", [])}

    snapshot = memory.get_latest_portfolio_snapshot()
    portfolio_tickers: set[str] = set(
        (snapshot.get("positions") or {}).keys() if snapshot else set()
    )
    logger.info(f"Portfolio tickers to exclude: {len(portfolio_tickers)}")

    already_tracked = memory.get_all_exploration_tickers()
    logger.info(f"Already tracked candidates: {len(already_tracked)}")

    active_themes = {
        k: v for k, v in convictions.get("themes", {}).items()
        if v.get("conviction") in ("high", "very_high")
        and k not in EXPLORATION_EXCLUDED_THEMES
    }
    if not active_themes:
        logger.warning("No high/very_high conviction themes found — nothing to explore.")
        return
    logger.info(f"Active themes: {', '.join(active_themes)}")

    logger.info(f"Calling Claude for candidate discovery  (model={EXPLORATION_MODEL})")
    try:
        with timer("Exploration Claude call", logger):
            raw_text = call_claude_exploration(
                convictions, portfolio_tickers, exclusion_tickers, already_tracked
            )
        candidates = parse_candidates(raw_text)
    except Exception as exc:
        logger.exception(f"Claude exploration call failed: {exc}")
        return

    candidates = filter_known_tickers(
        candidates, portfolio_tickers, exclusion_tickers, already_tracked
    )
    candidates = [c for c in candidates if c.get("source_theme") not in EXPLORATION_EXCLUDED_THEMES]
    for c in candidates:
        c["ticker"] = c["ticker"].upper()

    logger.info(f"Discovered {len(candidates)} new candidate(s):")
    for c in candidates:
        ticker     = c["ticker"]
        theme      = c.get("source_theme", "?")
        conviction = c.get("claude_conviction", "?")
        rationale  = c.get("rationale", "")[:80]
        logger.info(f"  {ticker:<8}  theme={theme:<14}  conviction={conviction:<6}  {rationale}")

    for candidate in candidates:
        memory.upsert_exploration_candidate(candidate)
        logger.info(f"Saved: {candidate['ticker']}")

    total_elapsed = time.perf_counter() - run_start
    logger.info(f"Exploration complete — {len(candidates)} candidate(s) saved to on_radar.")
    logger.info(f"TIMING  {'Total exploration run':<40}  {total_elapsed:.2f}s")


if __name__ == "__main__":
    from src.logger import setup as _setup_logging
    _setup_logging()
    run()
