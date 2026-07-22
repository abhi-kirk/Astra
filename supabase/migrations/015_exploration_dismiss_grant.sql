-- Migration 015: allow the authenticated owner to dismiss On Radar candidates
-- Bug: the dashboard "DISMISS ✕" button on the On Radar panel sets
-- exploration_candidates.status = 'rejected', but migration 004 only granted
-- public SELECT — no UPDATE grant or UPDATE policy existed. The write returned a
-- 403, so the card never went away. Grant + policy the update, owner-scoped to
-- match agent_control (migration 009). service_role (pipeline) already bypasses RLS.

GRANT UPDATE ON public.exploration_candidates TO authenticated;

CREATE POLICY "owner update exploration" ON public.exploration_candidates
    FOR UPDATE TO authenticated
    USING (auth.email() = 'abhikirk@icloud.com')
    WITH CHECK (auth.email() = 'abhikirk@icloud.com');
