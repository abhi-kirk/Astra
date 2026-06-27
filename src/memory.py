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
