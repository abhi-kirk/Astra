-- Migration 014: performance-vs-S&P time series for the dashboard charts.
--   benchmark_prices — daily SPY (total-return) close, one row per (symbol, date)
--   paper_equity     — forward-only snapshot of the clean ASTRA paper book (notional
--                      fixed-capital account) marked to market each run + its NAV index
-- Both are public dashboard reads (the paper track + benchmark are the public story) → anon
-- + authenticated SELECT with a USING(true) policy. Also adds nav_index to the (owner-only)
-- agent_account_snapshots for the Autotrader's time-weighted-return curve.
-- Apply once in: Supabase Dashboard → SQL Editor.

-- ── benchmark_prices: daily SPY close (dividend-adjusted → total return), idempotent ──
CREATE TABLE IF NOT EXISTS public.benchmark_prices (
    id          BIGSERIAL PRIMARY KEY,
    symbol      TEXT        NOT NULL,        -- e.g. SPY
    price_date  DATE        NOT NULL,
    close       NUMERIC(18,4) NOT NULL,
    created_at  TIMESTAMPTZ DEFAULT now(),
    UNIQUE (symbol, price_date)
);
CREATE INDEX IF NOT EXISTS idx_benchmark_prices_sym_date
    ON public.benchmark_prices(symbol, price_date DESC);

GRANT ALL    ON public.benchmark_prices TO service_role;
GRANT USAGE, SELECT ON SEQUENCE public.benchmark_prices_id_seq TO service_role;
GRANT SELECT ON public.benchmark_prices TO anon;
GRANT SELECT ON public.benchmark_prices TO authenticated;

ALTER TABLE public.benchmark_prices ENABLE ROW LEVEL SECURITY;
CREATE POLICY "public read benchmark_prices" ON public.benchmark_prices
    FOR SELECT TO anon, authenticated
    USING (true);

-- ── paper_equity: one row per run — the notional paper book marked to market + NAV index ──
-- holdings carries the notional lot ledger forward (lot_id/ticker/notional_shares/entry_price/
-- virtual_shares_ref) so trims/closes reconcile without replaying the un-replayable real book.
CREATE TABLE IF NOT EXISTS public.paper_equity (
    id                BIGSERIAL PRIMARY KEY,
    snapshot_time     TIMESTAMPTZ NOT NULL DEFAULT now(),
    cash              NUMERIC(18,4),
    market_value      NUMERIC(18,4),
    total_equity      NUMERIC(18,4),
    invested_cost     NUMERIC(18,4),
    unrealized_pnl    NUMERIC(18,4),
    realized_pnl_cum  NUMERIC(18,4),
    nav_index         NUMERIC(12,6),        -- total_equity / nav0 * 100 (no external flows)
    holdings          JSONB,
    created_at        TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_paper_equity_time
    ON public.paper_equity(snapshot_time DESC);

GRANT ALL    ON public.paper_equity TO service_role;
GRANT USAGE, SELECT ON SEQUENCE public.paper_equity_id_seq TO service_role;
GRANT SELECT ON public.paper_equity TO anon;
GRANT SELECT ON public.paper_equity TO authenticated;

ALTER TABLE public.paper_equity ENABLE ROW LEVEL SECURITY;
CREATE POLICY "public read paper_equity" ON public.paper_equity
    FOR SELECT TO anon, authenticated
    USING (true);

-- ── Autotrader NAV index — time-weighted return (linked Modified Dietz) per run ──
-- Inherits agent_account_snapshots' table-level grants; no new GRANTs.
ALTER TABLE public.agent_account_snapshots ADD COLUMN IF NOT EXISTS nav_index NUMERIC(12,6);
