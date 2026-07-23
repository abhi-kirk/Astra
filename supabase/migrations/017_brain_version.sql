-- Migration 017: Brain-version stamping — tag every logged decision/trade with the exact
-- brain that produced it, so future attribution (docs/attribution.md) can segment cleanly
-- by version instead of pooling data across brain changes it can't compare.
--
-- Two identifiers travel with every row (written by src/brain/version.py, shared by both
-- tracks — paper + Autotrader):
--   brain_code_version — git SHA (which code)
--   brain_config_hash  — hash of the effective BRAIN_* tunables + conviction_primary flag
--
-- The full config behind a hash lives once in the `brain_versions` registry (keyed on the
-- hash), so the per-row tag stays a compact pair and nothing is duplicated. New columns are
-- nullable — pre-versioning rows stay NULL (an honest "before instrumentation" marker).
-- Apply once in: Supabase Dashboard → SQL Editor.

-- ── brain_versions: registry mapping a config hash → the full brain config behind it ──
CREATE TABLE IF NOT EXISTS public.brain_versions (
    config_hash   TEXT PRIMARY KEY,        -- hash of the effective BRAIN_* tunables
    code_version  TEXT,                     -- git SHA seen with this config
    config        JSONB,                    -- full BRAIN_* snapshot (what actually changed)
    first_seen_at TIMESTAMPTZ DEFAULT now()
);

GRANT SELECT ON public.brain_versions TO authenticated;
GRANT ALL    ON public.brain_versions TO service_role;

ALTER TABLE public.brain_versions ENABLE ROW LEVEL SECURITY;
CREATE POLICY "owner read brain_versions" ON public.brain_versions
    FOR SELECT TO authenticated
    USING (auth.email() = 'abhikirk@icloud.com');

-- ── Stamp the version pair onto the prediction/summary tables ──
ALTER TABLE public.decision_features
    ADD COLUMN IF NOT EXISTS brain_code_version TEXT,
    ADD COLUMN IF NOT EXISTS brain_config_hash  TEXT;

ALTER TABLE public.agent_runs
    ADD COLUMN IF NOT EXISTS brain_code_version TEXT,
    ADD COLUMN IF NOT EXISTS brain_config_hash  TEXT;

-- ── Stamp at OPEN onto the trade tables, so the opening decision's brain travels with the
--    position all the way to its close (enables realized round-trip attribution by version) ──
ALTER TABLE public.paper_trades
    ADD COLUMN IF NOT EXISTS brain_code_version TEXT,
    ADD COLUMN IF NOT EXISTS brain_config_hash  TEXT;

ALTER TABLE public.agent_trades
    ADD COLUMN IF NOT EXISTS brain_code_version TEXT,
    ADD COLUMN IF NOT EXISTS brain_config_hash  TEXT;

CREATE INDEX IF NOT EXISTS idx_decision_features_cfg ON public.decision_features(brain_config_hash);
CREATE INDEX IF NOT EXISTS idx_paper_trades_cfg      ON public.paper_trades(brain_config_hash);
CREATE INDEX IF NOT EXISTS idx_agent_trades_cfg      ON public.agent_trades(brain_config_hash);
