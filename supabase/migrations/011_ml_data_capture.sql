-- Migration 011: ML data-capture — persist the decision/behavior signal that was
-- previously computed and discarded, so it accumulates as an ML training substrate.
-- Four append-only tables:
--   decision_features  — the brain's full feature vector + intermediate scores, per screened ticker per run
--   user_orders        — Abhi's real Robinhood fills (ground truth, replaces lossy snapshot-diff inference)
--   user_actions       — timeline of human control/feedback actions (previously state-overwrites only)
--   agent_runs         — the Autotrader per-run summary + sleeve math (previously Telegram/logs only)
-- All owner-only (no anon). Apply once in: Supabase Dashboard → SQL Editor.

-- ── decision_features: one row per screened ticker per run (no dedup; incl. hold/blocked) ──
CREATE TABLE IF NOT EXISTS public.decision_features (
    id                     BIGSERIAL PRIMARY KEY,
    run_date               TIMESTAMPTZ NOT NULL,
    ticker                 TEXT        NOT NULL,
    action                 TEXT        NOT NULL,   -- buy/sell/trim/watch/hold/blocked
    held                   BOOLEAN,                -- was a position held at decision time
    source                 TEXT,                   -- position screen / exploration
    score_buy              NUMERIC(10,6),          -- C·S (final)
    composite              NUMERIC(10,6),          -- S (pre-conviction weighted composite)
    conviction_weight      NUMERIC(10,6),          -- C
    regime                 TEXT,                   -- uptrend / downtrend / neutral
    suggested_position_pct NUMERIC(10,6),          -- final allocated size
    target_weight_raw      NUMERIC(10,6),          -- pre-allocation target weight
    vol_scalar             NUMERIC(10,6),
    hard_rule_block        TEXT,
    close_reason           TEXT,
    trim_fraction          NUMERIC(10,6),
    features               JSONB,                  -- full market_data feature vector
    scores                 JSONB,                  -- pillar floats + factor sub-components
    created_at             TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_decision_features_run    ON public.decision_features(run_date DESC);
CREATE INDEX IF NOT EXISTS idx_decision_features_ticker ON public.decision_features(ticker);
CREATE INDEX IF NOT EXISTS idx_decision_features_action ON public.decision_features(action);

GRANT SELECT ON public.decision_features TO authenticated;
GRANT ALL    ON public.decision_features TO service_role;
GRANT USAGE, SELECT ON SEQUENCE public.decision_features_id_seq TO service_role;

ALTER TABLE public.decision_features ENABLE ROW LEVEL SECURITY;
CREATE POLICY "owner read decision_features" ON public.decision_features
    FOR SELECT TO authenticated
    USING (auth.email() = 'abhikirk@icloud.com');

-- ── user_orders: Abhi's real Robinhood fills, idempotent on the RH order id ──
CREATE TABLE IF NOT EXISTS public.user_orders (
    id             BIGSERIAL PRIMARY KEY,
    order_id       TEXT UNIQUE NOT NULL,   -- Robinhood order UUID (idempotency key)
    ticker         TEXT NOT NULL,
    side           TEXT NOT NULL,          -- buy / sell
    order_type     TEXT,
    state          TEXT,                   -- filled / cancelled / ...
    quantity       NUMERIC(18,6),
    average_price  NUMERIC(18,4),
    created_at_rh  TIMESTAMPTZ,            -- order creation time per Robinhood
    filled_at      TIMESTAMPTZ,
    executions     JSONB,                  -- per-fill breakdown (price/qty/timestamp)
    realized_pnl   NUMERIC(18,4),
    raw            JSONB,                  -- full order payload for future re-parsing
    created_at     TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_user_orders_ticker ON public.user_orders(ticker);
CREATE INDEX IF NOT EXISTS idx_user_orders_filled ON public.user_orders(filled_at DESC);

GRANT SELECT ON public.user_orders TO authenticated;
GRANT ALL    ON public.user_orders TO service_role;
GRANT USAGE, SELECT ON SEQUENCE public.user_orders_id_seq TO service_role;

ALTER TABLE public.user_orders ENABLE ROW LEVEL SECURITY;
CREATE POLICY "owner read user_orders" ON public.user_orders
    FOR SELECT TO authenticated
    USING (auth.email() = 'abhikirk@icloud.com');

-- ── user_actions: append-only timeline of human control/feedback actions ──
CREATE TABLE IF NOT EXISTS public.user_actions (
    id           BIGSERIAL PRIMARY KEY,
    occurred_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    actor        TEXT NOT NULL DEFAULT 'abhi',
    action_type  TEXT NOT NULL,   -- autotrader_pause / autotrader_resume / advisor_rating /
                                   -- trade_feedback / conviction_edit / exploration_reject
    target       TEXT,            -- ticker / run id / etc. (nullable)
    payload      JSONB,
    created_at   TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_user_actions_time ON public.user_actions(occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_user_actions_type ON public.user_actions(action_type);

-- Owner can read AND insert (the dashboard writes these directly); service_role full access.
GRANT SELECT, INSERT ON public.user_actions TO authenticated;
GRANT ALL           ON public.user_actions TO service_role;
GRANT USAGE, SELECT ON SEQUENCE public.user_actions_id_seq TO service_role;

ALTER TABLE public.user_actions ENABLE ROW LEVEL SECURITY;
CREATE POLICY "owner rw user_actions" ON public.user_actions
    FOR ALL TO authenticated
    USING (auth.email() = 'abhikirk@icloud.com')
    WITH CHECK (auth.email() = 'abhikirk@icloud.com');

-- ── agent_runs: one row per Autotrader run — full summary + sleeve allocation math ──
CREATE TABLE IF NOT EXISTS public.agent_runs (
    id          BIGSERIAL PRIMARY KEY,
    run_date    TIMESTAMPTZ NOT NULL DEFAULT now(),
    dry_run     BOOLEAN,
    summary     JSONB,   -- placed/blocked/skipped(+reasons)/failed/halted/aborted
    sleeve      JSONB,   -- sleeve_budget, buy_slots, per-mirror conviction ranking + split
    created_at  TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_agent_runs_date ON public.agent_runs(run_date DESC);

GRANT SELECT ON public.agent_runs TO authenticated;
GRANT ALL    ON public.agent_runs TO service_role;
GRANT USAGE, SELECT ON SEQUENCE public.agent_runs_id_seq TO service_role;

ALTER TABLE public.agent_runs ENABLE ROW LEVEL SECURITY;
CREATE POLICY "owner read agent_runs" ON public.agent_runs
    FOR SELECT TO authenticated
    USING (auth.email() = 'abhikirk@icloud.com');

-- ── conviction_history: let the dashboard (authenticated owner) append edits ──
-- Previously service-role-only, so dashboard conviction edits (which write the live
-- `convictions` row directly) were never historized like the Python save path. Grant the
-- owner append access so every conviction edit — from either path — lands in the history.
GRANT SELECT, INSERT ON public.conviction_history TO authenticated;
GRANT USAGE, SELECT ON SEQUENCE public.conviction_history_id_seq TO authenticated;

ALTER TABLE public.conviction_history ENABLE ROW LEVEL SECURITY;
CREATE POLICY "owner rw conviction_history" ON public.conviction_history
    FOR ALL TO authenticated
    USING (auth.email() = 'abhikirk@icloud.com')
    WITH CHECK (auth.email() = 'abhikirk@icloud.com');
