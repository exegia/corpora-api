BEGIN;
SET LOCAL lock_timeout = '3s';
LOCK TABLE public.organizations IN ACCESS EXCLUSIVE MODE;

-- Fail closed if the reviewed production policy baseline has changed.
DO $$
BEGIN
    IF NOT (SELECT relrowsecurity FROM pg_catalog.pg_class
            WHERE oid='public.organizations'::regclass)
       OR (SELECT count(*) FROM pg_catalog.pg_policies
           WHERE schemaname='public' AND tablename='organizations') <> 6
       OR EXISTS (
           SELECT name FROM unnest(ARRAY[
               'anon insert orgs', 'anon read orgs', 'anon update orgs',
               'organizations_insert_authenticated', 'organizations_select_authenticated',
               'organizations_update_authenticated'
           ]) AS name
           WHERE NOT EXISTS (SELECT 1 FROM pg_catalog.pg_policies p
               WHERE p.schemaname='public' AND p.tablename='organizations' AND p.policyname=name)
       ) THEN
        RAISE EXCEPTION 'ORGANIZATION_POLICY_BASELINE_CHANGED';
    END IF;
END;
$$;

DROP POLICY "anon insert orgs" ON public.organizations;
DROP POLICY "anon update orgs" ON public.organizations;
DROP POLICY organizations_update_authenticated ON public.organizations;
DROP POLICY organizations_insert_authenticated ON public.organizations;

-- Retain signed-in creation with an explicit role and live session. A future
-- permissive policy cannot reopen anonymous inserts or client updates.
CREATE POLICY organizations_authenticated_insert ON public.organizations
    FOR INSERT TO authenticated WITH CHECK (true);
CREATE POLICY organizations_no_anon_insert ON public.organizations
    AS RESTRICTIVE FOR INSERT TO anon WITH CHECK (false);
CREATE POLICY organizations_live_session_insert ON public.organizations
    AS RESTRICTIVE FOR INSERT TO authenticated
    WITH CHECK (public.corpora_directory_reader());
CREATE POLICY organizations_no_client_update ON public.organizations
    AS RESTRICTIVE FOR UPDATE TO anon,authenticated
    USING (false) WITH CHECK (false);
NOTIFY pgrst,'reload schema';
COMMIT;
