"""
Persistent memory layer: Supabase-backed decision log.

All analysis decisions, outcomes, and run summaries are written here.
The agent reads recent history at the start of each run for continuity.
"""

from __future__ import annotations

import logging
from datetime import datetime

from src import config
from src.brain.version import (
    brain_version_fields,
    brain_version_registry_row,
)
from src.db import Rows, get_client
from src.db import rows as db_rows

logger = logging.getLogger(__name__)


def _register_brain_version() -> None:
    """Idempotently record the current brain config behind its hash (once per hash).

    Best-effort metadata for attribution — a registry failure must never abort a trading
    run, so it is logged and swallowed. The per-row version stamp is written regardless.
    """
    try:
        get_client().table("brain_versions").upsert(
            brain_version_registry_row(), on_conflict="config_hash", ignore_duplicates=True
        ).execute()
    except Exception as e:  # noqa: BLE001 — non-critical metadata, never break the run
        logger.warning(f"brain_versions registry upsert failed (non-fatal): {e}")


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


def save_plays_snapshot(content: dict) -> None:
    """Overwrite the single `plays` (Playbook) row and append a history snapshot — the exact
    upsert+historize pattern as convictions. `content` is the full {"plays": [...]} blob."""
    db = get_client()
    existing = db_rows(db.table("plays").select("id").limit(1).execute().data)
    if existing:
        db.table("plays").update({
            "content": content,
            "updated_at": datetime.now().isoformat(),
        }).eq("id", existing[0]["id"]).execute()
    else:
        db.table("plays").insert({
            "content": content,
            "updated_at": datetime.now().isoformat(),
        }).execute()
    save_plays_history(content)


def save_plays_history(content: dict) -> None:
    """Append a full Playbook snapshot for audit/history purposes."""
    get_client().table("plays_history").insert({
        "content": content,
        "saved_at": datetime.now().isoformat(),
    }).execute()


def save_portfolio_snapshot(positions: dict, buying_power: float | None = None) -> None:
    """Write a portfolio snapshot (called after a Robinhood read). `buying_power` is the
    Individual account's deployable cash — the Advisor's real sizing book."""
    get_client().table("portfolio_snapshots").insert({
        "snapshot_time": datetime.now().isoformat(),
        "source": "robinhood_mcp",
        "positions": positions,
        "buying_power": buying_power,
    }).execute()


def save_paper_equity_snapshot(row: dict) -> None:
    """Persist one forward-only snapshot of the notional paper book (see src/performance.py).
    `holdings` carries the notional lot ledger forward for the next run's reconciliation."""
    get_client().table("paper_equity").insert({
        "snapshot_time":    row.get("snapshot_time") or datetime.now().isoformat(),
        "cash":             row.get("cash"),
        "market_value":     row.get("market_value"),
        "total_equity":     row.get("total_equity"),
        "invested_cost":    row.get("invested_cost"),
        "unrealized_pnl":   row.get("unrealized_pnl"),
        "realized_pnl_cum": row.get("realized_pnl_cum"),
        "nav_index":        row.get("nav_index"),
        "holdings":         row.get("holdings"),
    }).execute()


def get_latest_paper_equity() -> dict | None:
    """Most recent paper-book snapshot — the carried cash / holdings / NAV for the next run."""
    data = db_rows(
        get_client().table("paper_equity").select("*")
        .order("snapshot_time", desc=True).limit(1).execute().data
    )
    return data[0] if data else None


def upsert_benchmark_price(symbol: str, price_date: str, close: float) -> None:
    """Idempotent daily benchmark close (one row per symbol+date)."""
    get_client().table("benchmark_prices").upsert({
        "symbol":     symbol,
        "price_date": price_date,
        "close":      close,
    }, on_conflict="symbol,price_date").execute()


def _may_add_paper_lot(open_lots: list[dict], run_date: str) -> bool:
    """Bounded-pyramiding gate: True if an additional paper lot may open now — under the
    max-adds cap AND the newest existing lot is older than the add cooldown. Pure/testable."""
    if len(open_lots) > config.paper.max_adds_per_ticker:  # initial lot + N adds already open
        return False
    newest = open_lots[0].get("run_date")
    try:
        prev = datetime.fromisoformat(str(newest).replace("Z", "+00:00")).replace(tzinfo=None)
        now = datetime.fromisoformat(str(run_date).replace("Z", "+00:00")).replace(tzinfo=None)
    except (ValueError, TypeError, AttributeError):
        return False  # unparseable timestamp → fail safe (no add)
    return (now - prev).days >= config.paper.add_cooldown_days


def _days_between(newest: str | None, run_date: str) -> int | None:
    """Whole calendar days between `newest` and `run_date`, or None if unparseable."""
    try:
        prev = datetime.fromisoformat(str(newest).replace("Z", "+00:00")).replace(tzinfo=None)
        now = datetime.fromisoformat(str(run_date).replace("Z", "+00:00")).replace(tzinfo=None)
    except (ValueError, TypeError, AttributeError):
        return None
    return (now - prev).days


def paper_buy_label(open_lots: list[dict], run_date: str) -> str:
    """Classify how *actionable* a BUY signal is, given the ticker's open paper lots.

    A conviction name in an uptrend clears the buy threshold every run, but bounded pyramiding
    (see `_may_add_paper_lot`) only lets it actually add every few days and caps total adds. This
    labels the signal so the advisor note can tell a fresh/actionable buy from a still-qualifying
    but throttled one. Pure — unit-testable. Mirrors the `_may_add_paper_lot` gate exactly.

    Returns one of:
      "NEW ENTRY"              — no open lot yet; a real first buy.
      "ADD n of N"             — an open lot exists, cooldown elapsed → an add would execute now.
      "AT ADD CAP"             — already at the initial lot + N adds; no further adds (informational).
      "IN COOLDOWN (Md left)"  — under the cap but the newest lot is younger than the cooldown.
    """
    max_adds = config.paper.max_adds_per_ticker
    cooldown = config.paper.add_cooldown_days
    if not open_lots:
        return "NEW ENTRY"
    if len(open_lots) > max_adds:  # initial lot + N adds already open
        return "AT ADD CAP"
    days = _days_between(open_lots[0].get("run_date"), run_date)
    if days is None:
        return "AT ADD CAP"  # fail safe — matches _may_add_paper_lot returning no-add
    if days >= cooldown:
        return f"ADD {len(open_lots)} of {max_adds}"  # this run would open add #len(open_lots)
    return f"IN COOLDOWN ({cooldown - days}d left)"


def get_paper_buy_labels(tickers: list[str], run_date: str) -> dict[str, str]:
    """Pyramiding-state label per ticker for the advisor note. One query for all names."""
    if not tickers:
        return {}
    rows = db_rows(
        get_client().table("paper_trades").select("ticker, run_date")
        .in_("ticker", tickers).eq("is_open", True)
        .order("run_date", desc=True).execute().data
    )
    lots_by_ticker: dict[str, list[dict]] = {}
    for row in rows:
        lots_by_ticker.setdefault(row["ticker"], []).append(row)
    return {t: paper_buy_label(lots_by_ticker.get(t, []), run_date) for t in tickers}


def log_paper_trade(
    ticker: str,
    price: float,
    run_date: str,
    signal_data: dict | None = None,
    suggested_pct: float | None = None,
) -> int | None:
    """Log a virtual BUY trade when ASTRA issues a BUY signal.

    Size = suggested_pct × the Advisor book (real Individual-account deployable cash, or the
    configured fallback), capped at config.paper.max_position_pct of that book.
    Bounded pyramiding: if an open lot already exists, opens an *additional* lot (an add) only
    when under config.paper.max_adds_per_ticker and the newest lot is older than the cooldown;
    otherwise returns the existing lot's id (no-op). Over-concentration is already gated upstream
    by the position-size + averaging-down hard rules (a buy over cap is `blocked`, never here).
    Returns the paper_trade id (existing or newly inserted) so Autotrader can link
    agent_trades.mirrors_paper_trade_id; None only if the insert returns no row.
    """
    db = get_client()
    existing = db_rows(
        db.table("paper_trades").select("id, run_date").eq("ticker", ticker).eq("is_open", True)
        .order("run_date", desc=True).execute().data
    )
    if existing and not _may_add_paper_lot(existing, run_date):
        return existing[0].get("id")  # at add cap or within cooldown — return the open lot's id

    from src.data_layer import get_advisor_book
    book = get_advisor_book()  # real Individual-account deployable cash, or the configured fallback
    pct = suggested_pct or config.paper.default_position_pct
    virtual_cost = min(pct * book, config.paper.max_position_pct * book)
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
        **brain_version_fields(),  # stamp the brain that opened this lot (travels to close)
    }).execute()
    logger.info(f"Paper BUY  {ticker}: {virtual_shares:.4f} shares @ ${price:.2f}  (${virtual_cost:.0f} = {pct:.0%} of ${book:.0f})")
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
    lots = db_rows(
        db.table("paper_trades")
        .select("id, price_at_signal, virtual_shares, virtual_cost")
        .eq("ticker", ticker).eq("is_open", True)
        .execute().data
    )
    if not lots:
        return

    # A ticker may hold several open lots (bounded pyramiding). A full close closes every lot;
    # a partial trim reduces each proportionally so the aggregate position is trimmed by `frac`.
    frac = min(max(fraction, 0.0), 1.0)
    trim_realized = 0.0
    trim_sold_shares = 0.0
    for t in lots:
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
            trim_realized += pnl_d
            trim_sold_shares += sold_shares
            logger.info(f"Paper TRIM  {ticker}: sold {frac:.0%} @ ${close_price:.2f}  "
                        f"realized ${pnl_d:+.2f} ({pnl_pct:+.1f}%)  reason={reason}")

    # Persist one aggregate trim event (append-only) so the Autotrader can mirror the ACTION —
    # sell `frac` of its own holding — and for trim attribution. A full close never lands here;
    # it flows to the Autotrader via the close-based sell mirror (get_paper_trades_closed_on).
    if 0.0 < frac < 1.0:
        db.table("paper_trims").insert({
            "ticker":       ticker,
            "run_date":     run_date,
            "close_price":  close_price,
            "fraction":     round(frac, 6),
            "realized_pnl": round(trim_realized, 2),
            "sold_shares":  round(trim_sold_shares, 6),
            "close_reason": reason,
            **brain_version_fields(),
        }).execute()


def get_paper_trims_on(day: str) -> Rows:
    """Parabolic-trim events fired on the calendar day of `day` (Autotrader trim-mirror source)."""
    d = str(day)[:10]
    return db_rows(
        get_client().table("paper_trims").select("*")
        .gte("run_date", f"{d}T00:00:00").lte("run_date", f"{d}T23:59:59.999999")
        .order("run_date", desc=True).execute().data
    )


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
        **brain_version_fields(),  # stamp the brain that placed this order (travels to close)
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
        "drawdown_pct":    snapshot.get("drawdown_pct"),
        "realized_pnl":        snapshot.get("realized_pnl"),
        "unrealized_pnl":      snapshot.get("unrealized_pnl"),
        "net_pnl":             snapshot.get("net_pnl"),
        "invested_cost_basis": snapshot.get("invested_cost_basis"),
        "nav_index":           snapshot.get("nav_index"),
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
    return {"id": 1, "paused": False, "halted": False, "halt_reason": None}


def set_agent_paused(paused: bool) -> None:
    get_client().table("agent_control").update({
        "paused": paused, "updated_at": datetime.now().isoformat(),
    }).eq("id", 1).execute()


def set_agent_halted(halted: bool, reason: str | None = None) -> None:
    get_client().table("agent_control").update({
        "halted": halted, "halt_reason": reason, "updated_at": datetime.now().isoformat(),
    }).eq("id", 1).execute()


# ---------------------------------------------------------------------------
# ML data capture (decision_features / user_orders / agent_runs)
# ---------------------------------------------------------------------------

def log_decision_features(rows: list[dict], run_date: str | None = None) -> None:
    """Append one decision_features row per screened ticker (the ML training substrate).

    Rows come from src.brain.snapshot.build_snapshot — no dedup, includes hold/blocked so
    negative examples are captured. `run_date` is stamped onto each row here. One bulk insert.
    """
    if not rows:
        return
    run_date = run_date or datetime.now().isoformat()
    vf = brain_version_fields()
    payload = [{**r, "run_date": run_date, **vf} for r in rows]
    _register_brain_version()
    get_client().table("decision_features").insert(payload).execute()
    logger.info(f"Logged {len(payload)} decision-feature snapshot(s)")


def log_contextual_inputs(rows: list[dict], run_date: str | None = None) -> None:
    """Append one contextual_inputs row per structured proposal from the contextual layer
    (docs/contextual_reasoning.md) — the play-tagged, bounded input the LLM/quant plays fed into
    the deterministic pipeline. `run_date` + the brain-version pair are stamped here so attribution
    can segment per play, proposed-vs-applied (`applied`), and quant-vs-LLM (`source`).

    Each row: ticker, play, kind, value, applied, source, rationale, evidence."""
    if not rows:
        return
    run_date = run_date or datetime.now().isoformat()
    vf = brain_version_fields()
    payload = [{**r, "run_date": run_date, **vf} for r in rows]
    _register_brain_version()
    get_client().table("contextual_inputs").insert(payload).execute()
    logger.info(f"Logged {len(payload)} contextual-input proposal(s)")


def upsert_user_orders(rows: list[dict]) -> None:
    """Idempotently persist Abhi's real Robinhood fills (ground truth). Keyed on order_id,
    so re-runs never duplicate. Rows come from robinhood.fetch_recent_orders."""
    if not rows:
        return
    get_client().table("user_orders").upsert(
        rows, on_conflict="order_id", ignore_duplicates=True
    ).execute()
    logger.info(f"Upserted {len(rows)} user order(s)")


def log_agent_run(summary: dict, sleeve: dict | None = None, run_date: str | None = None) -> None:
    """Persist one Autotrader run: the full summary (placed/blocked/skipped+reasons/failed/
    halted/aborted) + sleeve allocation math that previously only reached Telegram/logs."""
    _register_brain_version()
    get_client().table("agent_runs").insert({
        "run_date": run_date or datetime.now().isoformat(),
        "dry_run": bool(summary.get("dry_run")),
        "summary": summary,
        "sleeve": sleeve or {},
        **brain_version_fields(),
    }).execute()


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


def get_latest_plays() -> dict | None:
    data = db_rows(
        get_client().table("plays").select("content")
        .order("updated_at", desc=True).limit(1).execute().data
    )
    return data[0]["content"] if data else None


def get_latest_settings() -> dict | None:
    """The live settings-override blob ({"values": {...}}). Same single-row shape as
    convictions/plays. Missing table or empty row → None (the run uses env/code defaults)."""
    data = db_rows(
        get_client().table("settings").select("content")
        .order("updated_at", desc=True).limit(1).execute().data
    )
    return data[0]["content"] if data else None


def save_settings_snapshot(content: dict) -> None:
    """Write the override map and append a history snapshot — the same upsert+historize
    pattern as convictions and plays. `content` is {"values": {...}}.

    Merges rather than replaces: `save_settings_effective` writes a sibling key of the same
    row, so a blind overwrite here would drop the snapshot the dashboard displays. The
    dashboard's own save path merges the same way.
    """
    db = get_client()
    existing = db_rows(db.table("settings").select("id, content").limit(1).execute().data)
    if existing:
        merged = {**(existing[0].get("content") or {}), **content}
        db.table("settings").update({
            "content": merged,
            "updated_at": datetime.now().isoformat(),
        }).eq("id", existing[0]["id"]).execute()
    else:
        merged = content
        db.table("settings").insert({
            "content": merged,
            "updated_at": datetime.now().isoformat(),
        }).execute()
    save_settings_history(merged)


def save_settings_effective(snapshot: dict, source: str = "github-actions") -> None:
    """Record what the tunables ACTUALLY resolved to on this run, alongside the override map.

    The dashboard cannot compute this itself: it can read the saved overrides and the code
    defaults, but not the environment layer, so an env-supplied value (AGENT_TRADING_ENABLED
    in CI, say) would otherwise be displayed wrong. Writing the resolved snapshot back means
    the UI shows the truth the run used.

    `source` records which run produced it. Only production runs call this (see
    settings._record_effective) — a dev machine has a different environment, and this row is
    shared state the dashboard presents as the truth about production.

    Stored under a separate key of the same single row, so the override map and this snapshot
    never clobber each other: each writer merges into the existing content.
    """
    db = get_client()
    existing = db_rows(db.table("settings").select("id, content").limit(1).execute().data)
    payload = {
        "effective": snapshot,
        "effective_at": datetime.now().isoformat(),
        "effective_source": source,
    }
    if existing:
        content = dict(existing[0].get("content") or {})
        content.update(payload)
        db.table("settings").update({"content": content}).eq("id", existing[0]["id"]).execute()
    else:
        db.table("settings").insert({"content": {"values": {}, **payload}}).execute()


def save_settings_history(
    content: dict, reason: str | None = None, changed: list | None = None
) -> None:
    """Append a settings snapshot for audit/history.

    `reason` is the owner's rationale (the dashboard requires one before committing) and
    `changed` the structured per-key diff — together they make history answer "why did this
    dial move" without diffing blobs. Both are optional: a programmatic save has neither.
    """
    get_client().table("settings_history").insert({
        "content": content,
        "reason": reason,
        "changed": changed,
        "saved_at": datetime.now().isoformat(),
    }).execute()


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
        get_client().table("portfolio_snapshots").select("positions, snapshot_time, buying_power")
        .order("snapshot_time", desc=True).limit(1).execute().data
    )
    return data[0] if data else None


# ---------------------------------------------------------------------------
# Scorecard (src/scorecard.py) — read-only inputs + the verdict log
# ---------------------------------------------------------------------------

def get_paper_equity_series(since: str | None = None, limit: int = 2000) -> Rows:
    """Paper-book snapshots ascending, for the scorecard's NAV-vs-benchmark join.
    `market_value / total_equity` gives the invested fraction the cash-drag adjustment needs."""
    q = (get_client().table("paper_equity")
         .select("snapshot_time, nav_index, total_equity, market_value"))
    if since:
        q = q.gte("snapshot_time", since)
    return db_rows(q.order("snapshot_time", desc=False).limit(limit).execute().data)


def get_agent_snapshot_series(since: str | None = None, limit: int = 2000) -> Rows:
    """Autotrader account snapshots ascending — same shape as the paper series."""
    q = (get_client().table("agent_account_snapshots")
         .select("snapshot_time, nav_index, total_equity, market_value"))
    if since:
        q = q.gte("snapshot_time", since)
    return db_rows(q.order("snapshot_time", desc=False).limit(limit).execute().data)


def get_benchmark_series(symbol: str = "SPY", since: str | None = None, limit: int = 2000) -> Rows:
    """Daily benchmark closes ascending. The scorecard's fallback source — it prefers a full
    yfinance history, since this table only gains a row on days the daily run reached the end."""
    q = get_client().table("benchmark_prices").select("price_date, close").eq("symbol", symbol)
    if since:
        q = q.gte("price_date", since[:10])
    return db_rows(q.order("price_date", desc=False).limit(limit).execute().data)


def get_outcomes_with_decisions(since: str | None = None, limit: int = 5000) -> Rows:
    """Matured forward returns joined to the decision that produced them.

    NOTE `notes` carries a window label like "30d outcome", but it must NOT be trusted as the
    horizon: backfill_outcomes stamps it from the requested window while pricing at the day it
    happened to run, so a missed run skews the realized horizon. The scorecard derives the true
    horizon from `outcome_date - decisions.run_date` and filters on that instead.
    """
    q = (get_client().table("outcomes")
         .select("decision_id, outcome_date, pct_change, price_at_outcome, notes, "
                 "decisions(ticker, action, run_date, price_at_decision)"))
    if since:
        q = q.gte("outcome_date", since)
    return db_rows(q.order("outcome_date", desc=False).limit(limit).execute().data)


def get_agent_trades_history(limit: int = 5000) -> Rows:
    """All agentic orders (realized_pnl is stored here, unlike paper_trades)."""
    return db_rows(
        get_client().table("agent_trades").select("*")
        .order("run_date", desc=True).limit(limit).execute().data
    )


def get_decision_features_since(since: str, limit: int = 5000) -> Rows:
    """The full per-ticker feature substrate from `since` onward.

    The first reader for `decision_features` (writer-only until now). Unused by the scorecard's
    pick axis — that grades `decisions`, the calls ASTRA actually made — but the calibration
    work in docs/attribution.md needs this table, and a missing reader was the only thing
    standing between it and a query.
    """
    return db_rows(
        get_client().table("decision_features").select("*")
        .gte("run_date", since).order("run_date", desc=False).limit(limit).execute().data
    )


def save_scorecard_run(row: dict) -> None:
    """Append one scorecard verdict. The only write the scorecard is permitted to make."""
    get_client().table("scorecard_runs").insert({
        **row,
        **brain_version_fields(),
    }).execute()


def get_latest_scorecard(track: str) -> dict | None:
    """The previous verdict row for a track — drives change detection for the alert."""
    data = db_rows(
        get_client().table("scorecard_runs").select("*").eq("track", track)
        .order("scored_at", desc=True).limit(1).execute().data
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
