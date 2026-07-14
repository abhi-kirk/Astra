-- Advisor sizing off the real full-portfolio balance: persist the Individual account's
-- deployable cash with each snapshot so the paper track sizes against it (not a fixed book).
ALTER TABLE portfolio_snapshots ADD COLUMN IF NOT EXISTS buying_power numeric;
