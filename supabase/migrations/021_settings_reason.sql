-- Migration 021: require and record WHY a tuning change was made.
--
-- Migration 020 gave settings_history the full override blob on every edit. That answers
-- "what changed and when" but not "why", which is the part worth having six months later
-- when a stretch of performance has to be explained against the dials that produced it.
--
-- The dashboard now requires a reason before it will commit a change, and writes it here
-- alongside a structured per-key diff, so history is queryable without diffing JSON blobs:
--
--   SELECT saved_at, reason, changed FROM settings_history ORDER BY saved_at DESC;
--
-- Both columns are nullable: the Python save path (scripts/back-fill) may write without a
-- reason, and rows written before this migration have none.
--
-- Apply once in: Supabase Dashboard → SQL Editor.

ALTER TABLE public.settings_history
    ADD COLUMN IF NOT EXISTS reason  TEXT,
    ADD COLUMN IF NOT EXISTS changed JSONB;

COMMENT ON COLUMN public.settings_history.reason IS
    'Owner-supplied rationale for the change. Required by the dashboard commit flow.';
COMMENT ON COLUMN public.settings_history.changed IS
    'Structured diff: [{key, label, from, to}] — the keys this edit actually moved.';
