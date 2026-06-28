"""
Persistent memory layer: Supabase-backed decision log.

All analysis decisions, outcomes, and run summaries are written here.
The agent reads recent history at the start of each run for continuity.
"""

from __future__ import annotations

import json
from datetime import datetime

from src.db import get_client


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------

def log_decision(
    ticker: str,
    action: str,
    reasoning: str,
    signal_data: dict,
    price_at_decision: float | None = None,
    shares_held: float | None = None,
    avg_cost: float | None = None,
    executed: bool = False,
    run_date: str | None = None,
) -> int | None:
    """Insert a decision record. Returns the new row id."""
    run_date = run_date or datetime.now().isoformat()
    row = {
        "run_date": run_date,
        "ticker": ticker,
        "action": action,
        "reasoning": reasoning,
        "signal_data": signal_data,
        "price_at_decision": price_at_decision,
        "shares_held": shares_held,
        "avg_cost": avg_cost,
        "executed": executed,
    }
    result = get_client().table("decisions").insert(row).execute()
    if result.data:
        return result.data[0]["id"]
    return None


def log_run_summary(
    mode: str,
    signals: list[dict],
    summary: str,
    raw_output: dict | None = None,
    public_output: dict | None = None,
    run_date: str | None = None,
):
    run_date = run_date or datetime.now().isoformat()
    buy_signals = [s["ticker"] for s in signals if s.get("action") == "buy"]
    get_client().table("run_summaries").insert({
        "run_date": run_date,
        "mode": mode,
        "num_signals": len(signals),
        "buy_signals": buy_signals,
        "summary": summary,
        "raw_output": raw_output or {"signals": signals},
        "public_output": public_output,
    }).execute()


def record_outcome(
    decision_id: int,
    price_at_outcome: float,
    notes: str = "",
):
    """Record the observed outcome for a past decision."""
    db = get_client()
    row = db.table("decisions").select("price_at_decision").eq("id", decision_id).execute()
    pct_change = None
    if row.data and row.data[0].get("price_at_decision"):
        prior = float(row.data[0]["price_at_decision"])
        pct_change = round((price_at_outcome - prior) / prior * 100, 2)

    db.table("outcomes").insert({
        "decision_id": decision_id,
        "outcome_date": datetime.now().isoformat(),
        "price_at_outcome": price_at_outcome,
        "pct_change": pct_change,
        "notes": notes,
    }).execute()


def save_conviction_snapshot(content: dict):
    """Overwrite the single convictions row (upsert pattern)."""
    db = get_client()
    existing = db.table("convictions").select("id").limit(1).execute()
    if existing.data:
        db.table("convictions").update({
            "content": content,
            "updated_at": datetime.now().isoformat(),
        }).eq("id", existing.data[0]["id"]).execute()
    else:
        db.table("convictions").insert({
            "content": content,
            "updated_at": datetime.now().isoformat(),
        }).execute()


def save_portfolio_snapshot(positions: dict):
    """Write a portfolio snapshot (called after Robinhood MCP read)."""
    get_client().table("portfolio_snapshots").insert({
        "snapshot_time": datetime.now().isoformat(),
        "source": "robinhood_mcp",
        "positions": positions,
    }).execute()


PAPER_PORTFOLIO_SIZE  = 10_000.0  # virtual portfolio baseline ($10K)
PAPER_MAX_POSITION_PCT = 0.10     # hard cap: no single position > 10% of virtual portfolio

def log_paper_trade(
    ticker: str,
    price: float,
    run_date: str,
    signal_data: dict | None = None,
    suggested_pct: float | None = None,
) -> None:
    """Log a virtual BUY trade when ASTRA issues a BUY signal.

    Position size = suggested_position_pct × PAPER_PORTFOLIO_SIZE,
    capped at PAPER_MAX_POSITION_PCT × PAPER_PORTFOLIO_SIZE.
    Defaults to 4% ($400) if no suggested_pct is provided.
    Skips if an open paper position already exists for this ticker.
    """
    db = get_client()
    existing = (
        db.table("paper_trades")
        .select("id")
        .eq("ticker", ticker)
        .eq("is_open", True)
        .limit(1)
        .execute()
    )
    if existing.data:
        return  # already holding a virtual position — no pyramiding in paper mode

    pct = suggested_pct if suggested_pct else 0.04
    raw_cost = pct * PAPER_PORTFOLIO_SIZE
    virtual_cost = min(raw_cost, PAPER_MAX_POSITION_PCT * PAPER_PORTFOLIO_SIZE)
    virtual_shares = round(virtual_cost / price, 6) if price else 0

    db.table("paper_trades").insert({
        "ticker": ticker,
        "action": "buy",
        "price_at_signal": price,
        "virtual_shares": virtual_shares,
        "virtual_cost": round(virtual_cost, 2),
        "suggested_position_pct": round(pct, 4),
        "run_date": run_date,
        "signal_data": signal_data or {},
        "is_open": True,
    }).execute()
    print(f"  [paper] BUY  {ticker}: {virtual_shares:.4f} shares @ ${price:.2f}  "
          f"(${virtual_cost:.0f} = {pct:.0%} of ${PAPER_PORTFOLIO_SIZE:.0f})")


def close_paper_trade(
    ticker: str,
    close_price: float,
    run_date: str,
    reason: str,
) -> None:
    """Close an open paper trade. Reason: signal_inactive | profit_take | blocked."""
    db = get_client()
    trade = (
        db.table("paper_trades")
        .select("id, price_at_signal, virtual_shares, virtual_cost")
        .eq("ticker", ticker)
        .eq("is_open", True)
        .limit(1)
        .execute()
    )
    if not trade.data:
        return

    t = trade.data[0]
    pnl_d   = (close_price - t["price_at_signal"]) * t["virtual_shares"]
    pnl_pct = (close_price - t["price_at_signal"]) / t["price_at_signal"] * 100

    db.table("paper_trades").update({
        "is_open":      False,
        "closed_at":    run_date,
        "close_price":  close_price,
        "close_reason": reason,
    }).eq("id", t["id"]).execute()

    print(f"  [paper] CLOSE {ticker}: ${close_price:.2f}  "
          f"P&L ${pnl_d:+.2f} ({pnl_pct:+.1f}%)  reason={reason}")


def get_open_paper_trades() -> list[dict]:
    result = (
        get_client()
        .table("paper_trades")
        .select("*")
        .eq("is_open", True)
        .order("run_date", desc=True)
        .execute()
    )
    return result.data or []


def get_paper_trades_history() -> list[dict]:
    result = (
        get_client()
        .table("paper_trades")
        .select("*")
        .order("run_date", desc=True)
        .execute()
    )
    return result.data or []


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------

def get_recent_decisions(n: int = 30) -> list[dict]:
    result = (
        get_client()
        .table("decisions")
        .select("*, outcomes(price_at_outcome, pct_change)")
        .order("run_date", desc=True)
        .limit(n)
        .execute()
    )
    return result.data or []


def get_ticker_history(ticker: str) -> list[dict]:
    result = (
        get_client()
        .table("decisions")
        .select("*, outcomes(price_at_outcome, pct_change)")
        .eq("ticker", ticker)
        .order("run_date")
        .execute()
    )
    return result.data or []


def get_run_summaries(n: int = 10) -> list[dict]:
    result = (
        get_client()
        .table("run_summaries")
        .select("*")
        .order("run_date", desc=True)
        .limit(n)
        .execute()
    )
    return result.data or []


def get_latest_convictions() -> dict | None:
    result = (
        get_client()
        .table("convictions")
        .select("content")
        .order("updated_at", desc=True)
        .limit(1)
        .execute()
    )
    if result.data:
        return result.data[0]["content"]
    return None


def get_latest_portfolio_snapshot() -> dict | None:
    result = (
        get_client()
        .table("portfolio_snapshots")
        .select("positions, snapshot_time")
        .order("snapshot_time", desc=True)
        .limit(1)
        .execute()
    )
    if result.data:
        return result.data[0]
    return None


def build_agent_context_summary(max_decisions: int = 20) -> str:
    """Compact text summary of recent history for agent context. Token-efficient."""
    recent = get_recent_decisions(max_decisions)
    if not recent:
        return "No prior decisions on record. This is the first analysis run."

    lines = ["=== RECENT DECISION HISTORY ==="]
    for d in recent:
        outcome_rows = d.get("outcomes") or []
        outcome = ""
        if outcome_rows:
            pct = outcome_rows[0].get("pct_change")
            if pct is not None:
                outcome = f" → outcome: {pct:+.1f}%"
        lines.append(
            f"{str(d['run_date'])[:10]}  {d['ticker']:8s}  {d['action']:8s}  "
            f"@${d.get('price_at_decision') or 0:.2f}{outcome}"
        )
        if d.get("reasoning"):
            lines.append(f"  Reasoning: {d['reasoning'][:120]}")

    summaries = get_run_summaries(3)
    if summaries:
        lines.append("\n=== RECENT RUN SUMMARIES ===")
        for s in summaries:
            lines.append(f"{str(s['run_date'])[:10]}: {s.get('summary', '')[:200]}")

    return "\n".join(lines)
