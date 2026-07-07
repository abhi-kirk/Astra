"""
Persistent memory layer: Supabase-backed decision log.

All analysis decisions, outcomes, and run summaries are written here.
The agent reads recent history at the start of each run for continuity.
"""

from __future__ import annotations

import logging
from datetime import datetime

from src import config
from src.db import Rows, get_client
from src.db import rows as db_rows

logger = logging.getLogger(__name__)


# A persistent signal (e.g. a profit-take that stays >60% up) re-logs weekly, not on
# every daily run — keeps `decisions` and the advisor's history context signal-dense
# while still refreshing often enough for trade-journal attribution (3-day window).
DECISION_RELOG_DAYS = 7


def should_log_decision(last: dict | None, action: str, run_date: str) -> bool:
    """Dedup gate for the decision log: skip when the ticker's most recent decision has
    the same action and is fresher than DECISION_RELOG_DAYS. Pure — unit-testable."""
    if not last or last.get("action") != action:
        return True
    try:
        prev = datetime.fromisoformat(str(last["run_date"]).replace("Z", "+00:00")).replace(tzinfo=None)
        now = datetime.fromisoformat(str(run_date).replace("Z", "+00:00")).replace(tzinfo=None)
    except (ValueError, TypeError, KeyError):
        return True
    return (now - prev).days >= DECISION_RELOG_DAYS


def get_latest_decisions_for(tickers: list[str]) -> dict[str, dict]:
    """Most recent decision row per ticker (for the dedup gate). One query."""
    if not tickers:
        return {}
    data = db_rows(
        get_client().table("decisions")
        .select("ticker, action, run_date")
        .in_("ticker", tickers)
        .order("run_date", desc=True).limit(500)
        .execute().data
    )
    latest: dict[str, dict] = {}
    for row in data:
        latest.setdefault(row["ticker"], row)
    return latest


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
    result = get_client().table("decisions").insert({
        "run_date": run_date,
        "ticker": ticker,
        "action": action,
        "reasoning": reasoning,
        "signal_data": signal_data,
        "price_at_decision": price_at_decision,
        "shares_held": shares_held,
        "avg_cost": avg_cost,
        "executed": executed,
    }).execute()
    data = db_rows(result.data)
    return data[0].get("id") if data else None


def log_run_summary(
    mode: str,
    signals: list[dict],
    summary: str,
    raw_output: dict | None = None,
    public_output: dict | None = None,
    run_date: str | None = None,
) -> None:
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
) -> None:
    """Record the observed outcome for a past decision."""
    db = get_client()
    data = db_rows(db.table("decisions").select("price_at_decision").eq("id", decision_id).execute().data)
    pct_change = None
    if data and data[0].get("price_at_decision"):
        prior = float(data[0]["price_at_decision"])
        pct_change = round((price_at_outcome - prior) / prior * 100, 2)
    db.table("outcomes").insert({
        "decision_id": decision_id,
        "outcome_date": datetime.now().isoformat(),
        "price_at_outcome": price_at_outcome,
        "pct_change": pct_change,
        "notes": notes,
    }).execute()


def save_conviction_snapshot(content: dict) -> None:
    """Overwrite the single convictions row (upsert pattern) and append a history snapshot."""
    db = get_client()
    existing = db_rows(db.table("convictions").select("id").limit(1).execute().data)
    if existing:
        db.table("convictions").update({
            "content": content,
            "updated_at": datetime.now().isoformat(),
        }).eq("id", existing[0]["id"]).execute()
    else:
        db.table("convictions").insert({
            "content": content,
            "updated_at": datetime.now().isoformat(),
        }).execute()
    save_conviction_history(content)


def save_conviction_history(content: dict) -> None:
    """Append a full conviction snapshot for audit/history purposes."""
    get_client().table("conviction_history").insert({
        "content": content,
        "saved_at": datetime.now().isoformat(),
    }).execute()


def save_portfolio_snapshot(positions: dict) -> None:
    """Write a portfolio snapshot (called after Robinhood MCP read)."""
    get_client().table("portfolio_snapshots").insert({
        "snapshot_time": datetime.now().isoformat(),
        "source": "robinhood_mcp",
        "positions": positions,
    }).execute()


def log_paper_trade(
    ticker: str,
    price: float,
    run_date: str,
    signal_data: dict | None = None,
    suggested_pct: float | None = None,
) -> int | None:
    """Log a virtual BUY trade when ASTRA issues a BUY signal.

    Size = suggested_pct × config.paper.portfolio_size, capped at config.paper.max_position_pct.
    Skips if an open paper position already exists for this ticker.
    Returns the paper_trade id (existing or newly inserted) so Autotrader can link
    agent_trades.mirrors_paper_trade_id; None only if the insert returns no row.
    """
    db = get_client()
    existing = db_rows(
        db.table("paper_trades").select("id").eq("ticker", ticker).eq("is_open", True).limit(1).execute().data
    )
    if existing:
        return existing[0].get("id")  # no pyramiding in paper mode — return the open lot's id

    pct = suggested_pct or config.paper.default_position_pct
    virtual_cost = min(pct * config.paper.portfolio_size, config.paper.max_position_pct * config.paper.portfolio_size)
    virtual_shares = round(virtual_cost / price, 6) if price else 0

    result = db.table("paper_trades").insert({
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
    logger.info(f"Paper BUY  {ticker}: {virtual_shares:.4f} shares @ ${price:.2f}  (${virtual_cost:.0f} = {pct:.0%} of ${config.paper.portfolio_size:.0f})")
    data = db_rows(result.data)
    return data[0].get("id") if data else None


def close_paper_trade(
    ticker: str, close_price: float, run_date: str, reason: str, fraction: float = 1.0
) -> None:
    """Close (or partially trim) an open paper trade.

    Reasons: signal_inactive | thesis_invalidation | trailing_stop | profit_take | blocked
    (full close), or parabolic_trim (partial). `fraction` ∈ (0,1] is the portion to sell;
    a fraction < 1 reduces the open lot's shares/cost proportionally and keeps it open (the
    runner), leaving the per-share cost basis (price_at_signal) unchanged.
    """
    db = get_client()
    data = db_rows(
        db.table("paper_trades")
        .select("id, price_at_signal, virtual_shares, virtual_cost")
        .eq("ticker", ticker).eq("is_open", True).limit(1)
        .execute().data
    )
    if not data:
        return

    t = data[0]
    frac = min(max(fraction, 0.0), 1.0)
    sold_shares = t["virtual_shares"] * frac
    pnl_d   = (close_price - t["price_at_signal"]) * sold_shares
    pnl_pct = (close_price - t["price_at_signal"]) / t["price_at_signal"] * 100

    if frac >= 1.0:
        db.table("paper_trades").update({
            "is_open":      False,
            "closed_at":    run_date,
            "close_price":  close_price,
            "close_reason": reason,
        }).eq("id", t["id"]).execute()
        logger.info(f"Paper CLOSE {ticker}: ${close_price:.2f}  P&L ${pnl_d:+.2f} ({pnl_pct:+.1f}%)  reason={reason}")
    else:
        # Partial trim — reduce the open lot, keep the runner.
        db.table("paper_trades").update({
            "virtual_shares": round(t["virtual_shares"] - sold_shares, 6),
            "virtual_cost":   round(t["virtual_cost"] * (1.0 - frac), 2),
        }).eq("id", t["id"]).execute()
        logger.info(f"Paper TRIM  {ticker}: sold {frac:.0%} @ ${close_price:.2f}  "
                    f"realized ${pnl_d:+.2f} ({pnl_pct:+.1f}%)  reason={reason}")


def upsert_exploration_candidate(candidate: dict) -> None:
    """Insert or update an exploration candidate (keyed on ticker)."""
    get_client().table("exploration_candidates").upsert({
        "ticker":            candidate["ticker"],
        "source_theme":      candidate["source_theme"],
        "rationale":         candidate.get("rationale"),
        "quality_summary":   candidate.get("quality_summary"),
        "analyst_summary":   candidate.get("analyst_summary"),
        "claude_conviction": candidate.get("claude_conviction"),
        "status":            candidate.get("status", "on_radar"),
        "updated_at":        datetime.now().isoformat(),
    }, on_conflict="ticker").execute()


def get_on_radar_candidates() -> Rows:
    return db_rows(
        get_client().table("exploration_candidates")
        .select("*")
        .eq("status", "on_radar")
        .order("discovered_at", desc=True)
        .execute().data
    )


def update_exploration_status(ticker: str, status: str) -> None:
    get_client().table("exploration_candidates").update({
        "status":     status,
        "updated_at": datetime.now().isoformat(),
    }).eq("ticker", ticker).execute()


def get_all_exploration_tickers() -> set[str]:
    """
    Tickers to exclude from re-discovery: currently visible or already graduated.
    Rejected tickers are intentionally omitted so Claude can re-surface them
    if the thesis strengthens in a subsequent week.
    """
    data = db_rows(
        get_client().table("exploration_candidates")
        .select("ticker")
        .in_("status", ["on_radar", "paper_trading", "graduated"])
        .execute().data
    )
    return {row["ticker"] for row in data}


def get_open_paper_trades() -> Rows:
    return db_rows(
        get_client().table("paper_trades").select("*").eq("is_open", True)
        .order("run_date", desc=True).execute().data
    )


def get_paper_trades_opened_on(day: str) -> Rows:
    """Paper BUYs opened on the calendar day of `day` (Autotrader buy-mirror source)."""
    d = str(day)[:10]
    return db_rows(
        get_client().table("paper_trades").select("*")
        .gte("run_date", f"{d}T00:00:00").lte("run_date", f"{d}T23:59:59.999999")
        .order("run_date", desc=True).execute().data
    )


def get_paper_trades_closed_on(day: str) -> Rows:
    """Paper positions closed on the calendar day of `day` (Autotrader sell-mirror source)."""
    d = str(day)[:10]
    return db_rows(
        get_client().table("paper_trades").select("*").eq("is_open", False)
        .gte("closed_at", f"{d}T00:00:00").lte("closed_at", f"{d}T23:59:59.999999")
        .order("closed_at", desc=True).execute().data
    )


def get_paper_trades_history() -> Rows:
    return db_rows(
        get_client().table("paper_trades").select("*")
        .order("run_date", desc=True).execute().data
    )


# ---------------------------------------------------------------------------
# Autotrader — autonomous agentic trading (agent_trades / snapshots / control)
# ---------------------------------------------------------------------------

def log_agent_trade(
    ticker: str,
    side: str,
    run_date: str,
    order_type: str = "limit",
    quantity: float | None = None,
    limit_price: float | None = None,
    fill_price: float | None = None,
    dollar_amount: float | None = None,
    order_id: str | None = None,
    ref_id: str | None = None,
    status: str = "pending",
    rule_checks: dict | None = None,
    mirrors_paper_trade_id: int | None = None,
    source: str = "mirror",
    submitted_at: str | None = None,
    is_open: bool | None = None,
) -> int | None:
    """Insert one real (or dry-run) agentic order row. Returns the new row id.

    `is_open` marks a live long position for dashboard P&L; defaults to True only for a
    genuinely placed BUY (not blocked/dry-run/rejected).
    """
    if is_open is None:
        is_open = side == "buy" and status in ("pending", "submitted", "filled")
    result = get_client().table("agent_trades").insert({
        "ticker": ticker,
        "side": side,
        "order_type": order_type,
        "quantity": quantity,
        "limit_price": limit_price,
        "fill_price": fill_price,
        "dollar_amount": dollar_amount,
        "order_id": order_id,
        "ref_id": ref_id,
        "status": status,
        "submitted_at": submitted_at,
        "rule_checks": rule_checks or {},
        "mirrors_paper_trade_id": mirrors_paper_trade_id,
        "source": source,
        "run_date": run_date,
        "is_open": is_open,
    }).execute()
    data = db_rows(result.data)
    return data[0].get("id") if data else None


def update_agent_trade_status(
    trade_id: int,
    status: str,
    order_id: str | None = None,
    fill_price: float | None = None,
    executed_at: str | None = None,
) -> None:
    """Update an agent_trades row after the broker responds (fill / cancel / reject)."""
    patch: dict = {"status": status}
    if order_id is not None:
        patch["order_id"] = order_id
    if fill_price is not None:
        patch["fill_price"] = fill_price
    if executed_at is not None:
        patch["executed_at"] = executed_at
    get_client().table("agent_trades").update(patch).eq("id", trade_id).execute()


def close_agent_trade(trade_id: int, close_price: float, run_date: str, realized_pnl: float | None = None) -> None:
    """Mark an open agent position closed (after a mirrored SELL fills)."""
    get_client().table("agent_trades").update({
        "is_open": False,
        "closed_at": run_date,
        "close_price": close_price,
        "realized_pnl": realized_pnl,
    }).eq("id", trade_id).execute()


def get_open_agent_trades() -> Rows:
    return db_rows(
        get_client().table("agent_trades").select("*").eq("is_open", True)
        .order("run_date", desc=True).execute().data
    )


def get_agent_trades_today(run_date: str) -> Rows:
    """All agent orders submitted on the calendar day of run_date (for the max-trades/day cap)."""
    day = str(run_date)[:10]
    return db_rows(
        get_client().table("agent_trades").select("*")
        .gte("run_date", f"{day}T00:00:00")
        .lte("run_date", f"{day}T23:59:59.999999")
        .order("run_date", desc=True).execute().data
    )


def get_last_agent_buy(ticker: str) -> dict | None:
    """Most recent placed BUY for a ticker in the agentic account (for the min-hold
    check). Blocked/dry-run rows are not real purchases and must not reset the clock."""
    data = db_rows(
        get_client().table("agent_trades").select("*")
        .eq("ticker", ticker).eq("side", "buy")
        .in_("status", ["pending", "submitted", "filled"])
        .order("run_date", desc=True).limit(1).execute().data
    )
    return data[0] if data else None


def save_agent_account_snapshot(snapshot: dict) -> None:
    """Persist an agentic account state snapshot (positions / cash / equity / drawdown)."""
    get_client().table("agent_account_snapshots").insert({
        "snapshot_time":   snapshot.get("snapshot_time") or datetime.now().isoformat(),
        "cash":            snapshot.get("cash"),
        "buying_power":    snapshot.get("buying_power"),
        "market_value":    snapshot.get("market_value"),
        "total_equity":    snapshot.get("total_equity"),
        "positions":       snapshot.get("positions"),
        "baseline_equity": snapshot.get("baseline_equity"),
        "drawdown_pct":    snapshot.get("drawdown_pct"),
    }).execute()


def get_latest_agent_snapshot() -> dict | None:
    data = db_rows(
        get_client().table("agent_account_snapshots").select("*")
        .order("snapshot_time", desc=True).limit(1).execute().data
    )
    return data[0] if data else None


def get_agent_control() -> dict:
    """Read the single-row Autotrader control switch. Returns defaults if the row is missing."""
    data = db_rows(
        get_client().table("agent_control").select("*").eq("id", 1).limit(1).execute().data
    )
    if data:
        return data[0]
    return {"id": 1, "paused": False, "halted": False, "halt_reason": None, "baseline_equity": None}


def set_agent_paused(paused: bool) -> None:
    get_client().table("agent_control").update({
        "paused": paused, "updated_at": datetime.now().isoformat(),
    }).eq("id", 1).execute()


def set_agent_halted(halted: bool, reason: str | None = None) -> None:
    get_client().table("agent_control").update({
        "halted": halted, "halt_reason": reason, "updated_at": datetime.now().isoformat(),
    }).eq("id", 1).execute()


def set_agent_baseline(equity: float) -> None:
    """Set the drawdown baseline (first run, or a deliberate reset)."""
    get_client().table("agent_control").update({
        "baseline_equity": equity, "updated_at": datetime.now().isoformat(),
    }).eq("id", 1).execute()


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------

def get_recent_decisions(n: int = 30) -> Rows:
    return db_rows(
        get_client().table("decisions")
        .select("*, outcomes(price_at_outcome, pct_change)")
        .order("run_date", desc=True).limit(n).execute().data
    )


def get_ticker_history(ticker: str) -> Rows:
    return db_rows(
        get_client().table("decisions")
        .select("*, outcomes(price_at_outcome, pct_change)")
        .eq("ticker", ticker).order("run_date").execute().data
    )


def get_run_summaries(n: int = 10) -> Rows:
    return db_rows(
        get_client().table("run_summaries").select("*")
        .order("run_date", desc=True).limit(n).execute().data
    )


def get_latest_convictions() -> dict | None:
    data = db_rows(
        get_client().table("convictions").select("content")
        .order("updated_at", desc=True).limit(1).execute().data
    )
    return data[0]["content"] if data else None


def get_pending_trade_feedback() -> Rows:
    return db_rows(
        get_client().table("user_trades_log")
        .select("*")
        .eq("feedback_status", "pending")
        .order("detected_at", desc=True)
        .execute().data
    )


def get_latest_portfolio_snapshot() -> dict | None:
    data = db_rows(
        get_client().table("portfolio_snapshots").select("positions, snapshot_time")
        .order("snapshot_time", desc=True).limit(1).execute().data
    )
    return data[0] if data else None


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
