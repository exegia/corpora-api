-- Export holds project, head and corpus locks while checking every reference.
CREATE FUNCTION corpora_session_guard.project_link_export(
    p_actor uuid,p_session uuid,p_token_exp bigint,p_project uuid,p_references uuid[]
) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path='' SET lock_timeout='3s' AS $$
DECLARE wanted uuid; item uuid; access jsonb; unique_count integer; revisions jsonb;
BEGIN
    IF NOT corpora_session_guard.project_link_access(p_actor,p_session,p_token_exp,p_project,false) THEN
        RETURN pg_catalog.jsonb_build_object('status','not_found');
    END IF;
    IF p_references IS NULL OR cardinality(p_references) NOT BETWEEN 1 AND 10
       OR pg_catalog.array_position(p_references,NULL) IS NOT NULL THEN
        RAISE EXCEPTION 'PROJECT_LINK_EXPORT_INPUT_INVALID';
    END IF;
    SELECT count(DISTINCT id) INTO unique_count FROM pg_catalog.unnest(p_references) AS ids(id);
    IF unique_count<>cardinality(p_references) THEN RAISE EXCEPTION 'PROJECT_LINK_EXPORT_INPUT_INVALID'; END IF;
    FOR wanted IN SELECT id FROM pg_catalog.unnest(p_references) AS ids(id) ORDER BY id LOOP
        PERFORM 1 FROM public.corpora_project_link_heads
            WHERE project_id=p_project AND reference_id=wanted FOR SHARE;
        IF NOT FOUND THEN RETURN pg_catalog.jsonb_build_object('status','not_found'); END IF;
    END LOOP;
    FOR item IN
        SELECT DISTINCT id FROM (
            SELECT r.source_corpus AS id FROM public.corpora_project_link_heads h
                JOIN public.corpora_project_link_revisions r USING(project_id,reference_id,version)
                WHERE h.project_id=p_project AND h.reference_id=ANY(p_references)
            UNION
            SELECT r.target_corpus AS id FROM public.corpora_project_link_heads h
                JOIN public.corpora_project_link_revisions r USING(project_id,reference_id,version)
                WHERE h.project_id=p_project AND h.reference_id=ANY(p_references)
        ) corpora ORDER BY id
    LOOP
        access := corpora_session_guard.read_access(p_actor,p_session,p_token_exp,item);
        IF access->>'status' IS DISTINCT FROM 'ok' OR access->'head' IS NULL
           OR access->'head' = 'null'::jsonb THEN
            RETURN pg_catalog.jsonb_build_object('status','not_found');
        END IF;
    END LOOP;
    SELECT pg_catalog.jsonb_agg(pg_catalog.jsonb_build_object(
        'version',r.version,'actor_id',r.actor_id,'recorded_at',r.recorded_at,
        'reason',r.reason,'action',r.action,'reference',r.reference
    ) ORDER BY h.reference_id) INTO revisions FROM public.corpora_project_link_heads h
        JOIN public.corpora_project_link_revisions r USING(project_id,reference_id,version)
        WHERE h.project_id=p_project AND h.reference_id=ANY(p_references);
    IF revisions IS NULL OR pg_catalog.jsonb_array_length(revisions)<>cardinality(p_references) THEN
        RETURN pg_catalog.jsonb_build_object('status','not_found');
    END IF;
    PERFORM corpora_session_guard.lock_verified_session(p_actor,p_session,p_token_exp);
    RETURN pg_catalog.jsonb_build_object('status','ok','revisions',revisions);
END;
$$;
CREATE FUNCTION public.corpora_export_project_links(
    p_actor uuid,p_session uuid,p_token_exp bigint,p_project uuid,p_references uuid[]
) RETURNS jsonb LANGUAGE sql SECURITY INVOKER SET search_path='' AS $$
    SELECT corpora_session_guard.project_link_export(p_actor,p_session,p_token_exp,p_project,p_references);
$$;
REVOKE ALL ON FUNCTION corpora_session_guard.project_link_export(uuid,uuid,bigint,uuid,uuid[])
    FROM PUBLIC,anon,authenticated;
REVOKE ALL ON FUNCTION public.corpora_export_project_links(uuid,uuid,bigint,uuid,uuid[])
    FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION corpora_session_guard.project_link_export(uuid,uuid,bigint,uuid,uuid[]) TO service_role;
GRANT EXECUTE ON FUNCTION public.corpora_export_project_links(uuid,uuid,bigint,uuid,uuid[]) TO service_role;
NOTIFY pgrst,'reload schema';
