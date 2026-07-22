-- Migration 016: fundamentals cache — free small-cap financials from SEC EDGAR XBRL.
-- Fills the quality-filter gap where yfinance is patchy and FMP's free tier paywalls US
-- small-caps (RKLB/ASTS/CHPT/CRON). Refreshed weekly by src/fundamentals.refresh_fundamentals
-- (one companyfacts call per filer); the daily run reads it via get_cached_fundamentals.
-- One row per ticker (upsert on ticker — latest snapshot wins, not append-only). Owner-read.
-- Apply once in: Supabase Dashboard → SQL Editor.

CREATE TABLE IF NOT EXISTS public.fundamentals (
    ticker              TEXT PRIMARY KEY,
    cik                 TEXT,                    -- zero-padded SEC CIK
    source              TEXT,                    -- sec_edgar (yfinance/AV reserved for future fill)
    revenue_ttm         NUMERIC,
    revenue_growth_yoy  NUMERIC,                 -- fraction (0.15 = +15%), matches yfinance
    gross_margins       NUMERIC,                 -- fraction, matches yfinance grossMargins
    operating_margins   NUMERIC,                 -- fraction
    current_ratio       NUMERIC,
    debt_to_equity      NUMERIC,                 -- percentage-scaled (×100), matches yfinance
    period_end          TEXT,                    -- period end of the latest quarter used
    fetched_at          TIMESTAMPTZ,             -- staleness bound is enforced on read
    created_at          TIMESTAMPTZ DEFAULT now()
);

-- GRANT matrix (see docs/architecture.md): backend writes via service_role; dashboard/authenticated read-only.
GRANT SELECT ON public.fundamentals TO authenticated;
GRANT ALL    ON public.fundamentals TO service_role;

ALTER TABLE public.fundamentals ENABLE ROW LEVEL SECURITY;
CREATE POLICY "owner read fundamentals" ON public.fundamentals
    FOR SELECT TO authenticated
    USING (auth.email() = 'abhikirk@icloud.com');
