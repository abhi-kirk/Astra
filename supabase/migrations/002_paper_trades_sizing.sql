-- Migration 002: paper_trades — fractional sizing + close reason
-- Run in: Supabase Dashboard → SQL Editor

ALTER TABLE paper_trades
  ADD COLUMN IF NOT EXISTS suggested_position_pct NUMERIC(6,4),  -- e.g. 0.04 = 4% of virtual portfolio
  ADD COLUMN IF NOT EXISTS close_reason            TEXT;          -- signal_inactive | profit_take | blocked
