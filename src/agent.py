"""
Agent orchestrator: runs the full analysis pipeline and produces output.

In simulation mode (Phase 1): fetches data, screens positions, calls Claude
for narrative reasoning, logs decisions, writes analysis JSON.

Usage:
    python -m src.agent                    # simulation run
    python -m src.agent --mode live        # Phase 2: includes trade approval flow
    python -m src.agent --ticker RKLB      # analyze a single ticker
    python -m src.agent --no-ai            # skip Claude API call (mechanical only)
"""

import argparse
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import chevron

from src import mcp, mcp_loop, memory
from src.config import (
    ADVISOR_EFFORT,
    ADVISOR_MAX_TOOL_ROUNDS,
    ADVISOR_TIMEOUT,
    ADVISOR_TOOL_TIMEOUT,
    ANTHROPIC_API_KEY,
    MCP_TOOL_FAILURE_LIMIT,
    REASONING_MAX_TOKENS,
    REASONING_MODEL,
)
from src.data_layer import get_market_data_bulk, get_portfolio, load_convictions
from src.strategy import screen_all_positions
from src.timeout import run_with_timeout

logger = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).parent.parent / "prompts"


def call_claude_reasoning(
    signals: list,
    portfolio: dict,
    market_data: dict,
    history_context: str,
    convictions: dict,
) -> tuple[str, Any, list[dict[str, Any]]]:
    """
    Call Claude to synthesize mechanical signals into an advisor narrative.

    Runs the tool loop client-side (src.mcp_loop) so each MCP tool call is individually
    timed out and observable. Returns (advisor_note, token_usage, tool_log).
    """
    if not ANTHROPIC_API_KEY:
        return ("Claude API key not set — mechanical signals only.", None, [])

    action_groups: dict[str, list] = {}
    for sig in signals:
        action_groups.setdefault(sig["action"], []).append(sig)

    def fmt_sig(sig) -> str:
        pos = portfolio.get(sig["ticker"], {})
        mdata = market_data.get(sig["ticker"], {})
        price = mdata.get("current_price", 0)
        avg = pos.get("avg_cost", 0)
        gain = ((price - avg) / avg * 100) if avg else 0
        reasons = "; ".join(sig["reasons"][:3])
        intent = sig.get("intent", "opportunistic")
        catalyst = sig.get("original_catalyst")
        intent_note = f"\n    Intent: {intent}" + (f" — original catalyst: {catalyst}" if catalyst else "")
        return (
            f"  {sig['ticker']}: price=${price:.2f}, avg_cost=${avg:.2f}, "
            f"unrealized={gain:+.0f}%, shares={pos.get('shares', 0):.1f}\n"
            f"    Reasons: {reasons}{intent_note}"
        )

    signal_text = ""
    for action in ["buy", "sell", "watch"]:
        group = action_groups.get(action, [])
        if group:
            label = {"buy": "BUY SIGNALS", "sell": "SELL SIGNALS", "watch": "WATCHLIST"}[action]
            signal_text += f"\n{label}:\n" + "\n".join(fmt_sig(s) for s in group)

    blocked = action_groups.get("blocked", [])
    if blocked:
        signal_text += f"\nBLOCKED ({len(blocked)} positions): " + ", ".join(s["ticker"] for s in blocked)

    themes = {k: v.get("conviction") for k, v in convictions.get("themes", {}).items()}

    specs = mcp.advisor_specs()
    template = (PROMPTS_DIR / "advisor_note.mustache").read_text()
    prompt = chevron.render(template, {
        "themes_json":     json.dumps(themes),
        "history_context": history_context,
        "signal_text":     signal_text,
        "date":            datetime.now().strftime("%B %-d, %Y"),
        **mcp.search_context(servers=specs),
    })

    text, usage, tool_log = run_with_timeout(
        lambda: mcp_loop.run_agentic_sync(
            prompt, specs,
            model=REASONING_MODEL,
            max_tokens=REASONING_MAX_TOKENS,
            effort=ADVISOR_EFFORT,
            tool_timeout=ADVISOR_TOOL_TIMEOUT,
            max_rounds=ADVISOR_MAX_TOOL_ROUNDS,
            failure_limit=MCP_TOOL_FAILURE_LIMIT,
        ),
        ADVISOR_TIMEOUT,
        label="Advisor Claude call",
    )
    if not text:
        return ("No advisor note generated.", usage, tool_log)
    return (text, usage, tool_log)


def build_public_output(output: dict) -> dict:
    """
    Scrubbed version of the run output safe for public display.
    Strips: advisor note, avg_cost references in reasons, suggested position sizes,
    portfolio %/dollar amounts from blocked reasons, history context.
    """
    public_signals = []
    for s in output.get("signals", []):
        action = s.get("action", "")
        reasons = s.get("reasons", [])

        if action == "sell":
            public_reasons = ["Position has appreciated significantly — sell signal triggered."]
        elif action == "blocked":
            public_reasons = []
            for r in reasons:
                if "POSITION LIMIT" in r:
                    public_reasons.append("BLOCKED: Single-position size limit reached.")
                elif "AVERAGING DOWN CAP" in r:
                    public_reasons.append("BLOCKED: Averaging-down rule triggered — requires thesis re-confirmation.")
                elif "THEME LIMIT" in r:
                    public_reasons.append("BLOCKED: Theme concentration limit reached (max 15% per theme).")
                else:
                    public_reasons.append(r)
        else:
            public_reasons = reasons

        public_sig = {k: v for k, v in s.items() if k != "suggested_position_pct"}
        public_sig["reasons"] = public_reasons
        public_signals.append(public_sig)

    return {
        "run_date": output["run_date"],
        "mode": output["mode"],
        "num_positions_screened": output["num_positions_screened"],
        "summary": output["summary"],
        "signals": public_signals,
        "market_data_snapshot": output.get("market_data_snapshot", {}),
    }


def run(mode: str = "simulation", single_ticker: str | None = None, use_ai: bool = True):
    """Public entry point — wraps _run with observability (metrics + health)."""
    from src.observability import RunObserver
    run_date = datetime.now(timezone.utc).isoformat()
    obs = RunObserver(run_date, mode)
    try:
        return _run(obs, mode, single_ticker, use_ai)
    except Exception as e:
        obs.fail(e)
        raise
    finally:
        obs.flush()


def _run(obs, mode: str, single_ticker: str | None, use_ai: bool):
    run_start = time.perf_counter()
    run_date = obs.run_date
    logger.info("=" * 60)
    logger.info(f"ASTRA — {mode.upper()} run  |  {run_date[:10]}")
    logger.info("=" * 60)

    convictions = load_convictions()
    excluded = {e["ticker"] for e in convictions.get("exclusions", [])}

    from src.robinhood import sync_portfolio_to_supabase
    with obs.phase("robinhood_sync"):
        try:
            sync_portfolio_to_supabase()
            obs.record_service("robinhood", ok=True)
        except Exception as e:
            obs.record_service("robinhood", ok=False, detail=str(e))
            raise

    portfolio = get_portfolio()
    portfolio = {t: v for t, v in portfolio.items() if t not in excluded}
    logger.info(f"Portfolio loaded: {len(portfolio)} open positions (excluded: {sorted(excluded)})")

    with obs.phase("market_data"):
        market_data = get_market_data_bulk(list(portfolio.keys()))

    errors = [t for t, d in market_data.items() if "error" in d]
    if errors:
        logger.warning(f"Market data missing for {len(errors)} ticker(s): {errors}")

    portfolio_for_screen = (
        {single_ticker: portfolio.get(single_ticker, {})} if single_ticker else portfolio
    )

    with obs.phase("screening"):
        signals = screen_all_positions(
            portfolio_for_screen, market_data, convictions,
            full_portfolio=portfolio if single_ticker else None,
        )

    history_context = memory.build_agent_context_summary()

    advisor_note = ""
    advisor_tool_log: list[dict[str, Any]] = []
    if use_ai and any(s["action"] in ("buy", "sell", "watch") for s in signals):
        logger.info(f"Calling Claude for narrative reasoning  (model={REASONING_MODEL})")
        try:
            with obs.phase("advisor"):
                advisor_note, advisor_usage, advisor_tool_log = call_claude_reasoning(
                    signals, portfolio, market_data, history_context, convictions
                )
            obs.record_advisor(REASONING_MODEL, advisor_usage)
            obs.record_service("anthropic", ok=bool(advisor_note))
        except TimeoutError:
            logger.warning(f"Advisor Claude call timed out after {ADVISOR_TIMEOUT}s — pipeline continues without advisor note")
            obs.record_service("anthropic", ok=False, detail="timeout")
        except Exception as e:
            logger.error("Advisor Claude call failed — pipeline continues without advisor note", exc_info=True)
            obs.record_service("anthropic", ok=False, detail=str(e))

    # Log signals
    action_groups: dict[str, list] = {}
    for sig in signals:
        action_groups.setdefault(sig["action"], []).append(sig)

    logger.info("-" * 60)
    logger.info("SIGNALS")
    logger.info("-" * 60)

    for action in ["buy", "sell", "watch", "blocked"]:
        group = action_groups.get(action, [])
        if not group:
            continue
        label = {"buy": "BUY SIGNALS", "sell": "SELL SIGNALS",
                 "watch": "WATCHLIST", "blocked": "BLOCKED"}[action]
        logger.info(f"--- {label} ---")
        for sig in group:
            pos   = portfolio.get(sig["ticker"], {})
            mdata = market_data.get(sig["ticker"], {})
            ticker = sig["ticker"]
            shares = pos.get("shares", 0)
            avg    = pos.get("avg_cost", 0)
            price  = mdata.get("current_price", 0)
            logger.info(f"  {ticker}  shares={shares:.2f}  avg=${avg:.2f}  price=${price:.2f}")
            for r in sig["reasons"]:
                logger.info(f"    ✓ {r}")
            for rf in sig["risk_flags"]:
                logger.warning(f"    ⚠ {rf}")
            if sig["suggested_position_pct"]:
                pct = sig["suggested_position_pct"]
                logger.info(f"    → Suggested size: {pct:.0%}")

    if advisor_note:
        logger.info("-" * 60)
        logger.info("ADVISOR NOTE")
        logger.info("-" * 60)
        for line in advisor_note.splitlines():
            logger.info(line)

    # Log decisions + manage paper trades
    signals_by_ticker = {s["ticker"]: s for s in signals}

    # Close paper trades whose signal is no longer active
    open_paper_trades = memory.get_open_paper_trades()
    for pt in open_paper_trades:
        ticker = pt["ticker"]
        price = market_data.get(ticker, {}).get("current_price")
        if not price:
            continue
        sig = signals_by_ticker.get(ticker)
        if sig is None and not single_ticker:
            memory.close_paper_trade(ticker, price, run_date, "signal_inactive")
        elif sig is not None and sig["action"] == "blocked":
            memory.close_paper_trade(ticker, price, run_date, "blocked")
        elif sig is not None and sig["action"] == "sell":
            memory.close_paper_trade(ticker, price, run_date, "profit_take")
        # buy / watch → keep open

    # Dedup: a signal that persists unchanged (e.g. a profit-take that stays up >60%)
    # re-logs weekly instead of every run — see memory.should_log_decision.
    loggable = [s["ticker"] for s in signals if s["action"] in ("buy", "sell", "watch")]
    latest_decisions = memory.get_latest_decisions_for(loggable)

    for sig in signals:
        pos = portfolio.get(sig["ticker"], {})
        mdata = market_data.get(sig["ticker"], {})
        price = mdata.get("current_price")
        if sig["action"] in ("buy", "sell", "watch") and memory.should_log_decision(
            latest_decisions.get(sig["ticker"]), sig["action"], run_date
        ):
            memory.log_decision(
                ticker=sig["ticker"],
                action=sig["action"],
                reasoning="; ".join(sig["reasons"]),
                signal_data=sig,
                price_at_decision=price,
                shares_held=pos.get("shares"),
                avg_cost=pos.get("avg_cost"),
                executed=(mode == "live"),
                run_date=run_date,
            )
        if sig["action"] == "buy" and price:
            memory.log_paper_trade(
                ticker=sig["ticker"],
                price=price,
                run_date=run_date,
                signal_data=sig,
                suggested_pct=sig["suggested_position_pct"],
            )

    buy_tickers  = [s["ticker"] for s in signals if s["action"] == "buy"]
    sell_tickers = [s["ticker"] for s in signals if s["action"] == "sell"]
    summary = (
        f"{len(buy_tickers)} buy signal(s): {', '.join(buy_tickers) or 'none'}. "
        f"{len(sell_tickers)} sell signal(s): {', '.join(sell_tickers) or 'none'}. "
        f"{len(action_groups.get('watch', []))} on watchlist."
    )

    output = {
        "run_date": run_date,
        "mode": mode,
        "num_positions_screened": len(portfolio),
        "summary": summary,
        "advisor_note": advisor_note,
        "advisor_tool_log": advisor_tool_log,
        "signals": signals,
        "market_data_snapshot": {
            t: {k: v for k, v in d.items() if k != "fetched_at"}
            for t, d in market_data.items()
            if "error" not in d
        },
        "history_context": history_context,
    }

    memory.log_run_summary(
        mode=mode, signals=signals,
        summary=summary, raw_output=output,
        public_output=build_public_output(output),
        run_date=run_date,
    )

    # Push the tally + advisor note to Telegram (no-op if unconfigured; never fatal)
    from src import notify
    notify.notify_run(
        buy_tickers=buy_tickers,
        sell_tickers=sell_tickers,
        advisor_note=advisor_note,
        mode=mode,
    )

    # Screen on_radar exploration candidates against quality + technical signal
    from src.exploration import screen_and_paper_trade_candidates
    screen_and_paper_trade_candidates(run_date)

    # Outcome tracking: forward returns + trade journal detection
    from src.outcomes import backfill_outcomes, detect_portfolio_changes
    try:
        detect_portfolio_changes()
    except Exception:
        logger.error("Trade journal detection failed — pipeline continues", exc_info=True)
    try:
        backfill_outcomes()
    except Exception:
        logger.error("Outcome backfill failed — pipeline continues", exc_info=True)

    # Observability: capture run metrics + active infra/MCP health probes
    from src.observability import probe_endpoints
    obs.record(
        positions_screened=len(portfolio),
        num_signals=len(signals),
        buy_count=len(buy_tickers),
        sell_count=len(sell_tickers),
        watch_count=len(action_groups.get("watch", [])),
        market_data_errors=len(errors),
    )
    obs.record_service(
        "yfinance",
        ok=(len(errors) < max(1, len(portfolio)) * 0.5),
        detail=f"{len(errors)}/{len(portfolio)} tickers errored",
    )
    probe_endpoints(obs)

    total_elapsed = time.perf_counter() - run_start
    logger.info("=" * 60)
    logger.info(f"Run complete in {total_elapsed:.1f}s  |  {summary}")
    logger.info(f"TIMING  {'Total agent run':<40}  {total_elapsed:.2f}s")
    return output


if __name__ == "__main__":
    from src.logger import setup as _setup_logging
    _setup_logging()

    parser = argparse.ArgumentParser(description="ASTRA analysis run")
    parser.add_argument("--mode", choices=["simulation", "live"], default="simulation")
    parser.add_argument("--ticker", help="Analyze a single ticker only")
    parser.add_argument("--no-ai", action="store_true", help="Skip Claude API call")
    args = parser.parse_args()
    run(mode=args.mode, single_ticker=args.ticker, use_ai=not args.no_ai)
