-- 012_pnl_drawdown.sql
-- Deposit/withdrawal-immune drawdown halt for the Autotrader.
--
-- The old halt compared live account equity against a fixed `baseline_equity` set on the
-- first run. That conflates trading performance with funding: a deposit inflates equity
-- (masking losses / defeating the halt), a withdrawal deflates it (false −15% trigger),
-- so it required a MANUAL baseline reset on every cash movement — unworkable as the sleeve
-- scales (Phase 3). Robinhood's agentic OAuth scope exposes no transfer/ACH history, so
-- transfers can't be detected to auto-correct the baseline.
--
-- The fix measures actual trading P&L instead of equity. net_pnl = realized (all-time) +
-- unrealized (market value of open stock − cost basis). Deposits/withdrawals move cash but
-- never touch realized or unrealized P&L, so the metric is funding-immune by construction.
--
-- NOTE: this migration also added `peak_invested` (a high-water mark of deployed cost basis)
-- as the halt denominator. That was superseded before go-live by net contributed capital
-- (total_equity − net_pnl, computed at runtime, no stored state) — see 013, which drops
-- peak_invested. It is kept here so this file matches what was applied (append-only).
-- `baseline_equity` is retired (column kept, nullable, no longer written) for history.

ALTER TABLE public.agent_control
    ADD COLUMN IF NOT EXISTS peak_invested NUMERIC(18,4);  -- superseded — dropped in 013

ALTER TABLE public.agent_account_snapshots
    ADD COLUMN IF NOT EXISTS realized_pnl        NUMERIC(18,4),  -- all-time realized P&L ($)
    ADD COLUMN IF NOT EXISTS unrealized_pnl      NUMERIC(18,4),  -- open-position unrealized P&L ($)
    ADD COLUMN IF NOT EXISTS net_pnl             NUMERIC(18,4),  -- realized + unrealized ($)
    ADD COLUMN IF NOT EXISTS invested_cost_basis NUMERIC(18,4),  -- Σ shares·avg_cost of open book
    ADD COLUMN IF NOT EXISTS peak_invested       NUMERIC(18,4);  -- superseded — dropped in 013

-- No new GRANTs: columns inherit the table-level grants from migration 009.
