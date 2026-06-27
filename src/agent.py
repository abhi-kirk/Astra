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
from datetime import datetime
from pathlib import Path

import anthropic
from dotenv import load_dotenv

from src.data_layer import get_portfolio, get_market_data_bulk, load_convictions
from src.strategy import screen_all_positions
from src import memory

load_dotenv(Path(__file__).parent.parent / ".env")

ROOT = Path(__file__).parent.parent
ANALYSIS_DIR = ROOT / "analysis"

REASONING_MODEL = "claude-haiku-4-5-20251001"   # cheap + fast for weekly batch runs


def call_claude_reasoning(
    signals: list,
    portfolio: dict,
    market_data: dict,
    history_context: str,
    convictions: dict,
) -> str:
    """
    Call Claude API to synthesize mechanical signals into advisor narrative.
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

    prompt = f"""You are ASTRA, an AI trading advisor for a personal Robinhood portfolio.

INVESTOR PROFILE:
- Software engineer at Tesla. Deep tech + space sector expertise.
- TSLA excluded (employment equity + blackout restrictions).
- Conviction themes: {json.dumps(themes)}
- Investment style: medium-term (weeks to months), conviction-based, not high-frequency.
- Key rule: no averaging down past 3x on positions >35% below cost basis.

PRIOR DECISION HISTORY (for continuity):
{history_context}

THIS WEEK'S MECHANICAL SIGNALS:
{signal_text}

Write a concise weekly advisor note (200-300 words) covering:
1. PRIORITY ACTIONS: What needs attention this week and why (be specific about tickers).
2. RISK FLAGS: Any positions or patterns worth watching.
3. CONVICTION CHECK: Do this week's signals align with the investor's stated themes?
4. ONE THING TO WATCH: A single forward-looking observation for next week.

Be direct and specific. Reference actual tickers and prices. No generic disclaimers."""

    client = anthropic.Anthropic(api_key=api_key)
    message = client.messages.create(
        model=REASONING_MODEL,
        max_tokens=500,
        messages=[{"role": "user", "content": prompt}],
    )
    return message.content[0].text


def run(mode: str = "simulation", single_ticker: str | None = None, use_ai: bool = True):
    ANALYSIS_DIR.mkdir(exist_ok=True)

    run_date = datetime.now().isoformat()
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

    # 8. Log decisions to Supabase
    for sig in signals:
        pos = portfolio.get(sig.ticker, {})
        mdata = market_data.get(sig.ticker, {})
        if sig.action in ("buy", "review", "watch"):
            memory.log_decision(
                ticker=sig.ticker,
                action=sig.action,
                reasoning="; ".join(sig.reasons),
                signal_data=sig.to_dict(),
                price_at_decision=mdata.get("current_price"),
                shares_held=pos.get("shares"),
                avg_cost=pos.get("avg_cost"),
                executed=(mode == "live"),
                run_date=run_date,
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

    out_file = ANALYSIS_DIR / f"{run_date[:10]}.json"
    out_file.write_text(json.dumps(output, indent=2, default=str))
    print(f"\nAnalysis written to {out_file}")

    memory.log_run_summary(
        mode=mode, signals=[s.to_dict() for s in signals],
        summary=summary, raw_output=output, run_date=run_date,
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
