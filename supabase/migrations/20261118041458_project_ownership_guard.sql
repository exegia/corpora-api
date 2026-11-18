-- Project ownership is an Auth identity, never a mutable directory profile.
-- Fail instead of silently leaving an unexpected permissive policy in place.
BEGIN;
LOCK TABLE public.projects, public.user_directory IN ACCESS EXCLUSIVE MODE;
DO $$
BEGIN
    IF (SELECT count(*) FROM pg_catalog.pg_policies
        WHERE schemaname='public' AND tablename='projects') <> 2
       OR NOT EXISTS (SELECT 1 FROM pg_catalog.pg_policies WHERE schemaname='public'
           AND tablename='projects' AND policyname='anon full access (temporary)' AND cmd='ALL')
       OR NOT EXISTS (SELECT 1 FROM pg_catalog.pg_policies WHERE schemaname='public'
           AND tablename='projects' AND policyname='authenticated full access (temporary)' AND cmd='ALL') THEN
        RAISE EXCEPTION 'PROJECT_POLICY_BASELINE_CHANGED';
    END IF;
    -- Existing owner values came from an unprotected table. Never promote one
    -- into an authorization authority without a separate owner review.
    IF EXISTS(SELECT 1 FROM public.projects WHERE owner_id IS NOT NULL) THEN
        RAISE EXCEPTION 'PROJECT_EXISTING_OWNERS_REQUIRE_REVIEW';
    END IF;
    IF (SELECT count(*) FROM pg_catalog.pg_policies
        WHERE schemaname='public' AND tablename='user_directory') <> 1
       OR NOT EXISTS(SELECT 1 FROM pg_catalog.pg_policies WHERE schemaname='public'
           AND tablename='user_directory' AND policyname='anon read user_directory' AND cmd='SELECT') THEN
        RAISE EXCEPTION 'DIRECTORY_POLICY_BASELINE_CHANGED';
    END IF;
END;
$$;

-- PostgREST has already verified these JWT claims. Lock the live session for
-- the transaction, so a previously signed-out token cannot mutate projects.
CREATE FUNCTION public.corpora_project_actor() RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path='' AS $$
DECLARE actor uuid:=auth.uid(); claims jsonb:=auth.jwt(); session_id uuid; token_exp bigint;
BEGIN
    BEGIN
        session_id:=(claims->>'session_id')::uuid;
        token_exp:=(claims->>'exp')::bigint;
    EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range THEN
        RAISE EXCEPTION 'AUTH_SESSION_INVALID';
    END;
    PERFORM corpora_session_guard.lock_verified_session(actor,session_id,token_exp);
    RETURN actor;
END;
$$;
CREATE FUNCTION public.corpora_project_superadmin() RETURNS boolean
LANGUAGE sql SECURITY DEFINER SET search_path='' AS $$
    SELECT EXISTS(SELECT 1 FROM corpora_session_guard.superadmins
        WHERE user_id=public.corpora_project_actor());
$$;
-- PostgREST GET requests are read-only transactions, so they cannot take a
-- row lock. Check the live session without a lock for directory reads.
CREATE FUNCTION public.corpora_directory_reader() RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path='' AS $$
DECLARE actor uuid:=auth.uid(); claims jsonb:=auth.jwt(); session_id uuid; token_exp bigint;
BEGIN
    BEGIN
        session_id:=(claims->>'session_id')::uuid;
        token_exp:=(claims->>'exp')::bigint;
    EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range THEN
        RAISE EXCEPTION 'AUTH_SESSION_INVALID';
    END;
    IF actor IS NULL OR session_id IS NULL OR token_exp IS NULL
       OR token_exp<=extract(epoch FROM pg_catalog.clock_timestamp())
       OR NOT EXISTS(SELECT 1 FROM auth.sessions s WHERE s.id=session_id AND s.user_id=actor
           AND (s.not_after IS NULL OR s.not_after>pg_catalog.clock_timestamp())) THEN
        RAISE EXCEPTION 'AUTH_SESSION_INVALID';
    END IF;
    RETURN true;
END;
$$;
REVOKE ALL ON FUNCTION public.corpora_project_actor() FROM PUBLIC,anon;
REVOKE ALL ON FUNCTION public.corpora_project_superadmin() FROM PUBLIC,anon;
REVOKE ALL ON FUNCTION public.corpora_directory_reader() FROM PUBLIC,anon;
GRANT EXECUTE ON FUNCTION public.corpora_project_actor() TO authenticated;
GRANT EXECUTE ON FUNCTION public.corpora_project_superadmin() TO authenticated;
GRANT EXECUTE ON FUNCTION public.corpora_directory_reader() TO authenticated;

-- The directory remains read-only for browser roles, but now requires a
-- verified live session; trusted service access is unchanged.
REVOKE ALL ON public.user_directory FROM PUBLIC,anon,authenticated;
GRANT SELECT ON public.user_directory TO authenticated;
DROP POLICY "anon read user_directory" ON public.user_directory;
CREATE POLICY corpora_directory_read ON public.user_directory FOR SELECT TO authenticated
    USING (public.corpora_directory_reader());

ALTER TABLE public.projects ALTER COLUMN owner_id SET DEFAULT auth.uid();
ALTER TABLE public.projects ADD CONSTRAINT projects_owner_auth_fkey
    FOREIGN KEY(owner_id) REFERENCES auth.users(id) ON DELETE RESTRICT;
-- RLS does not cover TRUNCATE, REFERENCES, or TRIGGER privileges.
REVOKE ALL ON public.projects FROM PUBLIC,anon,authenticated;
GRANT SELECT ON public.projects TO anon,authenticated;
GRANT INSERT,UPDATE,DELETE ON public.projects TO authenticated;
DROP POLICY "anon full access (temporary)" ON public.projects;
DROP POLICY "authenticated full access (temporary)" ON public.projects;
CREATE POLICY corpora_project_read ON public.projects FOR SELECT TO anon,authenticated USING (true);
CREATE POLICY corpora_project_create ON public.projects FOR INSERT TO authenticated
    WITH CHECK (owner_id=public.corpora_project_actor() OR public.corpora_project_superadmin());
CREATE POLICY corpora_project_update ON public.projects FOR UPDATE TO authenticated
    USING (owner_id=public.corpora_project_actor() OR public.corpora_project_superadmin())
    WITH CHECK (owner_id=public.corpora_project_actor() OR public.corpora_project_superadmin());
CREATE POLICY corpora_project_delete ON public.projects FOR DELETE TO authenticated
    USING (owner_id=public.corpora_project_actor() OR public.corpora_project_superadmin());
NOTIFY pgrst,'reload schema';
COMMIT;
