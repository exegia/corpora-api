BEGIN;
SET LOCAL lock_timeout='3s';
LOCK TABLE public.corpora, public.project_corpora, public.project_licences,
    public.project_references IN ACCESS EXCLUSIVE MODE;

-- Fail closed if these legacy tables acquired new policies since review.
DO $$
DECLARE item record; names text[];
BEGIN
    FOR item IN SELECT * FROM (VALUES
        ('corpora', ARRAY['anon full access (temporary)','authenticated full access (temporary)']::text[]),
        ('project_corpora', ARRAY['anon full access (temporary)','authenticated full access (temporary)']::text[]),
        ('project_references', ARRAY['anon full access (temporary)','authenticated full access (temporary)']::text[]),
        ('project_licences', ARRAY['anon delete plicences','anon insert plicences','anon read plicences']::text[])
    ) AS expected(table_name,policy_names) LOOP
        SELECT coalesce(array_agg(policyname ORDER BY policyname),ARRAY[]::text[]) INTO names
            FROM pg_catalog.pg_policies WHERE schemaname='public' AND tablename=item.table_name;
        IF names IS DISTINCT FROM item.policy_names THEN
            RAISE EXCEPTION 'PROJECT_ASSOCIATION_POLICY_BASELINE_CHANGED';
        END IF;
    END LOOP;
END;
$$;

-- Derive identity from the verified JWT and lock the project while checking
-- ownership. A concurrent ownership transfer must wait for this transaction.
CREATE FUNCTION public.corpora_project_association_editable(p_project uuid)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path='' SET lock_timeout='3s' AS $$
DECLARE actor uuid; owner uuid;
BEGIN
    actor := public.corpora_project_actor();
    SELECT owner_id INTO owner FROM public.projects WHERE id=p_project FOR SHARE;
    IF NOT FOUND THEN RETURN false; END IF;
    RETURN (actor IS NOT NULL AND owner=actor) OR EXISTS (
        SELECT 1 FROM corpora_session_guard.superadmins WHERE user_id=actor);
END;
$$;
REVOKE ALL ON FUNCTION public.corpora_project_association_editable(uuid) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION public.corpora_project_association_editable(uuid) TO authenticated;

-- PostgREST GET transactions are read-only and cannot take the write lock.
CREATE FUNCTION public.corpora_project_association_readable(p_project uuid)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path='' AS $$
DECLARE actor uuid:=auth.uid(); owner uuid;
BEGIN
    IF NOT public.corpora_directory_reader() THEN RETURN false; END IF;
    SELECT owner_id INTO owner FROM public.projects WHERE id=p_project;
    IF NOT FOUND THEN RETURN false; END IF;
    RETURN (actor IS NOT NULL AND owner=actor) OR EXISTS (
        SELECT 1 FROM corpora_session_guard.superadmins WHERE user_id=actor);
END;
$$;
REVOKE ALL ON FUNCTION public.corpora_project_association_readable(uuid) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION public.corpora_project_association_readable(uuid) TO authenticated;

-- The old catalog has no trustworthy visibility flag or verified owner
-- mapping. Keep its API shape for embedded reads but expose no rows to clients.
DROP POLICY "anon full access (temporary)" ON public.corpora;
DROP POLICY "authenticated full access (temporary)" ON public.corpora;
REVOKE ALL ON public.corpora FROM PUBLIC,anon,authenticated;
GRANT SELECT ON public.corpora TO anon,authenticated;

-- Associations can only be read or changed by the owner or superadmin. The
-- legacy corpus IDs cannot establish a member's separate private-corpus access.
DROP POLICY "anon full access (temporary)" ON public.project_corpora;
DROP POLICY "authenticated full access (temporary)" ON public.project_corpora;
REVOKE ALL ON public.project_corpora FROM PUBLIC,anon,authenticated;
GRANT SELECT ON public.project_corpora TO anon,authenticated;
GRANT INSERT,UPDATE,DELETE ON public.project_corpora TO authenticated;
CREATE POLICY corpora_project_association_read ON public.project_corpora FOR SELECT TO authenticated
    USING (public.corpora_project_association_readable(project_id));
CREATE POLICY corpora_project_association_insert ON public.project_corpora FOR INSERT TO authenticated
    WITH CHECK (public.corpora_project_association_editable(project_id));
CREATE POLICY corpora_project_association_update ON public.project_corpora FOR UPDATE TO authenticated
    USING (public.corpora_project_association_editable(project_id))
    WITH CHECK (public.corpora_project_association_editable(project_id));
CREATE POLICY corpora_project_association_delete ON public.project_corpora FOR DELETE TO authenticated
    USING (public.corpora_project_association_editable(project_id));

-- The unused legacy reference table cannot enforce the new two-corpus access
-- rules. Current reference work goes through the protected project-link API.
DROP POLICY "anon full access (temporary)" ON public.project_references;
DROP POLICY "authenticated full access (temporary)" ON public.project_references;
REVOKE ALL ON public.project_references FROM PUBLIC,anon,authenticated;

-- Licence display stays readable; only the project owner or superadmin can
-- alter an attachment. This does not verify the named person's consent.
DROP POLICY "anon delete plicences" ON public.project_licences;
DROP POLICY "anon insert plicences" ON public.project_licences;
DROP POLICY "anon read plicences" ON public.project_licences;
REVOKE ALL ON public.project_licences FROM PUBLIC,anon,authenticated;
GRANT SELECT ON public.project_licences TO anon,authenticated;
GRANT INSERT,UPDATE,DELETE ON public.project_licences TO authenticated;
CREATE POLICY corpora_project_licence_read_anon ON public.project_licences FOR SELECT TO anon USING (true);
CREATE POLICY corpora_project_licence_read_user ON public.project_licences FOR SELECT TO authenticated
    USING (public.corpora_directory_reader());
CREATE POLICY corpora_project_licence_insert ON public.project_licences FOR INSERT TO authenticated
    WITH CHECK (public.corpora_project_association_editable(project_id));
CREATE POLICY corpora_project_licence_update ON public.project_licences FOR UPDATE TO authenticated
    USING (public.corpora_project_association_editable(project_id))
    WITH CHECK (public.corpora_project_association_editable(project_id));
CREATE POLICY corpora_project_licence_delete ON public.project_licences FOR DELETE TO authenticated
    USING (public.corpora_project_association_editable(project_id));

NOTIFY pgrst,'reload schema';
COMMIT;
