-- Migration 007: observability — per-run engineering metrics + per-service health.
-- Populated by src/observability.py (RunObserver) at the end of every daily run.
-- Apply once in Supabase Dashboard → SQL Editor.

-- run_metrics: one row per pipeline run
CREATE TABLE IF NOT EXISTS public.run_metrics (
  id                  SERIAL PRIMARY KEY,
  run_date            TIMESTAMPTZ NOT NULL DEFAULT now(),
  mode                TEXT        NOT NULL,
  status              TEXT        NOT NULL DEFAULT 'success'
                      CHECK (status IN ('success', 'partial', 'failed')),
  duration_s          NUMERIC,
  phase_timings       JSONB,        -- {robinhood_sync: 1.2, market_data: 3.4, ...}
  positions_screened  INTEGER,
  num_signals         INTEGER,
  buy_count           INTEGER,
  sell_count          INTEGER,
  watch_count         INTEGER,
  market_data_errors  INTEGER,
  advisor_model       TEXT,
  advisor_tokens_in   INTEGER,
  advisor_tokens_out  INTEGER,
  advisor_cost_usd    NUMERIC,
  error               TEXT
);
CREATE INDEX IF NOT EXISTS idx_run_metrics_date ON public.run_metrics(run_date DESC);
GRANT SELECT, INSERT ON public.run_metrics TO service_role;
GRANT USAGE, SELECT ON SEQUENCE public.run_metrics_id_seq TO service_role;
GRANT SELECT ON public.run_metrics TO authenticated;

ALTER TABLE public.run_metrics ENABLE ROW LEVEL SECURITY;
CREATE POLICY "owner read run_metrics" ON public.run_metrics
  FOR SELECT TO authenticated
  USING (auth.email() = 'abhikirk@icloud.com');

-- service_health: one row per service per run (external APIs, MCPs, infra)
CREATE TABLE IF NOT EXISTS public.service_health (
  id          SERIAL PRIMARY KEY,
  run_date    TIMESTAMPTZ NOT NULL DEFAULT now(),
  service     TEXT        NOT NULL,
  ok          BOOLEAN     NOT NULL,
  latency_ms  INTEGER,
  detail      TEXT
);
CREATE INDEX IF NOT EXISTS idx_service_health_date ON public.service_health(run_date DESC);
GRANT SELECT, INSERT ON public.service_health TO service_role;
GRANT USAGE, SELECT ON SEQUENCE public.service_health_id_seq TO service_role;
GRANT SELECT ON public.service_health TO authenticated;

ALTER TABLE public.service_health ENABLE ROW LEVEL SECURITY;
CREATE POLICY "owner read service_health" ON public.service_health
  FOR SELECT TO authenticated
  USING (auth.email() = 'abhikirk@icloud.com');
