-- Migration 024: rotation_intents — a sell placed today so a specific buy can be funded
-- once the cash settles (T+1 on the agentic cash account). The Autotrader writes these.
-- The dashboard reads pending rows so a sell is labelled as funding a buy, not an exit.
-- Apply once in: Supabase Dashboard → SQL Editor.
-- (The capital-rotation spec called this migration 023. 023 is owner_prompts.)

CREATE TABLE IF NOT EXISTS public.rotation_intents (
    id                 BIGSERIAL PRIMARY KEY,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    book               TEXT        NOT NULL,          -- 'autotrader' (advisor is not wired)
    status             TEXT        NOT NULL,          -- pending | filled | expired
    sell_ticker        TEXT        NOT NULL,
    sell_fraction      NUMERIC,                       -- null or 1 = full exit; else a trim
    sell_trade_id      BIGINT,                        -- agent_trades.id of the sell
    buy_ticker         TEXT        NOT NULL,
    expected_proceeds  NUMERIC,
    challenger_score   NUMERIC     NOT NULL,          -- OwnScore at the decision, not at the fill
    incumbent_score    NUMERIC     NOT NULL,
    margin             NUMERIC     NOT NULL,
    shortfall          NUMERIC,
    reason             TEXT,
    llm_influenced     BOOLEAN     NOT NULL DEFAULT FALSE,
    expires_on         DATE        NOT NULL,
    filled_trade_id    BIGINT,
    brain_code_version TEXT,
    brain_config_hash  TEXT
);
CREATE INDEX IF NOT EXISTS idx_rotation_intents_status ON public.rotation_intents(book, status, created_at DESC);

-- GRANT matrix (see docs/architecture.md): backend writes via service_role; owner reads.
GRANT SELECT ON public.rotation_intents TO authenticated;
GRANT ALL    ON public.rotation_intents TO service_role;
GRANT USAGE, SELECT ON SEQUENCE public.rotation_intents_id_seq TO service_role;

ALTER TABLE public.rotation_intents ENABLE ROW LEVEL SECURITY;
CREATE POLICY "owner read rotation_intents" ON public.rotation_intents
    FOR SELECT TO authenticated
    USING (auth.email() = 'abhikirk@icloud.com');
