-- Draft: apply only alongside the coordinated stored archive integration.
INSERT INTO storage.buckets(id, name, public)
VALUES ('corpus-stored-revisions', 'corpus-stored-revisions', false)
ON CONFLICT (id) DO NOTHING;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM storage.buckets WHERE id = 'corpus-stored-revisions' AND public) THEN
        RAISE EXCEPTION 'Stored revision bucket must be private';
    END IF;
END;
$$;

-- A broad existing permissive policy must not expose revision bytes.
CREATE POLICY corpora_stored_revisions_private ON storage.objects AS RESTRICTIVE
    FOR ALL TO PUBLIC USING (bucket_id <> 'corpus-stored-revisions')
    WITH CHECK (bucket_id <> 'corpus-stored-revisions');

CREATE FUNCTION public.corpora_stored_get(p_owner uuid, p_filename text, p_revision uuid DEFAULT NULL)
RETURNS jsonb LANGUAGE sql STABLE SECURITY INVOKER SET search_path = '' AS $$
    SELECT jsonb_build_object(
        'owner', r.owner, 'filename', r.filename, 'revision', r.revision,
        'digest', r.digest, 'version', r.version
    )
    FROM public.corpora_stored_revisions r
    JOIN public.corpora_stored_heads h USING (owner, filename)
    WHERE r.owner = p_owner AND r.filename = p_filename
      AND r.revision = COALESCE(p_revision, h.revision);
$$;

REVOKE ALL ON FUNCTION public.corpora_stored_get(uuid,text,uuid) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.corpora_stored_get(uuid,text,uuid) TO service_role;
NOTIFY pgrst, 'reload schema';
