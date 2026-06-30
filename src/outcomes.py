"""
Outcome tracking: forward returns for past signals, and portfolio change detection.

Called at the end of each agent run (after Robinhood sync) to:
  - backfill_outcomes: auto-populate 30/60-day forward returns for past decisions
  - detect_portfolio_changes: diff consecutive portfolio snapshots to detect real
    trades and create user_trades_log rows for the journal feedback flow
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone

import yfinance as yf

from src.db import get_client
from src.db import rows as db_rows

logger = logging.getLogger(__name__)

_OUTCOME_WINDOWS = [30, 60]
_SIGNAL_MATCH_WINDOW_DAYS = 3
_SHARE_DELTA_NOISE = 0.01   # fractional shares below this are ignored
_TRADE_JOURNAL_TTL_DAYS = 30


def backfill_outcomes(windows_days: list[int] = _OUTCOME_WINDOWS) -> None:
    """
    Insert 30-day and 60-day forward return outcomes for past decisions.
    Idempotent — skips if an outcome for that decision + window already exists.
    Runs silently if no decisions are old enough yet.
    """
    db = get_client()
    today = date.today()

    for window in windows_days:
        cutoff = today - timedelta(days=window)
        label  = f"{window}d outcome"

        # Decisions old enough but not yet matched with this outcome window
        due = db_rows(
            db.table("decisions")
            .select("id, ticker, price_at_decision, run_date")
            .in_("action", ["buy", "sell", "watch"])
            .lte("run_date", cutoff.isoformat())
            .execute().data
        )
        if not due:
            continue

        # Decisions that already have an outcome row for this window
        due_ids = [str(r["id"]) for r in due]
        existing = db_rows(
            db.table("outcomes")
            .select("decision_id")
            .in_("decision_id", due_ids)
            .ilike("notes", f"%{label}%")
            .execute().data
        )
        done_ids = {r["decision_id"] for r in existing}
        pending  = [r for r in due if r["id"] not in done_ids]

        if not pending:
            continue

        tickers_needed = list({r["ticker"] for r in pending})
        logger.info(f"Backfilling {window}d outcomes for {len(pending)} decision(s) on {len(tickers_needed)} ticker(s)")

        prices: dict[str, float] = {}
        for ticker in tickers_needed:
            try:
                hist = yf.Ticker(ticker).history(period="2d")
                if not hist.empty:
                    prices[ticker] = float(hist["Close"].iloc[-1])
            except Exception:
                logger.warning(f"Could not fetch price for {ticker} — skipping outcome")

        rows_to_insert = []
        for dec in pending:
            ticker = dec["ticker"]
            price  = prices.get(ticker)
            if price is None:
                continue
            prior = dec.get("price_at_decision")
            pct   = round((price - float(prior)) / float(prior) * 100, 2) if prior else None
            rows_to_insert.append({
                "decision_id":     dec["id"],
                "outcome_date":    datetime.now(timezone.utc).isoformat(),
                "price_at_outcome": price,
                "pct_change":      pct,
                "notes":           label,
            })

        if rows_to_insert:
            db.table("outcomes").insert(rows_to_insert).execute()
            logger.info(f"Inserted {len(rows_to_insert)} {window}d outcome row(s)")


def detect_portfolio_changes() -> None:
    """
    Diff the two most recent portfolio_snapshots rows to detect trades.
    For each detected buy/sell, cross-references recent ASTRA signals to form
    an attribution suspicion, then upserts a user_trades_log row.
    Also expires stale pending items older than TTL.
    """
    db = get_client()

    # Expire old pending items first
    db.table("user_trades_log").update({
        "feedback_status": "expired",
    }).lt("expires_at", datetime.now(timezone.utc).isoformat()).eq(
        "feedback_status", "pending"
    ).execute()

    # Load the two most recent snapshots
    snapshots = db_rows(
        db.table("portfolio_snapshots")
        .select("positions, snapshot_time")
        .order("snapshot_time", desc=True)
        .limit(2)
        .execute().data
    )
    if len(snapshots) < 2:
        logger.info("Not enough portfolio snapshots for change detection (need ≥ 2)")
        return

    current = snapshots[0]["positions"] or {}
    prev    = snapshots[1]["positions"] or {}

    trade_date = _parse_snapshot_date(snapshots[0]["snapshot_time"])

    # Collect all tickers across both snapshots
    all_tickers = set(current) | set(prev)
    detected: list[dict] = []

    for ticker in all_tickers:
        cur_pos  = current.get(ticker, {})
        prev_pos = prev.get(ticker, {})
        cur_shares  = float(cur_pos.get("shares", 0) or 0)
        prev_shares = float(prev_pos.get("shares", 0) or 0)
        delta = cur_shares - prev_shares

        if abs(delta) < _SHARE_DELTA_NOISE:
            continue

        action = "buy" if delta > 0 else "sell"
        shares_delta = abs(delta)

        # Estimate price from position value change if available
        cur_val  = float(cur_pos.get("equity", cur_pos.get("market_value", 0)) or 0)
        prev_val = float(prev_pos.get("equity", prev_pos.get("market_value", 0)) or 0)
        price_estimated: float | None = None
        if shares_delta > 0:
            val_delta = abs(cur_val - prev_val)
            if val_delta > 0:
                price_estimated = round(val_delta / shares_delta, 4)

        detected.append({
            "ticker":          ticker,
            "action":          action,
            "shares_delta":    round(shares_delta, 6),
            "price_estimated": price_estimated,
        })

    if not detected:
        logger.info("No portfolio changes detected since last snapshot")
        return

    logger.info(f"Detected {len(detected)} portfolio change(s): {[(d['ticker'], d['action']) for d in detected]}")

    # Load recent ASTRA signals to form attribution suspicion
    window_start = (datetime.now(timezone.utc) - timedelta(days=_SIGNAL_MATCH_WINDOW_DAYS)).isoformat()
    recent_signals = db_rows(
        db.table("decisions")
        .select("id, ticker, action, run_date, price_at_decision")
        .in_("action", ["buy", "sell"])
        .gte("run_date", window_start)
        .order("run_date", desc=True)
        .execute().data
    )
    # Index by (ticker, action) → most recent matching signal
    signal_index: dict[tuple[str, str], dict] = {}
    for sig in recent_signals:
        key = (sig["ticker"], sig["action"])
        if key not in signal_index:
            signal_index[key] = sig

    expires_at = (datetime.now(timezone.utc) + timedelta(days=_TRADE_JOURNAL_TTL_DAYS)).isoformat()

    rows_to_upsert = []
    for trade in detected:
        key    = (trade["ticker"], trade["action"])
        signal = signal_index.get(key)

        if signal:
            sig_date  = str(signal["run_date"])[:10]
            sig_price = signal.get("price_at_decision")
            price_str = f" (${sig_price:.2f})" if sig_price else ""
            rows_to_upsert.append({
                "trade_date":             trade_date,
                "ticker":                 trade["ticker"],
                "action":                 trade["action"],
                "shares_delta":           trade["shares_delta"],
                "price_estimated":        trade["price_estimated"],
                "astra_signal_id":        signal["id"],
                "astra_suspicion":        True,
                "astra_suspicion_reason": f"ASTRA had a {trade['action'].upper()} signal on {sig_date}{price_str} — is this it?",
                "expires_at":             expires_at,
            })
        else:
            rows_to_upsert.append({
                "trade_date":             trade_date,
                "ticker":                 trade["ticker"],
                "action":                 trade["action"],
                "shares_delta":           trade["shares_delta"],
                "price_estimated":        trade["price_estimated"],
                "astra_signal_id":        None,
                "astra_suspicion":        False,
                "astra_suspicion_reason": "No ASTRA signal for this ticker — looks like your own call.",
                "expires_at":             expires_at,
            })

    # Upsert — UNIQUE (ticker, trade_date, action) prevents duplicates on re-runs
    db.table("user_trades_log").upsert(
        rows_to_upsert,
        on_conflict="ticker,trade_date,action",
        ignore_duplicates=True,
    ).execute()

    logger.info(f"Upserted {len(rows_to_upsert)} trade journal row(s) for {trade_date}")


def _parse_snapshot_date(snapshot_time: str) -> str:
    """Extract YYYY-MM-DD from an ISO timestamp string."""
    return str(snapshot_time)[:10]
