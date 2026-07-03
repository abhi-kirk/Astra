-- Migration 006: pg_cron trigger for the daily analysis workflow_dispatch.
--
-- PREREQUISITE (run separately):
--   select vault.create_secret(
--     '<FINE_GRAINED_PAT>',            -- Astra repo only, permission Actions: Read and write
--     'github_pat_astra',
--     'GitHub fine-grained PAT, Astra repo, Actions:rw — for daily_analysis workflow_dispatch'
--   );

-- pg_cron is enabled by default on Supabase; pg_net must be enabled to make HTTP requests.
create extension if not exists pg_cron;
create extension if not exists pg_net;

-- Scheduling with an existing job name updates it, so this migration is safe to re-run.
-- The Vault secret is read at execution time — the token never appears in this file.
select cron.schedule(
  'astra-daily-dispatch',
  '0 13 * * 1-5',   -- weekdays 13:00 UTC / 6:00am PT (pre-market snapshot).
                    -- pg_cron fires on time, so the old :17 top-of-hour offset is no longer needed.
  $$
  select net.http_post(
    url := 'https://api.github.com/repos/abhi-kirk/Astra/actions/workflows/daily_analysis.yml/dispatches',
    body := jsonb_build_object('ref', 'main'),
    headers := jsonb_build_object(
      'Accept', 'application/vnd.github+json',
      'Authorization', 'Bearer ' || (select decrypted_secret from vault.decrypted_secrets where name = 'github_pat_astra'),
      'X-GitHub-Api-Version', '2022-11-28',
      'User-Agent', 'astra-pg-cron',   -- GitHub rejects API requests without a User-Agent
      'Content-Type', 'application/json'
    ),
    timeout_milliseconds := 8000
  );
  $$
);
