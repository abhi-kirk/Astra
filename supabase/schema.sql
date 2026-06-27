-- ASTRA — Supabase schema + auth setup
-- Run in: Supabase Dashboard → SQL Editor
-- Represents the complete desired state; safe to re-run (uses IF NOT EXISTS / OR REPLACE).

-- ============================================================
-- TABLES
-- ============================================================

-- Full trade history imported from Robinhood CSV (one-time seed via supabase/seed.py)
CREATE TABLE IF NOT EXISTS trades (
    id              BIGSERIAL PRIMARY KEY,
    activity_date   DATE,
    process_date    DATE,
    settle_date     DATE,
    ticker          TEXT,
    description     TEXT,
    trans_code      TEXT,          -- Buy, Sell, CDIV, ACH, etc.
    quantity        NUMERIC(18,6),
    price           NUMERIC(18,4),
    amount          NUMERIC(18,4),
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- Convictions store — replaces convictions.json on disk
CREATE TABLE IF NOT EXISTS convictions (
    id          BIGSERIAL PRIMARY KEY,
    content     JSONB NOT NULL,
    updated_at  TIMESTAMPTZ DEFAULT NOW(),
    updated_by  TEXT DEFAULT 'claude'
);

-- Portfolio snapshots from Robinhood MCP reads
CREATE TABLE IF NOT EXISTS portfolio_snapshots (
    id             BIGSERIAL PRIMARY KEY,
    snapshot_time  TIMESTAMPTZ NOT NULL,
    source         TEXT DEFAULT 'robinhood_mcp',
    positions      JSONB NOT NULL,
    created_at     TIMESTAMPTZ DEFAULT NOW()
);

-- Every analysis signal with reasoning (private — auth only)
CREATE TABLE IF NOT EXISTS decisions (
    id                  BIGSERIAL PRIMARY KEY,
    run_date            TIMESTAMPTZ NOT NULL,
    ticker              TEXT NOT NULL,
    action              TEXT NOT NULL,  -- buy/sell/hold/watch/review/blocked
    reasoning           TEXT,
    signal_data         JSONB,
    price_at_decision   NUMERIC(18,4),
    shares_held         NUMERIC(18,6),
    avg_cost            NUMERIC(18,4),
    executed            BOOLEAN DEFAULT FALSE,
    notes               TEXT,
    created_at          TIMESTAMPTZ DEFAULT NOW()
);

-- Outcome tracking for past decisions
CREATE TABLE IF NOT EXISTS outcomes (
    id                  BIGSERIAL PRIMARY KEY,
    decision_id         BIGINT REFERENCES decisions(id) ON DELETE CASCADE,
    outcome_date        TIMESTAMPTZ NOT NULL,
    price_at_outcome    NUMERIC(18,4),
    pct_change          NUMERIC(8,2),
    notes               TEXT,
    created_at          TIMESTAMPTZ DEFAULT NOW()
);

-- Weekly run audit log
-- raw_output: full data including advisor note (private, auth only)
-- public_output: scrubbed version safe for anon visitors (no personal financial data)
CREATE TABLE IF NOT EXISTS run_summaries (
    id            BIGSERIAL PRIMARY KEY,
    run_date      TIMESTAMPTZ NOT NULL,
    mode          TEXT NOT NULL,        -- simulation / live
    num_signals   INTEGER,
    buy_signals   JSONB,                -- ["RKLB", "ASTS"]
    summary       TEXT,
    raw_output    JSONB,
    public_output JSONB,
    created_at    TIMESTAMPTZ DEFAULT NOW()
);

-- ============================================================
-- INDEXES
-- ============================================================

CREATE INDEX IF NOT EXISTS idx_trades_ticker       ON trades(ticker);
CREATE INDEX IF NOT EXISTS idx_trades_date         ON trades(activity_date);
CREATE INDEX IF NOT EXISTS idx_decisions_ticker    ON decisions(ticker);
CREATE INDEX IF NOT EXISTS idx_decisions_run_date  ON decisions(run_date DESC);
CREATE INDEX IF NOT EXISTS idx_decisions_action    ON decisions(action);
CREATE INDEX IF NOT EXISTS idx_run_summaries_date  ON run_summaries(run_date DESC);
CREATE INDEX IF NOT EXISTS idx_snapshots_time      ON portfolio_snapshots(snapshot_time DESC);

-- ============================================================
-- ROW LEVEL SECURITY
-- ============================================================
-- Service role key (Python backend + GitHub Actions) bypasses RLS entirely.
-- Anon key (public dashboard visitors) gets read-only access via RPC only.
-- Authenticated users (owner, via Supabase Auth) get full read access.

ALTER TABLE trades             ENABLE ROW LEVEL SECURITY;
ALTER TABLE convictions        ENABLE ROW LEVEL SECURITY;
ALTER TABLE decisions          ENABLE ROW LEVEL SECURITY;
ALTER TABLE outcomes           ENABLE ROW LEVEL SECURITY;
ALTER TABLE run_summaries      ENABLE ROW LEVEL SECURITY;
ALTER TABLE portfolio_snapshots ENABLE ROW LEVEL SECURITY;

-- Public tables: anon can read convictions and trades (no personal financial data)
CREATE POLICY "anon read trades"       ON trades       FOR SELECT TO anon USING (true);
CREATE POLICY "anon read convictions"  ON convictions  FOR SELECT TO anon USING (true);
CREATE POLICY "anon read outcomes"     ON outcomes     FOR SELECT TO anon USING (true);

-- Private tables: only the owner account can read (enforced by email)
-- Replace 'your@email.com' with the email used in Supabase Auth → Users
CREATE POLICY "owner read run_summaries" ON run_summaries
    FOR SELECT TO authenticated USING (auth.email() = 'abhikirk@icloud.com');

CREATE POLICY "owner read decisions" ON decisions
    FOR SELECT TO authenticated USING (auth.email() = 'abhikirk@icloud.com');

-- portfolio_snapshots: owner only
CREATE POLICY "owner read snapshots" ON portfolio_snapshots
    FOR SELECT TO authenticated USING (auth.email() = 'abhikirk@icloud.com');

-- ============================================================
-- GRANTS
-- ============================================================

-- Service role: full access (bypasses RLS anyway, but be explicit)
GRANT ALL ON ALL TABLES IN SCHEMA public TO service_role;
GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO service_role;

-- Authenticated (owner): full read on all tables
GRANT SELECT ON run_summaries      TO authenticated;
GRANT SELECT ON decisions          TO authenticated;
GRANT SELECT ON trades             TO authenticated;
GRANT SELECT ON convictions        TO authenticated;
GRANT SELECT ON outcomes           TO authenticated;
GRANT SELECT ON portfolio_snapshots TO authenticated;

-- Anon: read-only on non-sensitive tables only
-- run_summaries and decisions are NOT granted to anon directly —
-- public data is served via the get_latest_run_public() RPC function below.
GRANT SELECT ON convictions TO anon;
GRANT SELECT ON trades      TO anon;
GRANT SELECT ON outcomes    TO anon;

-- ============================================================
-- PUBLIC RPC — scrubbed data for unauthenticated visitors
-- ============================================================
-- SECURITY DEFINER: runs as the function owner, bypassing RLS on run_summaries.
-- Only exposes public_output (no advisor note, no personal financial data).

CREATE OR REPLACE FUNCTION get_latest_run_public()
RETURNS jsonb
LANGUAGE sql
SECURITY DEFINER
SET search_path = public
AS $$
    SELECT COALESCE(public_output, '{}'::jsonb)
    FROM run_summaries
    WHERE public_output IS NOT NULL
    ORDER BY id DESC
    LIMIT 1;
$$;

GRANT EXECUTE ON FUNCTION get_latest_run_public() TO anon;
GRANT EXECUTE ON FUNCTION get_latest_run_public() TO authenticated;
