-- Draft only. This lists registry facts, not unverified imported history or a
-- guarantee that the corresponding immutable object has been retained.
CREATE INDEX corpora_stored_revisions_history_idx
    ON public.corpora_stored_revisions(owner, filename, created_at DESC, revision DESC);

CREATE FUNCTION public.corpora_stored_history(
    p_owner uuid, p_filename text, p_limit integer,
    p_before timestamptz DEFAULT NULL, p_before_revision uuid DEFAULT NULL
) RETURNS jsonb LANGUAGE plpgsql STABLE SECURITY INVOKER SET search_path = '' AS $$
DECLARE
    result jsonb;
BEGIN
    IF p_limit IS NULL OR p_limit < 1 OR p_limit > 100
       OR (p_before IS NULL) <> (p_before_revision IS NULL) THEN
        RAISE EXCEPTION 'ARCHIVE_INVALID';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM public.corpora_stored_heads
                   WHERE owner = p_owner AND filename = p_filename) THEN
        RAISE EXCEPTION 'ARCHIVE_NOT_FOUND';
    END IF;
    SELECT COALESCE(jsonb_agg(jsonb_build_object(
        'owner', page.owner, 'filename', page.filename, 'revision', page.revision,
        'digest', page.digest, 'version', page.version, 'created_at', page.created_at
    ) ORDER BY page.created_at DESC, page.revision DESC), '[]'::jsonb) INTO result
    FROM (
        SELECT owner, filename, revision, digest, version, created_at
        FROM public.corpora_stored_revisions
        WHERE owner = p_owner AND filename = p_filename
          AND (p_before IS NULL OR (created_at, revision) < (p_before, p_before_revision))
        ORDER BY created_at DESC, revision DESC LIMIT p_limit + 1
    ) AS page;
    RETURN result;
END;
$$;

REVOKE ALL ON FUNCTION public.corpora_stored_history(uuid,text,integer,timestamptz,uuid)
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.corpora_stored_history(uuid,text,integer,timestamptz,uuid)
    TO service_role;
NOTIFY pgrst, 'reload schema';
