-- Migration 019: paper_trims — persist parabolic-trim events so the Autotrader can mirror them.
-- A parabolic trim (exit layer 3) sells ~⅓ of a paper lot and keeps the runner: close_paper_trade
-- does an in-place UPDATE of the open lot (is_open stays true) and writes no record, so the trim was
-- invisible to the close-based sell mirror — the real sleeve rode blow-offs the paper track trimmed.
-- This append-only event log (one aggregate row per ticker per run) is the missing mirror source and
-- doubles as trim attribution. The Autotrader mirrors the ACTION (sell `fraction` of its own holding),
-- not the paper dollars — basis-independent, matching how buys mirror the decision not the paper size.
-- Apply once in: Supabase Dashboard → SQL Editor.

CREATE TABLE IF NOT EXISTS public.paper_trims (
    id             BIGSERIAL PRIMARY KEY,
    ticker         TEXT        NOT NULL,
    run_date       TIMESTAMPTZ NOT NULL,        -- when the trim fired (Autotrader mirrors by calendar day)
    close_price    NUMERIC     NOT NULL,        -- price the paper trim booked at
    fraction       NUMERIC     NOT NULL,        -- portion of the position sold (0,1) — the mirror's sell size
    realized_pnl   NUMERIC,                     -- Σ realized $ across the trimmed lots
    sold_shares    NUMERIC,                     -- Σ virtual shares sold (paper bookkeeping)
    close_reason   TEXT,                        -- parabolic_trim (reserved for future partial-exit reasons)
    mirrored       BOOLEAN     DEFAULT FALSE,   -- reserved: set once the Autotrader has mirrored this event
    brain_code_version TEXT,
    brain_config_hash  TEXT,
    created_at     TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_paper_trims_run    ON public.paper_trims(run_date DESC);
CREATE INDEX IF NOT EXISTS idx_paper_trims_ticker ON public.paper_trims(ticker);

-- GRANT matrix (see docs/architecture.md): backend writes via service_role; dashboard reads.
GRANT SELECT ON public.paper_trims TO authenticated;
GRANT ALL    ON public.paper_trims TO service_role;
GRANT USAGE, SELECT ON SEQUENCE public.paper_trims_id_seq TO service_role;

ALTER TABLE public.paper_trims ENABLE ROW LEVEL SECURITY;
CREATE POLICY "owner read paper_trims" ON public.paper_trims
    FOR SELECT TO authenticated
    USING (auth.email() = 'abhikirk@icloud.com');
