-- Migration 023: questions ASTRA asks on Telegram when Abhi's own trades
-- disagree with the conviction list, plus the cursor for reading his replies.
--
-- owner_prompts  — one row per (ticker, kind). kind is drop (he sold / no longer holds)
--                  or add (he bought a name that is not on the list). status leaves
--                  'pending' when he replies drop/keep/add/skip.
-- telegram_cursor — single row, the getUpdates offset so a reply is applied once.
--
-- Service role writes these from the daily run. The owner can read them.
-- Apply once in: Supabase Dashboard → SQL Editor.

CREATE TABLE IF NOT EXISTS public.owner_prompts (
    id           BIGSERIAL PRIMARY KEY,
    ticker       TEXT NOT NULL,
    kind         TEXT NOT NULL CHECK (kind IN ('drop', 'add')),
    status       TEXT NOT NULL DEFAULT 'pending',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at  TIMESTAMPTZ,
    UNIQUE (ticker, kind)
);

GRANT SELECT ON public.owner_prompts TO authenticated;
GRANT ALL    ON public.owner_prompts TO service_role;
GRANT USAGE, SELECT ON SEQUENCE public.owner_prompts_id_seq TO service_role;

ALTER TABLE public.owner_prompts ENABLE ROW LEVEL SECURITY;
CREATE POLICY "owner read owner_prompts" ON public.owner_prompts
    FOR SELECT TO authenticated
    USING (auth.email() = 'abhikirk@icloud.com');

CREATE TABLE IF NOT EXISTS public.telegram_cursor (
    id             INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    update_offset  BIGINT NOT NULL DEFAULT 0
);
INSERT INTO public.telegram_cursor (id) VALUES (1) ON CONFLICT (id) DO NOTHING;

GRANT SELECT ON public.telegram_cursor TO authenticated;
GRANT ALL    ON public.telegram_cursor TO service_role;

ALTER TABLE public.telegram_cursor ENABLE ROW LEVEL SECURITY;
CREATE POLICY "owner read telegram_cursor" ON public.telegram_cursor
    FOR SELECT TO authenticated
    USING (auth.email() = 'abhikirk@icloud.com');
