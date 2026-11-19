BEGIN;
SET LOCAL lock_timeout='3s';
LOCK TABLE public.corpus_documents, public.corpus_commits, public.corpus_changes, public.licences,
    storage.objects IN ACCESS EXCLUSIVE MODE;

-- Stop if the deployed table or bucket policies differ from the reviewed baseline.
DO $$
DECLARE item record; names text[];
BEGIN
    FOR item IN SELECT * FROM (VALUES
        ('corpus_documents', ARRAY['anon delete corpus documents','anon insert corpus documents','anon read corpus documents']::text[]),
        ('corpus_commits', ARRAY['anon delete corpus commits','anon insert corpus commits','anon read corpus commits']::text[]),
        ('corpus_changes', ARRAY['corpus_changes_select']::text[]),
        ('licences', ARRAY['anon insert licences','anon update licences','licences_insert_authenticated',
            'licences_select_public','licences_update_authenticated']::text[])
    ) AS expected(table_name,policy_names) LOOP
        SELECT coalesce(array_agg(policyname ORDER BY policyname),ARRAY[]::text[]) INTO names
            FROM pg_catalog.pg_policies WHERE schemaname='public' AND tablename=item.table_name;
        IF names IS DISTINCT FROM item.policy_names THEN
            RAISE EXCEPTION 'CORPUS_DOCUMENT_POLICY_BASELINE_CHANGED';
        END IF;
    END LOOP;
    IF (SELECT count(*) FROM pg_catalog.pg_policies WHERE schemaname='storage' AND tablename='objects'
        AND permissive='PERMISSIVE' AND roles=ARRAY['public']::name[] AND (
            (policyname='anon delete project corpora' AND cmd='DELETE'
                AND qual='(bucket_id = ''project-corpora''::text)' AND with_check IS NULL) OR
            (policyname='anon read project corpora' AND cmd='SELECT'
                AND qual='(bucket_id = ''project-corpora''::text)' AND with_check IS NULL) OR
            (policyname='anon update project corpora' AND cmd='UPDATE'
                AND qual='(bucket_id = ''project-corpora''::text)'
                AND with_check='(bucket_id = ''project-corpora''::text)') OR
            (policyname='anon upload project corpora' AND cmd='INSERT' AND qual IS NULL
                AND with_check='(bucket_id = ''project-corpora''::text)'))) <> 4 THEN
        RAISE EXCEPTION 'CORPUS_STORAGE_POLICY_BASELINE_CHANGED';
    END IF;
END;
$$;

-- Existing documents have no verified owner; keep them unassigned. New
-- client-created documents default to the signed-in user's Auth identity.
ALTER TABLE public.corpus_documents ADD COLUMN owner_id uuid REFERENCES auth.users(id) ON DELETE RESTRICT;
ALTER TABLE public.corpus_documents ALTER COLUMN owner_id SET DEFAULT auth.uid();

CREATE FUNCTION public.corpora_legacy_superadmin_readable() RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path='' AS $$
BEGIN
    IF NOT public.corpora_directory_reader() THEN RETURN false; END IF;
    RETURN EXISTS(SELECT 1 FROM corpora_session_guard.superadmins WHERE user_id=auth.uid());
END;
$$;
CREATE FUNCTION public.corpora_legacy_document_editable(p_document uuid) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path='' SET lock_timeout='3s' AS $$
DECLARE actor uuid; owner uuid;
BEGIN
    actor := public.corpora_project_actor();
    SELECT owner_id INTO owner FROM public.corpus_documents WHERE id=p_document FOR SHARE;
    IF NOT FOUND THEN RETURN false; END IF;
    RETURN (actor IS NOT NULL AND owner=actor) OR EXISTS (
        SELECT 1 FROM corpora_session_guard.superadmins WHERE user_id=actor);
END;
$$;
REVOKE ALL ON FUNCTION public.corpora_legacy_superadmin_readable(),
    public.corpora_legacy_document_editable(uuid) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION public.corpora_legacy_superadmin_readable(),
    public.corpora_legacy_document_editable(uuid) TO authenticated;

DROP POLICY "anon delete corpus documents" ON public.corpus_documents;
DROP POLICY "anon insert corpus documents" ON public.corpus_documents;
DROP POLICY "anon read corpus documents" ON public.corpus_documents;
REVOKE ALL ON public.corpus_documents FROM PUBLIC,anon,authenticated;
-- Keep SELECT available for PostgREST embeds, with RLS filtering all anon rows.
GRANT SELECT ON public.corpus_documents TO anon,authenticated;
GRANT INSERT,UPDATE,DELETE ON public.corpus_documents TO authenticated;
CREATE POLICY corpora_legacy_document_read ON public.corpus_documents FOR SELECT TO authenticated
    USING (public.corpora_directory_reader() AND
        (owner_id=auth.uid() OR public.corpora_legacy_superadmin_readable()));
CREATE POLICY corpora_legacy_document_insert ON public.corpus_documents FOR INSERT TO authenticated
    WITH CHECK (owner_id=public.corpora_project_actor() OR public.corpora_project_superadmin());
CREATE POLICY corpora_legacy_document_update ON public.corpus_documents FOR UPDATE TO authenticated
    USING (owner_id=public.corpora_project_actor() OR public.corpora_project_superadmin())
    WITH CHECK (owner_id=public.corpora_project_actor() OR public.corpora_project_superadmin());
CREATE POLICY corpora_legacy_document_delete ON public.corpus_documents FOR DELETE TO authenticated
    USING (owner_id=public.corpora_project_actor() OR public.corpora_project_superadmin());

DROP POLICY "anon delete corpus commits" ON public.corpus_commits;
DROP POLICY "anon insert corpus commits" ON public.corpus_commits;
DROP POLICY "anon read corpus commits" ON public.corpus_commits;
REVOKE ALL ON public.corpus_commits FROM PUBLIC,anon,authenticated;
GRANT SELECT ON public.corpus_commits TO anon,authenticated;
GRANT INSERT,UPDATE,DELETE ON public.corpus_commits TO authenticated;
CREATE POLICY corpora_legacy_commit_read ON public.corpus_commits FOR SELECT TO authenticated
    USING (EXISTS(SELECT 1 FROM public.corpus_documents d WHERE d.id=document_id));
CREATE POLICY corpora_legacy_commit_insert ON public.corpus_commits FOR INSERT TO authenticated
    WITH CHECK (public.corpora_legacy_document_editable(document_id));
CREATE POLICY corpora_legacy_commit_update ON public.corpus_commits FOR UPDATE TO authenticated
    USING (public.corpora_legacy_document_editable(document_id))
    WITH CHECK (public.corpora_legacy_document_editable(document_id));
CREATE POLICY corpora_legacy_commit_delete ON public.corpus_commits FOR DELETE TO authenticated
    USING (public.corpora_legacy_document_editable(document_id));

-- Existing change rows may include source text and diffs. Read only if the
-- associated document is visible under the owner/superadmin policy above.
DROP POLICY corpus_changes_select ON public.corpus_changes;
REVOKE ALL ON public.corpus_changes FROM PUBLIC,anon,authenticated;
GRANT SELECT ON public.corpus_changes TO authenticated;
CREATE POLICY corpora_legacy_change_read ON public.corpus_changes FOR SELECT TO authenticated
    USING (EXISTS(SELECT 1 FROM public.corpus_documents d WHERE d.id=document_id));

DROP POLICY "anon insert licences" ON public.licences;
DROP POLICY "anon update licences" ON public.licences;
DROP POLICY licences_insert_authenticated ON public.licences;
DROP POLICY licences_select_public ON public.licences;
DROP POLICY licences_update_authenticated ON public.licences;
REVOKE ALL ON public.licences FROM PUBLIC,anon,authenticated;
GRANT SELECT ON public.licences TO anon,authenticated;
GRANT INSERT,UPDATE ON public.licences TO authenticated;
CREATE POLICY corpora_licence_catalog_anon ON public.licences FOR SELECT TO anon USING (true);
CREATE POLICY corpora_licence_catalog_user ON public.licences FOR SELECT TO authenticated
    USING (public.corpora_directory_reader());
CREATE POLICY corpora_licence_catalog_insert ON public.licences FOR INSERT TO authenticated
    WITH CHECK (public.corpora_project_superadmin());
CREATE POLICY corpora_licence_catalog_update ON public.licences FOR UPDATE TO authenticated
    USING (public.corpora_project_superadmin()) WITH CHECK (public.corpora_project_superadmin());

DROP POLICY "anon delete project corpora" ON storage.objects;
DROP POLICY "anon read project corpora" ON storage.objects;
DROP POLICY "anon update project corpora" ON storage.objects;
DROP POLICY "anon upload project corpora" ON storage.objects;
CREATE POLICY corpora_private_objects_read ON storage.objects FOR SELECT TO authenticated
    USING (bucket_id='project-corpora' AND public.corpora_directory_reader() AND
        (owner_id=auth.uid()::text OR public.corpora_legacy_superadmin_readable()));
CREATE POLICY corpora_private_objects_insert ON storage.objects FOR INSERT TO authenticated
    WITH CHECK (bucket_id='project-corpora' AND
        (owner_id=public.corpora_project_actor()::text OR public.corpora_project_superadmin()));
CREATE POLICY corpora_private_objects_update ON storage.objects FOR UPDATE TO authenticated
    USING (bucket_id='project-corpora' AND
        (owner_id=public.corpora_project_actor()::text OR public.corpora_project_superadmin()))
    WITH CHECK (bucket_id='project-corpora' AND
        (owner_id=public.corpora_project_actor()::text OR public.corpora_project_superadmin()));
CREATE POLICY corpora_private_objects_delete ON storage.objects FOR DELETE TO authenticated
    USING (bucket_id='project-corpora' AND
        (owner_id=public.corpora_project_actor()::text OR public.corpora_project_superadmin()));

-- Restrictive guards prevent another permissive Storage policy from reopening
-- this bucket. They leave all other buckets and the service role unchanged.
CREATE POLICY corpora_private_objects_anon_guard ON storage.objects AS RESTRICTIVE FOR ALL TO anon
    USING (bucket_id<>'project-corpora') WITH CHECK (bucket_id<>'project-corpora');
CREATE POLICY corpora_private_objects_user_read_guard ON storage.objects AS RESTRICTIVE FOR SELECT TO authenticated
    USING (bucket_id<>'project-corpora' OR (public.corpora_directory_reader() AND
        (owner_id=auth.uid()::text OR public.corpora_legacy_superadmin_readable())));
CREATE POLICY corpora_private_objects_user_insert_guard ON storage.objects AS RESTRICTIVE FOR INSERT TO authenticated
    WITH CHECK (bucket_id<>'project-corpora' OR
        (owner_id=public.corpora_project_actor()::text OR public.corpora_project_superadmin()));
CREATE POLICY corpora_private_objects_user_update_guard ON storage.objects AS RESTRICTIVE FOR UPDATE TO authenticated
    USING (bucket_id<>'project-corpora' OR
        (owner_id=public.corpora_project_actor()::text OR public.corpora_project_superadmin()))
    WITH CHECK (bucket_id<>'project-corpora' OR
        (owner_id=public.corpora_project_actor()::text OR public.corpora_project_superadmin()));
CREATE POLICY corpora_private_objects_user_delete_guard ON storage.objects AS RESTRICTIVE FOR DELETE TO authenticated
    USING (bucket_id<>'project-corpora' OR
        (owner_id=public.corpora_project_actor()::text OR public.corpora_project_superadmin()));

NOTIFY pgrst,'reload schema';
COMMIT;
