"""
Persistent memory layer: SQLite trade log.

Records every analysis decision with full reasoning and tracks outcomes
over time. The agent reads recent history at the start of each run to
maintain continuity across sessions.
"""

import json
import sqlite3
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parent.parent
DB_PATH = ROOT / "data" / "trading_memory.db"


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Create tables if they don't exist."""
    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS decisions (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                run_date    TEXT NOT NULL,
                ticker      TEXT NOT NULL,
                action      TEXT NOT NULL,       -- buy/sell/hold/watch/review/blocked
                reasoning   TEXT,                -- free-text agent reasoning
                signal_data TEXT,                -- JSON blob of Signal fields
                price_at_decision REAL,
                shares_held REAL,
                avg_cost    REAL,
                executed    INTEGER DEFAULT 0,   -- 0=simulation, 1=real trade
                notes       TEXT
            );

            CREATE TABLE IF NOT EXISTS outcomes (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                decision_id     INTEGER REFERENCES decisions(id),
                outcome_date    TEXT NOT NULL,
                price_at_outcome REAL,
                pct_change      REAL,            -- from price_at_decision
                notes           TEXT
            );

            CREATE TABLE IF NOT EXISTS run_summaries (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                run_date    TEXT NOT NULL,
                mode        TEXT NOT NULL,       -- simulation / live
                num_signals INTEGER,
                buy_signals TEXT,                -- JSON list of tickers
                summary     TEXT,                -- agent's free-text summary
                raw_output  TEXT                 -- full JSON of all signals
            );

            CREATE TABLE IF NOT EXISTS conviction_snapshots (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                captured_at TEXT NOT NULL,
                content     TEXT NOT NULL        -- JSON snapshot of convictions.json
            );
        """)


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
) -> int:
    """Insert a decision record. Returns the new row id."""
    init_db()
    run_date = run_date or datetime.now().isoformat()
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO decisions
               (run_date, ticker, action, reasoning, signal_data,
                price_at_decision, shares_held, avg_cost, executed)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                run_date, ticker, action, reasoning,
                json.dumps(signal_data),
                price_at_decision, shares_held, avg_cost,
                int(executed),
            ),
        )
        return cur.lastrowid


def log_run_summary(
    mode: str,
    signals: list[dict],
    summary: str,
    run_date: str | None = None,
):
    init_db()
    run_date = run_date or datetime.now().isoformat()
    buy_signals = [s["ticker"] for s in signals if s.get("action") == "buy"]
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO run_summaries
               (run_date, mode, num_signals, buy_signals, summary, raw_output)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                run_date, mode, len(signals),
                json.dumps(buy_signals), summary,
                json.dumps(signals),
            ),
        )


def record_outcome(
    decision_id: int,
    price_at_outcome: float,
    notes: str = "",
):
    """Update a past decision with its observed outcome."""
    init_db()
    with get_conn() as conn:
        row = conn.execute(
            "SELECT price_at_decision FROM decisions WHERE id = ?", (decision_id,)
        ).fetchone()
        if not row or not row["price_at_decision"]:
            pct_change = None
        else:
            pct_change = round(
                (price_at_outcome - row["price_at_decision"]) / row["price_at_decision"] * 100, 2
            )
        conn.execute(
            """INSERT INTO outcomes (decision_id, outcome_date, price_at_outcome, pct_change, notes)
               VALUES (?, ?, ?, ?, ?)""",
            (decision_id, datetime.now().isoformat(), price_at_outcome, pct_change, notes),
        )


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------

def get_recent_decisions(n: int = 30) -> list[dict]:
    """Retrieve the n most recent decisions for agent context."""
    init_db()
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT d.*, o.price_at_outcome, o.pct_change as outcome_pct
               FROM decisions d
               LEFT JOIN outcomes o ON o.decision_id = d.id
               ORDER BY d.run_date DESC LIMIT ?""",
            (n,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_ticker_history(ticker: str) -> list[dict]:
    """All decisions for a specific ticker, oldest first."""
    init_db()
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT d.*, o.price_at_outcome, o.pct_change as outcome_pct
               FROM decisions d
               LEFT JOIN outcomes o ON o.decision_id = d.id
               WHERE d.ticker = ?
               ORDER BY d.run_date ASC""",
            (ticker,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_run_summaries(n: int = 10) -> list[dict]:
    """Recent run summaries for dashboard / context."""
    init_db()
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM run_summaries ORDER BY run_date DESC LIMIT ?", (n,)
        ).fetchall()
    return [dict(r) for r in rows]


def build_agent_context_summary(max_decisions: int = 20) -> str:
    """
    Compact text summary of recent history for injecting into the agent's context.
    Designed to be token-efficient.
    """
    recent = get_recent_decisions(max_decisions)
    if not recent:
        return "No prior decisions on record. This is the first analysis run."

    lines = ["=== RECENT DECISION HISTORY ==="]
    for d in recent:
        outcome = ""
        if d.get("outcome_pct") is not None:
            outcome = f" → outcome: {d['outcome_pct']:+.1f}%"
        lines.append(
            f"{d['run_date'][:10]}  {d['ticker']:8s}  {d['action']:8s}  "
            f"@${d['price_at_decision'] or 0:.2f}{outcome}"
        )
        if d.get("reasoning"):
            lines.append(f"  Reasoning: {d['reasoning'][:120]}")

    summaries = get_run_summaries(3)
    if summaries:
        lines.append("\n=== RECENT RUN SUMMARIES ===")
        for s in summaries:
            lines.append(f"{s['run_date'][:10]}: {s['summary'][:200]}")

    return "\n".join(lines)
