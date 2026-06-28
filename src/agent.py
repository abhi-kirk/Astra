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
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import anthropic
from dotenv import load_dotenv

from src.data_layer import get_portfolio, get_market_data_bulk, load_convictions
from src.strategy import screen_all_positions
from src import memory

load_dotenv(Path(__file__).parent.parent / ".env")

REASONING_MODEL = "claude-sonnet-4-6"


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
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        return "Claude API key not set — mechanical signals only."

    # Build a compact signal summary to keep tokens low
    action_groups: dict[str, list] = {}
    for sig in signals:
        action_groups.setdefault(sig.action, []).append(sig)

    def fmt_sig(sig) -> str:
        pos = portfolio.get(sig.ticker, {})
        mdata = market_data.get(sig.ticker, {})
        price = mdata.get("current_price", 0)
        avg = pos.get("avg_cost", 0)
        gain = ((price - avg) / avg * 100) if avg else 0
        reasons = "; ".join(sig.reasons[:3])
        return (
            f"  {sig.ticker}: price=${price:.2f}, avg_cost=${avg:.2f}, "
            f"unrealized={gain:+.0f}%, shares={pos.get('shares',0):.1f}\n"
            f"    Reasons: {reasons}"
        )

    signal_text = ""
    for action in ["buy", "review", "watch"]:
        group = action_groups.get(action, [])
        if group:
            label = {"buy": "BUY SIGNALS", "review": "PROFIT-TAKE REVIEWS", "watch": "WATCHLIST"}[action]
            signal_text += f"\n{label}:\n" + "\n".join(fmt_sig(s) for s in group)

    blocked = action_groups.get("blocked", [])
    if blocked:
        signal_text += f"\nBLOCKED ({len(blocked)} positions): " + ", ".join(s.ticker for s in blocked)

    # Theme conviction summary
    themes = {k: v.get("conviction") for k, v in convictions.get("themes", {}).items()}

    tavily_mcp_url = os.environ.get("TAVILY_MCP_URL", "").strip()
    news_instruction = (
        "You have access to a web search tool. Use it to look up recent news for the most "
        "relevant tickers — prioritize BUY and WATCH signals, but also search any HOLD position "
        "if it seems like something significant may have happened (earnings, big price move, "
        "sector news). Limit yourself to a maximum of 5 searches total. Search for things like "
        "'[TICKER] news' or '[TICKER] earnings'. Use what you find to make the note specific "
        "and grounded — mention actual events if you find them. "
        "If results are thin or unhelpful, skip and write from the mechanical signals."
        if tavily_mcp_url else
        "No search tool available this run — write from the mechanical signals only."
    )

    prompt = f"""You are ASTRA, a personal trading assistant for a non-finance-professional investor.

INVESTOR PROFILE:
- Software engineer at Tesla. Understands tech deeply, but does not have a finance background and does not trade daily.
- Invests based on personal conviction in themes he follows (space, tech, EV) — not short-term trading signals.
- Medium-term horizon: holds positions for weeks to months, sometimes longer if the thesis is strong.
- TSLA excluded (employment equity + blackout restrictions).
- Conviction themes: {json.dumps(themes)}
- Key rule: no buying more of a stock that is already more than 35% below what he paid, if he has bought it more than 3 times already.

PRIOR DECISION HISTORY (for continuity):
{history_context}

TODAY'S MECHANICAL SIGNALS:
{signal_text}

NEWS RESEARCH INSTRUCTIONS:
{news_instruction}

Write a plain-English daily note (200-300 words) as if you're a knowledgeable friend explaining what's happening in his portfolio today. Avoid finance jargon — no terms like "D/E ratio", "RSI", "basis points", "technicals", "fundamentals", "unrealized P&L". Instead say things like "the stock is down 35% from what you paid" or "the company is growing revenue fast" or "this one has a lot of debt". Be conversational but specific — name actual tickers, prices, and real events where you found them. Structure the note as:

1. PRIORITY ACTIONS: What he should actually consider doing today/this week, and the simple reason why.
2. RISK FLAGS: Anything that looks concerning in plain terms.
3. CONVICTION CHECK: Are the signals today in line with his long-term bets, or noise?
4. ONE THING TO WATCH: One forward-looking thing to keep an eye on.

No disclaimers. No "as always, consult a financial advisor." Just honest, clear, friend-level advice."""

    client = anthropic.Anthropic(api_key=api_key)

    tavily_mcp_url = os.environ.get("TAVILY_MCP_URL", "").strip()
    if tavily_mcp_url:
        message = client.beta.messages.create(
            model=REASONING_MODEL,
            max_tokens=3000,
            mcp_servers=[{
                "type": "url",
                "url": tavily_mcp_url,
                "name": "tavily",
                "tool_configuration": {"enabled": True, "allowed_tools": ["tavily-search"]},
            }],
            messages=[{"role": "user", "content": prompt}],
            betas=["mcp-client-2025-04-04"],
        )
        # Extract text and strip inline MCP tool call/response XML
        text_blocks = [block.text for block in message.content if hasattr(block, "text")]
        raw_text = text_blocks[-1] if text_blocks else ""
        clean = re.sub(r"<tool_call>.*?</tool_call>", "", raw_text, flags=re.DOTALL)
        clean = re.sub(r"<tool_response>.*?</tool_response>", "", clean, flags=re.DOTALL)
        # Drop any preamble before the first markdown heading
        heading_match = re.search(r"^#{1,3} ", clean, flags=re.MULTILINE)
        if heading_match:
            clean = clean[heading_match.start():]
        return clean.strip() or "No advisor note generated."
    else:
        message = client.messages.create(
            model=REASONING_MODEL,
            max_tokens=800,
            messages=[{"role": "user", "content": prompt}],
        )
        return message.content[0].text


def build_public_output(output: dict) -> dict:
    """
    Scrubbed version of the weekly output safe for public display.
    Strips: advisor note, avg_cost references in reasons, suggested position sizes,
    portfolio % / dollar amounts from blocked reasons, history context.
    """
    public_signals = []
    for s in output.get("signals", []):
        action = s.get("action", "")
        reasons = s.get("reasons", [])

        if action == "review":
            # Review reasons always embed avg_cost — replace with generic
            public_reasons = ["Position has appreciated significantly — profit-take review triggered."]
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
        # advisor_note excluded — references personal financial data
        # history_context excluded — contains personal decision history
    }


def run(mode: str = "simulation", single_ticker: str | None = None, use_ai: bool = True):
    run_date = datetime.now(timezone.utc).isoformat()
    print(f"\n{'='*60}")
    print(f"ASTRA — {mode.upper()} run")
    print(f"Date: {run_date[:10]}")
    print(f"{'='*60}\n")

    # 1. Load convictions
    convictions = load_convictions()
    excluded = {e["ticker"] for e in convictions.get("exclusions", [])}

    # 2. Load full portfolio
    print("Loading portfolio...")
    portfolio = get_portfolio()
    portfolio = {t: v for t, v in portfolio.items() if t not in excluded}
    print(f"  {len(portfolio)} open positions (excluding {excluded})\n")

    # 3. Fetch market data
    print("Fetching market data...")
    market_data = get_market_data_bulk(list(portfolio.keys()))

    portfolio_for_screen = (
        {single_ticker: portfolio.get(single_ticker, {})} if single_ticker else portfolio
    )
    print()

    # 4. Run strategy screens
    print("Running strategy screens...")
    signals = screen_all_positions(
        portfolio_for_screen, market_data, convictions,
        full_portfolio=portfolio if single_ticker else None,
    )

    # 5. Load memory context
    history_context = memory.build_agent_context_summary()

    # 6. Claude reasoning
    advisor_note = ""
    if use_ai and any(s.action in ("buy", "review", "watch") for s in signals):
        print("\nCalling Claude for narrative reasoning...")
        advisor_note = call_claude_reasoning(
            signals, portfolio, market_data, history_context, convictions
        )

    # 7. Print results
    print("\n" + "="*60)
    print("SIGNALS")
    print("="*60)

    action_groups: dict[str, list] = {}
    for sig in signals:
        action_groups.setdefault(sig.action, []).append(sig)

    for action in ["buy", "review", "watch", "blocked"]:
        group = action_groups.get(action, [])
        if not group:
            continue
        label = {"buy": "BUY SIGNALS", "review": "PROFIT-TAKE REVIEW",
                 "watch": "WATCHLIST", "blocked": "BLOCKED"}[action]
        print(f"\n--- {label} ---")
        for sig in group:
            pos = portfolio.get(sig.ticker, {})
            mdata = market_data.get(sig.ticker, {})
            print(f"\n  {sig.ticker}")
            print(f"    Shares: {pos.get('shares', 0):.2f}  "
                  f"Avg cost: ${pos.get('avg_cost', 0):.2f}  "
                  f"Current: ${mdata.get('current_price', 0):.2f}")
            for r in sig.reasons:
                print(f"    ✓ {r}")
            for rf in sig.risk_flags:
                print(f"    ⚠ {rf}")
            if sig.suggested_position_pct:
                print(f"    → Suggested size: {sig.suggested_position_pct:.0%} of portfolio")

    if advisor_note:
        print(f"\n{'='*60}")
        print("ADVISOR NOTE")
        print("="*60)
        print(advisor_note)

    # 8. Log decisions + paper trades to Supabase
    for sig in signals:
        pos = portfolio.get(sig.ticker, {})
        mdata = market_data.get(sig.ticker, {})
        price = mdata.get("current_price")
        if sig.action in ("buy", "review", "watch"):
            memory.log_decision(
                ticker=sig.ticker,
                action=sig.action,
                reasoning="; ".join(sig.reasons),
                signal_data=sig.to_dict(),
                price_at_decision=price,
                shares_held=pos.get("shares"),
                avg_cost=pos.get("avg_cost"),
                executed=(mode == "live"),
                run_date=run_date,
            )
        if sig.action == "buy" and price:
            memory.log_paper_trade(
                ticker=sig.ticker,
                price=price,
                run_date=run_date,
                signal_data=sig.to_dict(),
            )

    # 9. Write analysis JSON
    buy_tickers = [s.ticker for s in signals if s.action == "buy"]
    summary = (
        f"{len(buy_tickers)} buy signal(s): {', '.join(buy_tickers) or 'none'}. "
        f"{len(action_groups.get('review', []))} profit-take review(s). "
        f"{len(action_groups.get('watch', []))} on watchlist."
    )

    output = {
        "run_date": run_date,
        "mode": mode,
        "num_positions_screened": len(portfolio),
        "summary": summary,
        "advisor_note": advisor_note,
        "signals": [s.to_dict() for s in signals],
        "market_data_snapshot": {
            t: {k: v for k, v in d.items() if k != "fetched_at"}
            for t, d in market_data.items()
            if "error" not in d
        },
        "history_context": history_context,
    }

    memory.log_run_summary(
        mode=mode, signals=[s.to_dict() for s in signals],
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
