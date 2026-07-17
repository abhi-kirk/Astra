-- 013_drop_peak_invested.sql
-- Supersede 012's peak_invested denominator.
--
-- 012 sized the P&L drawdown halt against `peak_invested` (a high-water mark of deployed
-- cost basis). That measured loss against capital *at work* (~$700 of a $1000 sleeve),
-- ignoring the reserve cash — ~43% more sensitive than "−15% of the sleeve" and out of step
-- with the account's actual contributed capital.
--
-- The halt now uses net contributed capital = total_equity − net_pnl, reconstructed at
-- runtime (no stored state): stable under P&L, rises on a deposit, falls on a withdrawal.
-- So peak_invested is unused — drop it from both tables.

ALTER TABLE public.agent_control            DROP COLUMN IF EXISTS peak_invested;
ALTER TABLE public.agent_account_snapshots  DROP COLUMN IF EXISTS peak_invested;
