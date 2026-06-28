-- Migration 004: exploration_candidates table
-- Stores tickers surfaced by ASTRA's weekly exploration run (Friday 9pm ET).
-- Tracks each candidate through its lifecycle: on_radar → paper_trading → graduated | rejected.
-- Run in: Supabase Dashboard → SQL Editor

CREATE TABLE IF NOT EXISTS exploration_candidates (
    id                BIGSERIAL PRIMARY KEY,
    ticker            TEXT NOT NULL UNIQUE,  -- one row per ticker; upsert on re-discovery
    source_theme      TEXT NOT NULL,         -- which conviction theme search found it
    rationale         TEXT,                  -- why it fits the theme (Claude explanation)
    quality_summary   TEXT,                  -- revenue/margin/cash assessment
    analyst_summary   TEXT,                  -- analyst consensus from FMP (or 'none found')
    claude_conviction TEXT,                  -- 'high' | 'medium' | 'low'
    status            TEXT NOT NULL DEFAULT 'on_radar',
    -- on_radar:      surfaced by exploration, awaiting daily screening signal
    -- paper_trading: daily run fired BUY signal, virtual position is open
    -- graduated:     detected in real Robinhood portfolio (Robinhood sync sets this)
    -- rejected:      user dismissed from dashboard
    discovered_at     TIMESTAMPTZ DEFAULT NOW(),
    updated_at        TIMESTAMPTZ DEFAULT NOW()
);

ALTER TABLE exploration_candidates ENABLE ROW LEVEL SECURITY;

-- Public read: dashboard shows On Radar swim lane to all visitors
-- (no financial data — just ticker, theme, and Claude's rationale)
CREATE POLICY "public read exploration" ON exploration_candidates
    FOR SELECT USING (true);

-- Write restricted to service role only (exploration pipeline + daily agent)
-- No explicit INSERT/UPDATE policy needed — service role bypasses RLS by default
