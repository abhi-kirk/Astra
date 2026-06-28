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
import re
from datetime import datetime, timezone
from pathlib import Path

import anthropic
from anthropic.types import TextBlock
from anthropic.types.beta import BetaTextBlock
import chevron

from src import memory
from src.config import (
    ANTHROPIC_API_KEY,
    REASONING_MAX_TOKENS,
    REASONING_MODEL,
    TAVILY_MAX_SEARCHES,
    TAVILY_MCP_URL,
)
from src.data_layer import get_market_data_bulk, get_portfolio, load_convictions
from src.strategy import screen_all_positions

PROMPTS_DIR = Path(__file__).parent.parent / "prompts"


def call_claude_reasoning(
    signals: list,
    portfolio: dict,
    market_data: dict,
    history_context: str,
    convictions: dict,
) -> str:
    """
    Call Claude API to synthesize mechanical signals into advisor narrative.
    Uses Tavily MCP for live news search when available.
    Returns a markdown-formatted analysis string.
    """
    if not ANTHROPIC_API_KEY:
        return "Claude API key not set — mechanical signals only."

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
        return (
            f"  {sig["ticker"]}: price=${price:.2f}, avg_cost=${avg:.2f}, "
            f"unrealized={gain:+.0f}%, shares={pos.get('shares', 0):.1f}\n"
            f"    Reasons: {reasons}"
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

    news_instruction = (
        f"You have access to a web search tool. Use it to look up recent news for the most "
        f"relevant tickers — prioritize BUY and WATCH signals, but also search any HOLD position "
        f"if it seems like something significant may have happened (earnings, big price move, "
        f"sector news). Limit yourself to a maximum of {TAVILY_MAX_SEARCHES} searches total. "
        f"Search for things like '[TICKER] news' or '[TICKER] earnings'. Use what you find to "
        f"make the note specific and grounded — mention actual events if you find them. "
        f"If results are thin or unhelpful, skip and write from the mechanical signals."
        if TAVILY_MCP_URL else
        "No search tool available this run — write from the mechanical signals only."
    )

    template = (PROMPTS_DIR / "advisor_note.mustache").read_text()
    prompt = chevron.render(template, {
        "themes_json":     json.dumps(themes),
        "history_context": history_context,
        "signal_text":     signal_text,
        "news_instruction": news_instruction,
        "date":            datetime.now().strftime("%B %-d, %Y"),
    })

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    if TAVILY_MCP_URL:
        message = client.beta.messages.create(
            model=REASONING_MODEL,
            max_tokens=REASONING_MAX_TOKENS,
            mcp_servers=[{
                "type": "url",
                "url": TAVILY_MCP_URL,
                "name": "tavily",
                "tool_configuration": {"enabled": True, "allowed_tools": ["tavily-search"]},
            }],
            messages=[{"role": "user", "content": prompt}],
            betas=["mcp-client-2025-04-04"],
        )
        text_blocks = [b.text for b in message.content if isinstance(b, BetaTextBlock)]
        raw_text = text_blocks[-1] if text_blocks else ""
        # Strip inline MCP tool call/response XML blocks
        clean = re.sub(r"<tool_call>.*?</tool_call>", "", raw_text, flags=re.DOTALL)
        clean = re.sub(r"<tool_response>.*?</tool_response>", "", clean, flags=re.DOTALL)
        # Drop any preamble before the first markdown heading
        lines = clean.split("\n")
        first_heading = next((i for i, l in enumerate(lines) if re.match(r"^#{1,3} ", l)), None)
        if first_heading is not None:
            clean = "\n".join(lines[first_heading:])
        return clean.strip() or "No advisor note generated."
    else:
        message = client.messages.create(
            model=REASONING_MODEL,
            max_tokens=REASONING_MAX_TOKENS,
            messages=[{"role": "user", "content": prompt}],
        )
        text = next((b.text for b in message.content if isinstance(b, TextBlock)), "")
        return text or "No advisor note generated."


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
    run_date = datetime.now(timezone.utc).isoformat()
    print(f"\n{'='*60}")
    print(f"ASTRA — {mode.upper()} run")
    print(f"Date: {run_date[:10]}")
    print(f"{'='*60}\n")

    convictions = load_convictions()
    excluded = {e["ticker"] for e in convictions.get("exclusions", [])}

    print("Loading portfolio...")
    portfolio = get_portfolio()
    portfolio = {t: v for t, v in portfolio.items() if t not in excluded}
    print(f"  {len(portfolio)} open positions (excluding {excluded})\n")

    print("Fetching market data...")
    market_data = get_market_data_bulk(list(portfolio.keys()))

    portfolio_for_screen = (
        {single_ticker: portfolio.get(single_ticker, {})} if single_ticker else portfolio
    )
    print()

    print("Running strategy screens...")
    signals = screen_all_positions(
        portfolio_for_screen, market_data, convictions,
        full_portfolio=portfolio if single_ticker else None,
    )

    history_context = memory.build_agent_context_summary()

    advisor_note = ""
    if use_ai and any(s["action"] in ("buy", "sell", "watch") for s in signals):
        print("\nCalling Claude for narrative reasoning...")
        advisor_note = call_claude_reasoning(
            signals, portfolio, market_data, history_context, convictions
        )

    # Print results
    print("\n" + "="*60)
    print("SIGNALS")
    print("="*60)

    action_groups: dict[str, list] = {}
    for sig in signals:
        action_groups.setdefault(sig["action"], []).append(sig)

    for action in ["buy", "sell", "watch", "blocked"]:
        group = action_groups.get(action, [])
        if not group:
            continue
        label = {"buy": "BUY SIGNALS", "sell": "SELL SIGNALS",
                 "watch": "WATCHLIST", "blocked": "BLOCKED"}[action]
        print(f"\n--- {label} ---")
        for sig in group:
            pos = portfolio.get(sig["ticker"], {})
            mdata = market_data.get(sig["ticker"], {})
            print(f"\n  {sig["ticker"]}")
            print(f"    Shares: {pos.get('shares', 0):.2f}  "
                  f"Avg cost: ${pos.get('avg_cost', 0):.2f}  "
                  f"Current: ${mdata.get('current_price', 0):.2f}")
            for r in sig["reasons"]:
                print(f"    ✓ {r}")
            for rf in sig["risk_flags"]:
                print(f"    ⚠ {rf}")
            if sig["suggested_position_pct"]:
                print(f"    → Suggested size: {sig["suggested_position_pct"]:.0%} of portfolio")

    if advisor_note:
        print(f"\n{'='*60}")
        print("ADVISOR NOTE")
        print("="*60)
        print(advisor_note)

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

    for sig in signals:
        pos = portfolio.get(sig["ticker"], {})
        mdata = market_data.get(sig["ticker"], {})
        price = mdata.get("current_price")
        if sig["action"] in ("buy", "sell", "watch"):
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

    print(f"Summary: {summary}")
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="ASTRA analysis run")
    parser.add_argument("--mode", choices=["simulation", "live"], default="simulation")
    parser.add_argument("--ticker", help="Analyze a single ticker only")
    parser.add_argument("--no-ai", action="store_true", help="Skip Claude API call")
    args = parser.parse_args()
    run(mode=args.mode, single_ticker=args.ticker, use_ai=not args.no_ai)
