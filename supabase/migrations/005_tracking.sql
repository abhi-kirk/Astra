-- Migration 005: tracking tables for signal outcomes, trade journal, conviction history, advisor ratings
-- Apply once in Supabase Dashboard → SQL Editor

-- decisions: track whether user acted on a signal
ALTER TABLE public.decisions
  ADD COLUMN IF NOT EXISTS user_acted  BOOLEAN,
  ADD COLUMN IF NOT EXISTS acted_at    TIMESTAMPTZ;
-- No new grants needed — existing column-level grants apply

-- user_trades_log: detected portfolio trades with ASTRA attribution suspicion
CREATE TABLE IF NOT EXISTS public.user_trades_log (
  id                        SERIAL PRIMARY KEY,
  detected_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
  trade_date                DATE        NOT NULL,
  ticker                    TEXT        NOT NULL,
  action                    TEXT        NOT NULL CHECK (action IN ('buy', 'sell')),
  shares_delta              NUMERIC     NOT NULL,
  price_estimated           NUMERIC,
  astra_signal_id           INTEGER     REFERENCES decisions(id),
  astra_suspicion           BOOLEAN     NOT NULL DEFAULT false,
  astra_suspicion_reason    TEXT,
  from_astra_recommendation BOOLEAN,
  user_reason               TEXT,
  feedback_status           TEXT        NOT NULL DEFAULT 'pending'
                            CHECK (feedback_status IN ('pending', 'submitted', 'expired')),
  feedback_given_at         TIMESTAMPTZ,
  expires_at                TIMESTAMPTZ NOT NULL,
  UNIQUE (ticker, trade_date, action)
);
GRANT SELECT, INSERT, UPDATE ON public.user_trades_log TO service_role;
GRANT USAGE, SELECT ON SEQUENCE public.user_trades_log_id_seq TO service_role;
GRANT SELECT, UPDATE ON public.user_trades_log TO authenticated;
-- No anon grant — personal trade data only visible to authenticated owner

-- Supabase auto-enables RLS; add owner-only policy so the dashboard can read/update
ALTER TABLE public.user_trades_log ENABLE ROW LEVEL SECURITY;
CREATE POLICY "owner rw user_trades_log" ON public.user_trades_log
  FOR ALL TO authenticated
  USING (auth.email() = 'abhikirk@icloud.com')
  WITH CHECK (auth.email() = 'abhikirk@icloud.com');

-- conviction_history: append-only snapshots of the full convictions JSON
CREATE TABLE IF NOT EXISTS public.conviction_history (
  id        SERIAL PRIMARY KEY,
  saved_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  content   JSONB       NOT NULL
);
GRANT SELECT, INSERT ON public.conviction_history TO service_role;
GRANT USAGE, SELECT ON SEQUENCE public.conviction_history_id_seq TO service_role;
-- No dashboard access — internal audit log only

-- run_summaries: advisor note usefulness rating (-1 not useful, 1 useful)
ALTER TABLE public.run_summaries
  ADD COLUMN IF NOT EXISTS advisor_rating SMALLINT CHECK (advisor_rating IN (-1, 1));
