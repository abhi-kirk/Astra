"""
Exploration engine: weekly discovery of new tickers within conviction themes.

Completely separate from the daily exploitation pipeline (agent.py).
Entry point: `python -m src.exploration` (run by exploration.yml, Fridays 9pm ET).

Bridges to the exploitation pipeline (kept minimal):
  - data_layer.load_convictions()        — shared source of truth
  - memory.get_latest_portfolio_snapshot() — to exclude current holdings
  - mcp.exploration_specs() / mcp_loop.run_structured_sync() — shared client-side tool loop
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
from typing import Any

import chevron

from src import mcp, mcp_loop
from src.logger import timer

logger = logging.getLogger(__name__)
from src import config
from src.timeout import run_with_timeout

PROMPTS_DIR = Path(__file__).parent.parent / "prompts"


# ---------------------------------------------------------------------------
# Pure helpers (testable without I/O)
# ---------------------------------------------------------------------------

EXPLORATION_EXCLUDED_THEMES: set[str] = set()  # all high/very_high themes are eligible


def eligible_themes(convictions: dict) -> dict[str, dict]:
    """High and very-high themes that are still an active hunt. Frozen themes are not."""
    out: dict[str, dict] = {}
    for key, theme in (convictions.get("themes") or {}).items():
        if not isinstance(theme, dict):
            continue
        if key in EXPLORATION_EXCLUDED_THEMES or theme.get("frozen"):
            continue
        if theme.get("conviction", "medium") not in ("high", "very_high"):
            continue
        out[key] = theme
    return out


def build_theme_queries(convictions: dict) -> dict[str, str]:
    """Return {theme_key: tavily_search_query} for themes we are still willing to hunt."""
    queries = {}
    for key in eligible_themes(convictions):
        label = key.replace("_", " ")
        queries[key] = f"best emerging {label} stocks not widely held 2026 2027 under 10 billion market cap"
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


_PER_THEME_CAP = 3
_TOTAL_CAP = 8
_CONVICTION_LEVELS = {"high", "medium", "low"}

# The forced-emit tool. Its schema is the output contract, same pattern as contextual emit_proposals.
_EMIT_CANDIDATES: dict[str, Any] = {
    "name": "emit_candidates",
    "description": (
        "Emit the final discovery list. Each candidate passed every hard filter. "
        "An empty list is valid and is better than a weak name."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "candidates": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "ticker": {"type": "string"},
                        "source_theme": {"type": "string"},
                        "rationale": {"type": "string", "description": "2-3 sentences on why it fits the theme"},
                        "quality_summary": {"type": "string"},
                        "analyst_summary": {"type": "string"},
                        "conviction": {"type": "string", "enum": ["high", "medium", "low"]},
                        "conviction_rationale": {"type": "string"},
                    },
                    "required": [
                        "ticker", "source_theme", "rationale", "quality_summary",
                        "analyst_summary", "conviction", "conviction_rationale",
                    ],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["candidates"],
        "additionalProperties": False,
    },
}


def candidates_from_payload(payload: dict | None, allowed_themes: set[str]) -> list[dict]:
    """Turn the emit_candidates object into rows the radar can store.

    Drops Tesla, unknown themes, empty rationales, and bad conviction labels.
    Caps at 3 per theme and 8 total. `conviction` is stored as `claude_conviction`
    because that is the column the radar and the dashboard already read.
    """
    rows = payload.get("candidates") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return []
    per_theme: dict[str, int] = {}
    out: list[dict] = []
    for row in rows:
        if len(out) >= _TOTAL_CAP:
            break
        if not isinstance(row, dict):
            continue
        ticker = str(row.get("ticker") or "").strip().upper().lstrip("$")
        theme = str(row.get("source_theme") or "").strip()
        rationale = " ".join(str(row.get("rationale") or "").split())
        conviction = str(row.get("conviction") or row.get("claude_conviction") or "").strip().lower()
        if (
            not ticker
            or ticker == "TSLA"
            or theme not in allowed_themes
            or not rationale
            or conviction not in _CONVICTION_LEVELS
            or per_theme.get(theme, 0) >= _PER_THEME_CAP
        ):
            continue
        per_theme[theme] = per_theme.get(theme, 0) + 1
        out.append({
            "ticker": ticker,
            "source_theme": theme,
            "rationale": rationale[:800],
            "quality_summary": " ".join(str(row.get("quality_summary") or "").split())[:500],
            "analyst_summary": " ".join(str(row.get("analyst_summary") or "").split())[:500],
            "claude_conviction": conviction,
            "conviction_rationale": " ".join(str(row.get("conviction_rationale") or "").split())[:400],
        })
    return out


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

def _named_tickers(theme: dict) -> list[str]:
    named: list[str] = []
    for bucket in ("preferred", "approved", "hold_only"):
        for ticker in theme.get(bucket) or []:
            text = str(ticker).upper()
            if text and text not in named:
                named.append(text)
    return named


def _build_themes_detail(convictions: dict) -> str:
    """Format the themes we will actually hunt. Frozen and low-conviction themes are omitted."""
    lines = []
    for key, theme in eligible_themes(convictions).items():
        conviction = theme.get("conviction", "medium")
        thesis = theme.get("thesis", "No thesis documented.")
        do_not_add = [str(t).upper() for t in (theme.get("do_not_add") or [])]
        named = ", ".join(_named_tickers(theme)) or "none"
        blocked = ", ".join(do_not_add) or "none"
        lines.append(
            f"Theme: {key} (conviction: {conviction})\n"
            f"  Thesis: {thesis}\n"
            f"  Already named, do not recommend: {named}\n"
            f"  Do not add: {blocked}"
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
) -> list[dict]:
    """Hunt new names inside the active themes and return validated candidate rows.

    The model must finish by calling emit_candidates. Code drops anything outside
    the active themes, over the per-theme cap, or missing a rationale.
    """
    if not config.services.grok_api_key:
        raise RuntimeError("GROK_API_KEY not set")

    themes = eligible_themes(convictions)
    theme_keys = list(themes)
    existing_csv = ", ".join(sorted(portfolio_tickers)) or "none"
    exclusions_csv = ", ".join(sorted(exclusion_tickers)) or "none"
    known_csv = ", ".join(sorted(already_tracked)) or "none"

    specs = mcp.exploration_specs()
    ctx = mcp.search_context(max_searches=config.exploration.max_searches, servers=specs)
    template = (PROMPTS_DIR / "exploration_candidates.mustache").read_text()
    prompt = chevron.render(template, {
        "themes_detail": _build_themes_detail(convictions),
        "theme_keys_csv": ", ".join(theme_keys),
        "existing_tickers_csv": existing_csv,
        "exclusions_csv": exclusions_csv,
        "known_tickers_csv": known_csv,
        "max_searches": config.exploration.max_searches,
        "max_total_searches": config.exploration.max_searches * max(len(theme_keys), 1),
        "max_av_calls": config.exploration.max_av_calls,
        "max_fmp_calls": config.exploration.max_fmp_calls,
        **ctx,
    })

    payload, _usage, _tool_log = run_with_timeout(
        lambda: mcp_loop.run_structured_sync(
            prompt, specs, _EMIT_CANDIDATES,
            model=config.exploration.model,
            max_tokens=config.exploration.max_tokens,
            effort=config.exploration.effort,
            tool_timeout=config.tool_loop.advisor_tool_timeout,
            max_rounds=config.tool_loop.advisor_max_tool_rounds,
            failure_limit=config.tool_loop.mcp_tool_failure_limit,
        ),
        config.timeouts.exploration,
        label="Exploration Grok call",
    )
    return candidates_from_payload(payload if isinstance(payload, dict) else {}, set(theme_keys))


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

def screen_candidates() -> tuple[list[dict], dict]:
    """
    Screen on_radar exploration candidates against the quality + technical filter and
    return their BUY signals as standard-shaped signal dicts, plus the market data fetched
    for them. Called by agent.py *before* the advisor note so exploration decisions flow
    through the single `signals` list — the advisor note, decision log, paper trades, and
    the Autotrader mirror all read one source of truth.

    Returns (exploration_buy_signals, market_data). Pure w.r.t. the decision store: it does
    NOT log paper trades or promote candidate status — the unified agent.py buy loop does
    both, so exploration buys are logged identically to conviction buys.
    """
    from src import memory
    from src.data_layer import get_market_data_bulk
    from src.strategy import quality_filter, technical_signal

    candidates = memory.get_on_radar_candidates()
    if not candidates:
        return [], {}

    tickers = [c["ticker"] for c in candidates]
    logger.info(f"Screening {len(tickers)} on-radar candidate(s): {', '.join(tickers)}")

    market_data = get_market_data_bulk(tickers)
    signals: list[dict] = []

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
            logger.info(f"{ticker}: BUY signal → exploration entry")
            signals.append({
                "ticker": ticker,
                "action": "buy",
                "reasons": quality_reasons + tech_reasons,
                "risk_flags": risk_flags,
                "suggested_position_pct": None,  # None → log_paper_trade uses the default starter size
                "source": "exploration",
                "source_theme": candidate.get("source_theme"),
            })
        else:
            status_parts = []
            if not quality_pass:
                status_parts.append("quality fail")
            if not tech_pass:
                status_parts.append("no technical signal")
            logger.info(f"{ticker}: hold ({', '.join(status_parts)})")

    return signals, market_data


# ---------------------------------------------------------------------------
# Weekly exploration entry point
# ---------------------------------------------------------------------------

def run() -> None:
    run_start = time.perf_counter()
    run_date  = datetime.now(timezone.utc).isoformat()
    logger.info("=" * 60)
    logger.info(f"ASTRA EXPLORATION — weekly discovery run  |  {run_date[:10]}")
    logger.info("=" * 60)

    from src import memory, notify
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

    active_themes = eligible_themes(convictions)
    if not active_themes:
        logger.warning("No active conviction theme to explore.")
        notify.notify_exploration([], themes_explored=0, already_tracked=len(already_tracked))
        return
    logger.info(f"Active themes: {', '.join(active_themes)}")

    logger.info(f"Calling Grok for candidate discovery  (model={config.exploration.model})")
    try:
        with timer("Exploration Grok call", logger):
            candidates = call_claude_exploration(
                convictions, portfolio_tickers, exclusion_tickers, already_tracked
            )
    except Exception as exc:
        logger.exception(f"Grok exploration call failed: {exc}")
        notify.send(notify.markdownify(
            f"🔭 **ASTRA Exploration** — discovery run failed: `{str(exc)[:200]}`. "
            "No candidates screened this week."
        ))
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

    notify.notify_exploration(
        candidates,
        themes_explored=len(active_themes),
        already_tracked=len(already_tracked),
    )

    total_elapsed = time.perf_counter() - run_start
    logger.info(f"Exploration complete — {len(candidates)} candidate(s) saved to on_radar.")
    logger.info(f"TIMING  {'Total exploration run':<40}  {total_elapsed:.2f}s")


if __name__ == "__main__":
    from src.logger import setup as _setup_logging
    from src.settings import load_and_apply as _apply_settings
    _setup_logging()
    _apply_settings()   # dashboard-saved tunables layer over env/code. Fail-open.
    run()
