-- Read-only import planning. The plan is advisory and must be recomputed at apply time.
CREATE FUNCTION corpora_session_guard.project_link_import_preview(
    p_actor uuid,p_session uuid,p_token_exp bigint,p_project uuid,p_items jsonb
) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path='' SET lock_timeout='3s' AS $$
DECLARE candidate jsonb; wanted uuid; corpus uuid; access jsonb; items jsonb; unique_count integer;
BEGIN
    IF NOT corpora_session_guard.project_link_access(p_actor,p_session,p_token_exp,p_project,true) THEN
        RETURN pg_catalog.jsonb_build_object('status','not_found');
    END IF;
    IF pg_catalog.jsonb_typeof(p_items) IS DISTINCT FROM 'array'
       OR pg_catalog.jsonb_array_length(p_items) NOT BETWEEN 1 AND 10
       OR pg_catalog.pg_column_size(p_items)>350000 THEN
        RAISE EXCEPTION 'PROJECT_LINK_IMPORT_INVALID';
    END IF;
    SELECT count(DISTINCT (x->>'id')::uuid) INTO unique_count
        FROM pg_catalog.jsonb_array_elements(p_items) x;
    IF unique_count<>pg_catalog.jsonb_array_length(p_items) THEN
        RAISE EXCEPTION 'PROJECT_LINK_IMPORT_INVALID';
    END IF;
    FOR candidate IN SELECT value FROM pg_catalog.jsonb_array_elements(p_items) ORDER BY value->>'id' LOOP
        IF pg_catalog.jsonb_typeof(candidate) IS DISTINCT FROM 'object'
           OR candidate->>'id' IS NULL OR candidate->'source' IS NULL OR candidate->'target' IS NULL
           OR candidate->>'relationship' IS NULL OR candidate#>>'{source,package_id}' IS NULL
           OR candidate#>>'{target,package_id}' IS NULL OR candidate#>>'{source,revision}' IS NULL
           OR candidate#>>'{target,revision}' IS NULL THEN
            RAISE EXCEPTION 'PROJECT_LINK_IMPORT_INVALID';
        END IF;
        wanted := (candidate->>'id')::uuid;
        PERFORM 1 FROM public.corpora_project_link_heads
            WHERE project_id=p_project AND reference_id=wanted FOR SHARE;
    END LOOP;
    -- Check both incoming and existing corpora before revealing a conflict.
    FOR corpus IN
        SELECT DISTINCT id FROM (
            SELECT (x#>>'{source,package_id}')::uuid AS id FROM pg_catalog.jsonb_array_elements(p_items) x
            UNION ALL SELECT (x#>>'{target,package_id}')::uuid FROM pg_catalog.jsonb_array_elements(p_items) x
            UNION ALL SELECT r.source_corpus FROM public.corpora_project_link_heads h
                JOIN public.corpora_project_link_revisions r USING(project_id,reference_id,version)
                WHERE h.project_id=p_project AND h.reference_id IN (
                    SELECT (x->>'id')::uuid FROM pg_catalog.jsonb_array_elements(p_items) x)
            UNION ALL SELECT r.target_corpus FROM public.corpora_project_link_heads h
                JOIN public.corpora_project_link_revisions r USING(project_id,reference_id,version)
                WHERE h.project_id=p_project AND h.reference_id IN (
                    SELECT (x->>'id')::uuid FROM pg_catalog.jsonb_array_elements(p_items) x)
        ) ids WHERE id IS NOT NULL ORDER BY id
    LOOP
        access := corpora_session_guard.read_access(p_actor,p_session,p_token_exp,corpus);
        IF access->>'status' IS DISTINCT FROM 'ok' OR access->'head' IS NULL
           OR access->'head' = 'null'::jsonb THEN
            RETURN pg_catalog.jsonb_build_object('status','not_found');
        END IF;
    END LOOP;
    FOR candidate IN SELECT value FROM pg_catalog.jsonb_array_elements(p_items) LOOP
        IF NOT EXISTS (SELECT 1 FROM public.corpora_corpus_versions WHERE
            corpus_id=(candidate#>>'{source,package_id}')::uuid
            AND commit_sha=candidate#>>'{source,revision}') OR NOT EXISTS (
            SELECT 1 FROM public.corpora_corpus_versions WHERE
            corpus_id=(candidate#>>'{target,package_id}')::uuid
            AND commit_sha=candidate#>>'{target,revision}') THEN
            RETURN pg_catalog.jsonb_build_object('status','not_found');
        END IF;
    END LOOP;
    SELECT pg_catalog.jsonb_agg(pg_catalog.jsonb_build_object(
        'reference_id',x->>'id',
        'outcome',CASE WHEN h.version IS NULL THEN 'new'
            WHEN r.reference->'source'=x->'source' AND r.reference->'target'=x->'target'
                AND r.reference->>'relationship'=x->>'relationship' THEN 'unchanged'
            ELSE 'conflict' END,
        'current_version',h.version
    ) ORDER BY (x->>'id')::uuid) INTO items
    FROM pg_catalog.jsonb_array_elements(p_items) x
    LEFT JOIN public.corpora_project_link_heads h
        ON h.project_id=p_project AND h.reference_id=(x->>'id')::uuid
    LEFT JOIN public.corpora_project_link_revisions r USING(project_id,reference_id,version);
    PERFORM corpora_session_guard.lock_verified_session(p_actor,p_session,p_token_exp);
    RETURN pg_catalog.jsonb_build_object('status','ok','items',items);
END;
$$;
CREATE FUNCTION public.corpora_preview_project_link_import(
    p_actor uuid,p_session uuid,p_token_exp bigint,p_project uuid,p_items jsonb
) RETURNS jsonb LANGUAGE sql SECURITY INVOKER SET search_path='' AS $$
    SELECT corpora_session_guard.project_link_import_preview(p_actor,p_session,p_token_exp,p_project,p_items);
$$;
REVOKE ALL ON FUNCTION corpora_session_guard.project_link_import_preview(uuid,uuid,bigint,uuid,jsonb)
    FROM PUBLIC,anon,authenticated;
REVOKE ALL ON FUNCTION public.corpora_preview_project_link_import(uuid,uuid,bigint,uuid,jsonb)
    FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION corpora_session_guard.project_link_import_preview(uuid,uuid,bigint,uuid,jsonb) TO service_role;
GRANT EXECUTE ON FUNCTION public.corpora_preview_project_link_import(uuid,uuid,bigint,uuid,jsonb) TO service_role;
NOTIFY pgrst,'reload schema';
