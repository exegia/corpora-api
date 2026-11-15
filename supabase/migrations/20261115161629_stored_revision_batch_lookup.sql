-- Draft only: facts do not guarantee object retention or establish provenance
-- for history rows that predate the registry.
CREATE FUNCTION public.corpora_stored_revision_facts(
    p_owner uuid, p_filename text, p_revisions uuid[]
) RETURNS jsonb LANGUAGE plpgsql STABLE SECURITY INVOKER SET search_path = '' AS $$
DECLARE
    result jsonb;
BEGIN
    IF p_revisions IS NULL OR cardinality(p_revisions) < 1 OR cardinality(p_revisions) > 100
       OR array_position(p_revisions, NULL) IS NOT NULL
       OR cardinality(p_revisions) <> (SELECT count(DISTINCT v) FROM unnest(p_revisions) v) THEN
        RAISE EXCEPTION 'ARCHIVE_INVALID';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM public.corpora_stored_heads
                   WHERE owner = p_owner AND filename = p_filename) THEN
        RAISE EXCEPTION 'ARCHIVE_NOT_FOUND';
    END IF;
    SELECT COALESCE(jsonb_agg(jsonb_build_object(
        'owner', r.owner, 'filename', r.filename, 'revision', r.revision,
        'digest', r.digest, 'version', r.version
    )), '[]'::jsonb) INTO result
    FROM public.corpora_stored_revisions r
    WHERE r.owner = p_owner AND r.filename = p_filename AND r.revision = ANY(p_revisions);
    RETURN result;
END;
$$;

REVOKE ALL ON FUNCTION public.corpora_stored_revision_facts(uuid,text,uuid[])
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.corpora_stored_revision_facts(uuid,text,uuid[])
    TO service_role;
NOTIFY pgrst, 'reload schema';
