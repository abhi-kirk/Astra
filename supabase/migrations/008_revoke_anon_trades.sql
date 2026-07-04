-- Migration 008: revoke public (anon) access to the raw trades ledger.
--
-- After this migration, anon receives `permission denied for table trades`.
-- Owner (authenticated) and service_role access are unchanged.

DROP POLICY IF EXISTS "anon read trades" ON public.trades;
REVOKE SELECT ON public.trades FROM anon;
