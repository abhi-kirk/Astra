-- Migration 009: Autotrader — autonomous agentic trading (Phase 2).
-- Real-money orders placed by ASTRA in the dedicated Robinhood Agentic account,
-- mirroring the paper track and filtered by code-enforced guardrails.
-- All tables are owner-only (no anon) — Autotrader stays behind auth for now.
-- Apply once in: Supabase Dashboard → SQL Editor.

-- ── agent_trades: one row per real order in the agentic account ───────────────
CREATE TABLE IF NOT EXISTS public.agent_trades (
    id                    BIGSERIAL PRIMARY KEY,
    ticker                TEXT NOT NULL,
    side                  TEXT NOT NULL,          -- buy / sell
    order_type            TEXT NOT NULL DEFAULT 'limit',
    quantity              NUMERIC(18,6),
    limit_price           NUMERIC(18,4),
    fill_price            NUMERIC(18,4),
    dollar_amount         NUMERIC(18,4),
    order_id              TEXT,                   -- Robinhood order UUID
    ref_id                TEXT UNIQUE,            -- client idempotency key (UUID)
    status                TEXT NOT NULL DEFAULT 'pending',  -- pending/submitted/filled/cancelled/rejected/failed/dry_run
    submitted_at          TIMESTAMPTZ,
    executed_at           TIMESTAMPTZ,
    rule_checks           JSONB,                  -- which guardrails passed/failed
    mirrors_paper_trade_id BIGINT REFERENCES public.paper_trades(id),
    source                TEXT NOT NULL DEFAULT 'mirror',   -- mirror / independent
    realized_pnl          NUMERIC(18,4),
    is_open               BOOLEAN DEFAULT TRUE,
    closed_at             TIMESTAMPTZ,
    close_price           NUMERIC(18,4),
    run_date              TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at            TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_agent_trades_ticker ON public.agent_trades(ticker);
CREATE INDEX IF NOT EXISTS idx_agent_trades_open   ON public.agent_trades(is_open) WHERE is_open = TRUE;
CREATE INDEX IF NOT EXISTS idx_agent_trades_date   ON public.agent_trades(run_date DESC);

GRANT SELECT ON public.agent_trades TO authenticated;
GRANT ALL    ON public.agent_trades TO service_role;
GRANT USAGE, SELECT ON SEQUENCE public.agent_trades_id_seq TO service_role;

ALTER TABLE public.agent_trades ENABLE ROW LEVEL SECURITY;
CREATE POLICY "owner read agent_trades" ON public.agent_trades
    FOR SELECT TO authenticated
    USING (auth.email() = 'abhikirk@icloud.com');

-- ── agent_account_snapshots: agentic account state for the private dashboard ──
CREATE TABLE IF NOT EXISTS public.agent_account_snapshots (
    id              BIGSERIAL PRIMARY KEY,
    snapshot_time   TIMESTAMPTZ NOT NULL DEFAULT now(),
    cash            NUMERIC(18,4),
    buying_power    NUMERIC(18,4),
    market_value    NUMERIC(18,4),
    total_equity    NUMERIC(18,4),
    positions       JSONB,
    baseline_equity NUMERIC(18,4),
    drawdown_pct    NUMERIC(8,4),
    created_at      TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_agent_snapshots_time ON public.agent_account_snapshots(snapshot_time DESC);

GRANT SELECT ON public.agent_account_snapshots TO authenticated;
GRANT ALL    ON public.agent_account_snapshots TO service_role;
GRANT USAGE, SELECT ON SEQUENCE public.agent_account_snapshots_id_seq TO service_role;

ALTER TABLE public.agent_account_snapshots ENABLE ROW LEVEL SECURITY;
CREATE POLICY "owner read agent_account_snapshots" ON public.agent_account_snapshots
    FOR SELECT TO authenticated
    USING (auth.email() = 'abhikirk@icloud.com');

-- ── agent_control: single-row kill/pause switch (id = 1) ─────────────────────
-- paused  → toggled by the owner from the auth-gated dashboard; execution skips while true.
-- halted  → set by code on a drawdown breach; requires manual reset (dashboard/DB).
CREATE TABLE IF NOT EXISTS public.agent_control (
    id              INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    paused          BOOLEAN NOT NULL DEFAULT false,
    halted          BOOLEAN NOT NULL DEFAULT false,
    halt_reason     TEXT,
    baseline_equity NUMERIC(18,4),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
INSERT INTO public.agent_control (id) VALUES (1) ON CONFLICT (id) DO NOTHING;

-- Owner can read AND update (the dashboard pause/resume toggle); service_role full access.
GRANT SELECT, UPDATE ON public.agent_control TO authenticated;
GRANT ALL           ON public.agent_control TO service_role;

ALTER TABLE public.agent_control ENABLE ROW LEVEL SECURITY;
CREATE POLICY "owner rw agent_control" ON public.agent_control
    FOR ALL TO authenticated
    USING (auth.email() = 'abhikirk@icloud.com')
    WITH CHECK (auth.email() = 'abhikirk@icloud.com');
