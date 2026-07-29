-- Migration 020: dashboard-editable settings overrides.
--
-- ASTRA's tunables live in src/config.py (values) + src/settings_schema.py (meaning, tier,
-- safe range). This adds the third layer: a saved override map you edit from the dashboard,
-- applied at run start by src/settings.py with precedence
--     code default → environment → this table.
--
-- Same single-row-JSONB + append-only-history shape as `convictions` and `plays`:
--   settings          — the live override map (single JSONB row; {"values": {"brain.f_global": 0.12}})
--   settings_history   — append-only full-blob snapshot on every edit
--
-- Secrets are NEVER stored here — src/settings.py refuses any key not marked editable in the
-- registry, so a secret, a plumbing constant or a locked hard rule cannot be set from the UI
-- even if someone writes one into the row by hand.
--
-- Apply once in: Supabase Dashboard → SQL Editor.

-- ── settings: the live override map ───────────────────────────────────────────
CREATE TABLE IF NOT EXISTS public.settings (
    id          BIGSERIAL PRIMARY KEY,
    content     JSONB NOT NULL,
    updated_at  TIMESTAMPTZ DEFAULT now(),
    updated_by  TEXT DEFAULT 'dashboard'
);

-- Owner-only: unlike convictions/plays, this row governs real-money behaviour, so anon gets
-- no read. The dashboard reads it with the authenticated (logged-in owner) session.
GRANT SELECT, INSERT, UPDATE ON public.settings TO authenticated;
GRANT ALL    ON public.settings TO service_role;
GRANT USAGE, SELECT ON SEQUENCE public.settings_id_seq TO authenticated;
GRANT USAGE, SELECT ON SEQUENCE public.settings_id_seq TO service_role;

ALTER TABLE public.settings ENABLE ROW LEVEL SECURITY;
CREATE POLICY "owner rw settings" ON public.settings
    FOR ALL TO authenticated
    USING (auth.email() = 'abhikirk@icloud.com')
    WITH CHECK (auth.email() = 'abhikirk@icloud.com');

-- ── settings_history: append-only audit of every edit ─────────────────────────
-- Pairs with the brain_versions registry: brain_config_hash already changes when a BRAIN_*
-- value changes, so these snapshots explain WHY a hash changed and when.
CREATE TABLE IF NOT EXISTS public.settings_history (
    id        BIGSERIAL PRIMARY KEY,
    saved_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    content   JSONB       NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_settings_history_saved ON public.settings_history(saved_at DESC);

GRANT SELECT, INSERT ON public.settings_history TO authenticated;
GRANT ALL           ON public.settings_history TO service_role;
GRANT USAGE, SELECT ON SEQUENCE public.settings_history_id_seq TO authenticated;
GRANT USAGE, SELECT ON SEQUENCE public.settings_history_id_seq TO service_role;

ALTER TABLE public.settings_history ENABLE ROW LEVEL SECURITY;
CREATE POLICY "owner rw settings_history" ON public.settings_history
    FOR ALL TO authenticated
    USING (auth.email() = 'abhikirk@icloud.com')
    WITH CHECK (auth.email() = 'abhikirk@icloud.com');
