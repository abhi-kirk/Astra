-- Migration 001: paper_trades table
-- Phase 1.5 — track ASTRA's virtual BUY trades for performance measurement.
-- Run in: Supabase Dashboard → SQL Editor

CREATE TABLE IF NOT EXISTS paper_trades (
    id                  BIGSERIAL PRIMARY KEY,
    ticker              TEXT NOT NULL,
    action              TEXT NOT NULL,          -- buy / sell (sell = signal flip or manual close)
    price_at_signal     NUMERIC(18,4) NOT NULL,
    virtual_shares      NUMERIC(18,6) NOT NULL, -- virtual_cost / price_at_signal
    virtual_cost        NUMERIC(18,4) NOT NULL, -- fixed $1,000 per BUY signal
    run_date            TIMESTAMPTZ NOT NULL,
    signal_data         JSONB,
    is_open             BOOLEAN DEFAULT TRUE,   -- FALSE once position is closed
    closed_at           TIMESTAMPTZ,
    close_price         NUMERIC(18,4),
    created_at          TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_paper_trades_ticker ON paper_trades(ticker);
CREATE INDEX IF NOT EXISTS idx_paper_trades_open   ON paper_trades(is_open) WHERE is_open = TRUE;
CREATE INDEX IF NOT EXISTS idx_paper_trades_date   ON paper_trades(run_date DESC);

ALTER TABLE paper_trades ENABLE ROW LEVEL SECURITY;

CREATE POLICY "owner read paper_trades" ON paper_trades
    FOR SELECT TO authenticated USING (auth.email() = 'abhikirk@icloud.com');

GRANT SELECT ON paper_trades TO authenticated;
GRANT ALL    ON paper_trades TO service_role;
GRANT USAGE, SELECT ON SEQUENCE paper_trades_id_seq TO service_role;
