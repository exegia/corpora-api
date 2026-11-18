-- Version history is editor-only. The API verifies the JWT before supplying
-- these claims; neither browser roles nor anonymous clients can call this RPC.
CREATE FUNCTION corpora_session_guard.list_corpus_versions(
    p_actor uuid, p_session uuid, p_token_exp bigint, p_corpus uuid,
    p_before bigint, p_limit integer
) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = ''
SET lock_timeout = '3s' AS $$
DECLARE
    m public.corpora_corpus_metadata%ROWTYPE;
    head_sequence bigint;
    page jsonb;
    last_sequence bigint;
    has_more boolean;
BEGIN
    PERFORM corpora_session_guard.lock_verified_session(p_actor,p_session,p_token_exp);
    IF p_corpus IS NULL OR p_limit IS NULL OR p_limit NOT BETWEEN 1 AND 100
       OR (p_before IS NOT NULL AND p_before < 1) THEN
        RAISE EXCEPTION 'CORPUS_VERSION_HISTORY_INPUT_INVALID';
    END IF;
    SELECT * INTO m FROM public.corpora_corpus_metadata WHERE id=p_corpus AND (
        owner_id=p_actor OR EXISTS (
            SELECT 1 FROM corpora_session_guard.superadmins WHERE user_id=p_actor
        )
    ) FOR SHARE;
    IF NOT FOUND OR NOT EXISTS (
        SELECT 1 FROM public.corpora_corpus_access WHERE corpus_id=p_corpus AND deleted_at IS NULL
    ) THEN
        PERFORM corpora_session_guard.lock_verified_session(p_actor,p_session,p_token_exp);
        RETURN pg_catalog.jsonb_build_object('status','not_found');
    END IF;
    SELECT sequence INTO head_sequence FROM public.corpora_corpus_heads WHERE corpus_id=m.id;
    SELECT coalesce(pg_catalog.jsonb_agg(pg_catalog.jsonb_build_object(
        'sequence', item.sequence, 'commit_sha', item.commit_sha, 'parent_sha', item.parent_sha,
        'operation_id', item.operation_id, 'actor_id', item.actor_id, 'created_at', item.created_at
    ) ORDER BY item.sequence DESC), '[]'::jsonb), min(item.sequence)
    INTO page, last_sequence FROM (
        SELECT v.sequence,v.commit_sha,v.parent_sha,v.operation_id,o.actor_id,v.created_at
        FROM public.corpora_corpus_versions v
        JOIN public.corpora_corpus_write_ops o ON o.id=v.operation_id
            AND o.corpus_id=v.corpus_id AND o.state='completed'
        WHERE v.corpus_id=m.id AND (p_before IS NULL OR v.sequence<p_before)
        ORDER BY v.sequence DESC LIMIT p_limit
    ) item;
    SELECT EXISTS (
        SELECT 1 FROM public.corpora_corpus_versions v
        JOIN public.corpora_corpus_write_ops o ON o.id=v.operation_id
            AND o.corpus_id=v.corpus_id AND o.state='completed'
        WHERE v.corpus_id=m.id AND last_sequence IS NOT NULL AND v.sequence<last_sequence
    ) INTO has_more;
    PERFORM corpora_session_guard.lock_verified_session(p_actor,p_session,p_token_exp);
    RETURN pg_catalog.jsonb_build_object(
        'status','ok','corpus_id',m.id,'current_sequence',head_sequence,
        'versions',page,'next_before',CASE WHEN has_more THEN last_sequence ELSE NULL END
    );
END;
$$;
CREATE FUNCTION public.corpora_list_corpus_versions(
    p_actor uuid, p_session uuid, p_token_exp bigint, p_corpus uuid,
    p_before bigint, p_limit integer
) RETURNS jsonb LANGUAGE sql SECURITY INVOKER SET search_path = '' AS $$
    SELECT corpora_session_guard.list_corpus_versions(
        p_actor,p_session,p_token_exp,p_corpus,p_before,p_limit
    );
$$;
REVOKE ALL ON FUNCTION corpora_session_guard.list_corpus_versions(uuid,uuid,bigint,uuid,bigint,integer)
    FROM PUBLIC,anon,authenticated;
REVOKE ALL ON FUNCTION public.corpora_list_corpus_versions(uuid,uuid,bigint,uuid,bigint,integer)
    FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION corpora_session_guard.list_corpus_versions(uuid,uuid,bigint,uuid,bigint,integer)
    TO service_role;
GRANT EXECUTE ON FUNCTION public.corpora_list_corpus_versions(uuid,uuid,bigint,uuid,bigint,integer)
    TO service_role;
NOTIFY pgrst, 'reload schema';
