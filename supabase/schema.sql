-- ASTRA schema
-- Run this in: Supabase Dashboard → SQL Editor
-- Or via CLI: supabase db push

-- ============================================================
-- TABLES
-- ============================================================

-- Full trade history imported from Robinhood CSV (one-time seed)
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

-- Convictions store — replaces convictions.json
-- Single row updated in place; full history via conviction_snapshots
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
    positions      JSONB NOT NULL,   -- {ticker: {shares, current_price, avg_cost, ...}}
    created_at     TIMESTAMPTZ DEFAULT NOW()
);

-- Every analysis signal with reasoning
CREATE TABLE IF NOT EXISTS decisions (
    id                  BIGSERIAL PRIMARY KEY,
    run_date            TIMESTAMPTZ NOT NULL,
    ticker              TEXT NOT NULL,
    action              TEXT NOT NULL,  -- buy/sell/hold/watch/review/blocked
    reasoning           TEXT,
    signal_data         JSONB,          -- full Signal dataclass
    price_at_decision   NUMERIC(18,4),
    shares_held         NUMERIC(18,6),
    avg_cost            NUMERIC(18,4),
    executed            BOOLEAN DEFAULT FALSE,
    notes               TEXT,
    created_at          TIMESTAMPTZ DEFAULT NOW()
);

-- Outcome tracking for closed/reviewed decisions
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
CREATE TABLE IF NOT EXISTS run_summaries (
    id           BIGSERIAL PRIMARY KEY,
    run_date     TIMESTAMPTZ NOT NULL,
    mode         TEXT NOT NULL,        -- simulation / live
    num_signals  INTEGER,
    buy_signals  JSONB,                -- ["RKLB", "ASTS"]
    summary      TEXT,
    raw_output   JSONB,
    created_at   TIMESTAMPTZ DEFAULT NOW()
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
-- RLS is enabled automatically on all tables (we checked that box at project creation).
-- Service role key bypasses RLS entirely (used by Python backend + GitHub Actions).
-- Anon key (used by dashboard) gets explicit read-only access below.

-- trades: dashboard can read for cost basis display
ALTER TABLE trades ENABLE ROW LEVEL SECURITY;
CREATE POLICY "anon read trades" ON trades FOR SELECT TO anon USING (true);

-- convictions: dashboard can read current thesis
ALTER TABLE convictions ENABLE ROW LEVEL SECURITY;
CREATE POLICY "anon read convictions" ON convictions FOR SELECT TO anon USING (true);

-- decisions: dashboard reads all signals
ALTER TABLE decisions ENABLE ROW LEVEL SECURITY;
CREATE POLICY "anon read decisions" ON decisions FOR SELECT TO anon USING (true);

-- outcomes: dashboard reads outcomes
ALTER TABLE outcomes ENABLE ROW LEVEL SECURITY;
CREATE POLICY "anon read outcomes" ON outcomes FOR SELECT TO anon USING (true);

-- run_summaries: dashboard reads run history
ALTER TABLE run_summaries ENABLE ROW LEVEL SECURITY;
CREATE POLICY "anon read run_summaries" ON run_summaries FOR SELECT TO anon USING (true);

-- portfolio_snapshots: NO anon read — positions are private
ALTER TABLE portfolio_snapshots ENABLE ROW LEVEL SECURITY;
-- (no anon policy = anon gets nothing; service role still has full access)

-- ============================================================
-- GRANTS (run if service_role gets permission denied errors)
-- ============================================================
GRANT ALL ON ALL TABLES IN SCHEMA public TO service_role;
GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO service_role;
