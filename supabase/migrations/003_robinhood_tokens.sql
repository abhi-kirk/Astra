-- Migration 003: robinhood_tokens table
-- Stores AES-256-GCM re-encrypted Robinhood OAuth tokens so the daily GitHub
-- Actions run can persist rotated refresh tokens between runs without requiring
-- repository secrets to be updated manually.
-- Run in: Supabase Dashboard → SQL Editor

CREATE TABLE IF NOT EXISTS robinhood_tokens (
    id              INTEGER PRIMARY KEY DEFAULT 1,
    encrypted_blob  TEXT NOT NULL,        -- AES-256-GCM JSON: {iv, tag, ciphertext}
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

ALTER TABLE robinhood_tokens ENABLE ROW LEVEL SECURITY;
-- No SELECT/INSERT/UPDATE policies — only the service role key (used by the agent)
-- can access this table. The anon key used by the dashboard has no access.
