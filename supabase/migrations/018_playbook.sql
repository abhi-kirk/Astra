-- Migration 018: Playbook + contextual reasoning layer.
--
-- Strategies become first-class editable data — a "Playbook" of plays that mirror convictions
-- exactly (single-row JSONB blob + append-only history). The contextual reasoning layer
-- (docs/contextual_reasoning.md) lets bounded LLM judgment feed the deterministic pipeline as
-- clamped, logged, structured proposals — never decisions. Three tables:
--   plays             — the live Playbook (single-row JSONB content; upsert), mirrors `convictions`
--   plays_history     — append-only full-blob snapshot on every edit, mirrors `conviction_history`
--   contextual_inputs — one row per structured proposal per run, sibling to `decision_features`
-- Apply once in: Supabase Dashboard → SQL Editor.

-- ── plays: the live Playbook — a single JSONB row, upsert-overwritten (like convictions) ──
CREATE TABLE IF NOT EXISTS public.plays (
    id          BIGSERIAL PRIMARY KEY,
    content     JSONB NOT NULL,
    updated_at  TIMESTAMPTZ DEFAULT now(),
    updated_by  TEXT DEFAULT 'claude'
);

-- Anon can read the Playbook (no personal financial data — same posture as `convictions`,
-- so the future dashboard plays-drawer can read it with the anon key).
GRANT SELECT ON public.plays TO anon;
GRANT SELECT ON public.plays TO authenticated;
GRANT ALL    ON public.plays TO service_role;
GRANT USAGE, SELECT ON SEQUENCE public.plays_id_seq TO service_role;

ALTER TABLE public.plays ENABLE ROW LEVEL SECURITY;
CREATE POLICY "anon read plays"  ON public.plays FOR SELECT TO anon          USING (true);
-- Owner may also write directly (the future dashboard drawer edits the live row like convictions).
CREATE POLICY "owner rw plays"   ON public.plays FOR ALL   TO authenticated
    USING (auth.email() = 'abhikirk@icloud.com')
    WITH CHECK (auth.email() = 'abhikirk@icloud.com');

-- ── plays_history: append-only snapshot of the full Playbook on every edit (audit) ──
CREATE TABLE IF NOT EXISTS public.plays_history (
    id        BIGSERIAL PRIMARY KEY,
    saved_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    content   JSONB       NOT NULL
);

-- Owner (dashboard) may append + read; service_role full access — mirrors conviction_history.
GRANT SELECT, INSERT ON public.plays_history TO authenticated;
GRANT ALL           ON public.plays_history TO service_role;
GRANT USAGE, SELECT ON SEQUENCE public.plays_history_id_seq TO authenticated;
GRANT USAGE, SELECT ON SEQUENCE public.plays_history_id_seq TO service_role;

ALTER TABLE public.plays_history ENABLE ROW LEVEL SECURITY;
CREATE POLICY "owner rw plays_history" ON public.plays_history
    FOR ALL TO authenticated
    USING (auth.email() = 'abhikirk@icloud.com')
    WITH CHECK (auth.email() = 'abhikirk@icloud.com');

-- ── contextual_inputs: one row per structured proposal per run (append-only training/audit) ──
-- The contextual layer emits bounded proposals (size_multiplier / conviction_nudge / thesis_flag)
-- tagged with the play that produced them; `applied` records whether the proposal survived the
-- code clamps + hard invariants, and `source` splits deterministic-quant from LLM proposals — so
-- attribution (docs/attribution.md) can score each play, proposed-vs-applied, quant-vs-LLM.
CREATE TABLE IF NOT EXISTS public.contextual_inputs (
    id                 BIGSERIAL PRIMARY KEY,
    run_date           TIMESTAMPTZ NOT NULL,
    ticker             TEXT        NOT NULL,
    play               TEXT        NOT NULL,   -- which play fired
    kind               TEXT        NOT NULL,   -- size_multiplier / conviction_nudge / thesis_flag
    value              NUMERIC(10,6),          -- the proposed (pre-clamp) delta/multiplier
    applied            BOOLEAN,                -- did it survive clamps + hard invariants
    source             TEXT,                   -- quant / llm
    rationale          TEXT,                   -- cited reasoning (attribution + the note)
    evidence           JSONB,                  -- what the layer gathered to support it
    brain_code_version TEXT,
    brain_config_hash  TEXT,
    created_at         TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_contextual_inputs_run    ON public.contextual_inputs(run_date DESC);
CREATE INDEX IF NOT EXISTS idx_contextual_inputs_ticker ON public.contextual_inputs(ticker);
CREATE INDEX IF NOT EXISTS idx_contextual_inputs_play   ON public.contextual_inputs(play);
CREATE INDEX IF NOT EXISTS idx_contextual_inputs_cfg    ON public.contextual_inputs(brain_config_hash);

GRANT SELECT ON public.contextual_inputs TO authenticated;
GRANT ALL    ON public.contextual_inputs TO service_role;
GRANT USAGE, SELECT ON SEQUENCE public.contextual_inputs_id_seq TO service_role;

ALTER TABLE public.contextual_inputs ENABLE ROW LEVEL SECURITY;
CREATE POLICY "owner read contextual_inputs" ON public.contextual_inputs
    FOR SELECT TO authenticated
    USING (auth.email() = 'abhikirk@icloud.com');
