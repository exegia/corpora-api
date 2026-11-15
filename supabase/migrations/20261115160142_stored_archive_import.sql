-- Draft only: legacy readers and writers must be paused/coordinated at cutover.
-- A version check during import cannot stop an old writer after this commits.
CREATE FUNCTION public.corpora_legacy_source_stamp(p_owner uuid, p_bucket text, p_filename text)
RETURNS jsonb LANGUAGE plpgsql SECURITY INVOKER SET search_path = '' AS $$
DECLARE
    source_id uuid;
    source_version text;
BEGIN
    SELECT id, version INTO STRICT source_id, source_version FROM storage.objects
        WHERE bucket_id = p_bucket AND name = p_owner::text || '/' || p_filename;
    IF source_version IS NULL OR source_version = '' THEN
        RAISE EXCEPTION 'ARCHIVE_SOURCE_UNVERSIONED';
    END IF;
    RETURN jsonb_build_object('id', source_id, 'version', source_version);
EXCEPTION WHEN no_data_found THEN
    RAISE EXCEPTION 'ARCHIVE_NOT_FOUND';
END;
$$;

CREATE FUNCTION public.corpora_stored_import(
    p_owner uuid, p_bucket text, p_filename text, p_source_id uuid, p_source_version text,
    p_revision uuid, p_digest text, p_version text
) RETURNS void LANGUAGE plpgsql SECURITY INVOKER SET search_path = '' AS $$
DECLARE
    source_id uuid;
    source_version text;
BEGIN
    SELECT id, version INTO STRICT source_id, source_version FROM storage.objects
        WHERE bucket_id = p_bucket AND name = p_owner::text || '/' || p_filename FOR UPDATE;
    IF p_source_id IS NULL OR p_source_version IS NULL OR p_source_version = ''
        OR source_id IS DISTINCT FROM p_source_id
        OR source_version IS DISTINCT FROM p_source_version THEN
        RAISE EXCEPTION 'ARCHIVE_CONFLICT';
    END IF;
    PERFORM public.corpora_stored_create(p_owner, p_filename, p_revision, p_digest, p_version);
EXCEPTION WHEN no_data_found THEN
    RAISE EXCEPTION 'ARCHIVE_CONFLICT';
END;
$$;

REVOKE ALL ON FUNCTION public.corpora_legacy_source_stamp(uuid,text,text)
    FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.corpora_stored_import(uuid,text,text,uuid,text,uuid,text,text)
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.corpora_legacy_source_stamp(uuid,text,text) TO service_role;
GRANT EXECUTE ON FUNCTION public.corpora_stored_import(uuid,text,text,uuid,text,uuid,text,text)
    TO service_role;
NOTIFY pgrst, 'reload schema';
