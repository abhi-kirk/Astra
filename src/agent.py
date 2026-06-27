"""
Agent orchestrator: runs the full analysis pipeline and produces output.

In simulation mode (Phase 1): fetches data, screens positions, logs decisions,
writes analysis JSON. No trades executed.

Usage:
    python -m src.agent                    # simulation run
    python -m src.agent --mode live        # Phase 2: includes trade approval flow
    python -m src.agent --ticker RKLB      # analyze a single ticker
"""

import argparse
import json
from datetime import datetime
from pathlib import Path

from src.data_layer import get_portfolio, get_market_data_bulk, load_convictions
from src.strategy import screen_all_positions
from src import memory

ROOT = Path(__file__).parent.parent
ANALYSIS_DIR = ROOT / "analysis"


def run(mode: str = "simulation", single_ticker: str | None = None):
    ANALYSIS_DIR.mkdir(exist_ok=True)


    run_date = datetime.now().isoformat()
    print(f"\n{'='*60}")
    print(f"Trading Advisor — {mode.upper()} run")
    print(f"Date: {run_date[:10]}")
    print(f"{'='*60}\n")

    # 1. Load convictions
    convictions = load_convictions()
    excluded = {e["ticker"] for e in convictions.get("exclusions", [])}

    # 2. Load full portfolio (always — needed for accurate sizing calculations)
    print("Loading portfolio...")
    portfolio = get_portfolio()
    portfolio = {t: v for t, v in portfolio.items() if t not in excluded}
    print(f"  {len(portfolio)} open positions (excluding {excluded})\n")

    # 3. Fetch market data for full portfolio so position-size checks are accurate
    print("Fetching market data...")
    market_data = get_market_data_bulk(list(portfolio.keys()))

    # Narrow to single ticker for screening output if requested
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

    # 6. Print and log results
    print("\n" + "="*60)
    print("SIGNALS")
    print("="*60)

    action_groups = {"buy": [], "review": [], "watch": [], "hold": [], "blocked": []}
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

    # 7. Log each signal to memory
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

    # 8. Write analysis JSON (for dashboard)
    output = {
        "run_date": run_date,
        "mode": mode,
        "num_positions_screened": len(portfolio),
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

    # 9. Run summary
    buy_tickers = [s.ticker for s in signals if s.action == "buy"]
    summary = (
        f"{len(buy_tickers)} buy signal(s): {', '.join(buy_tickers) or 'none'}. "
        f"{len(action_groups.get('review', []))} profit-take review(s). "
        f"{len(action_groups.get('watch', []))} on watchlist."
    )
    memory.log_run_summary(mode=mode, signals=[s.to_dict() for s in signals],
                           summary=summary, run_date=run_date)

    print(f"\nSummary: {summary}")
    print(f"\nMemory updated. Next run will have context of today's decisions.")
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Trading advisor analysis run")
    parser.add_argument("--mode", choices=["simulation", "live"], default="simulation")
    parser.add_argument("--ticker", help="Analyze a single ticker only")
    args = parser.parse_args()
    run(mode=args.mode, single_ticker=args.ticker)
