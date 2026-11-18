-- A current selection requires project membership and access to both corpora.
CREATE FUNCTION corpora_session_guard.project_link_read_current(
    p_actor uuid,p_session uuid,p_token_exp bigint,p_project uuid,p_reference uuid
) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path='' SET lock_timeout='3s' AS $$
DECLARE current_row record; item uuid; access jsonb;
BEGIN
    IF NOT corpora_session_guard.project_link_access(p_actor,p_session,p_token_exp,p_project,false) THEN
        RETURN pg_catalog.jsonb_build_object('status','not_found');
    END IF;
    SELECT r.* INTO current_row FROM public.corpora_project_link_heads h
        JOIN public.corpora_project_link_revisions r USING(project_id,reference_id,version)
        WHERE h.project_id=p_project AND h.reference_id=p_reference FOR SHARE OF h;
    IF NOT FOUND THEN RETURN pg_catalog.jsonb_build_object('status','not_found'); END IF;
    FOR item IN SELECT DISTINCT id FROM (VALUES(current_row.source_corpus),(current_row.target_corpus)) AS ids(id)
        ORDER BY id LOOP
        access := corpora_session_guard.read_access(p_actor,p_session,p_token_exp,item);
        IF access->>'status' IS DISTINCT FROM 'ok' OR access->'head' IS NULL
           OR access->'head' = 'null'::jsonb THEN
            RETURN pg_catalog.jsonb_build_object('status','not_found');
        END IF;
    END LOOP;
    PERFORM corpora_session_guard.lock_verified_session(p_actor,p_session,p_token_exp);
    RETURN pg_catalog.jsonb_build_object('status','ok','revision',pg_catalog.jsonb_build_object(
        'version',current_row.version,'actor_id',current_row.actor_id,'recorded_at',current_row.recorded_at,
        'reason',current_row.reason,'action',current_row.action,'reference',current_row.reference));
END;
$$;
CREATE FUNCTION public.corpora_read_current_project_link(
    p_actor uuid,p_session uuid,p_token_exp bigint,p_project uuid,p_reference uuid
) RETURNS jsonb LANGUAGE sql SECURITY INVOKER SET search_path='' AS $$
    SELECT corpora_session_guard.project_link_read_current(p_actor,p_session,p_token_exp,p_project,p_reference);
$$;
REVOKE ALL ON FUNCTION corpora_session_guard.project_link_read_current(uuid,uuid,bigint,uuid,uuid)
    FROM PUBLIC,anon,authenticated;
REVOKE ALL ON FUNCTION public.corpora_read_current_project_link(uuid,uuid,bigint,uuid,uuid)
    FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION corpora_session_guard.project_link_read_current(uuid,uuid,bigint,uuid,uuid) TO service_role;
GRANT EXECUTE ON FUNCTION public.corpora_read_current_project_link(uuid,uuid,bigint,uuid,uuid) TO service_role;
NOTIFY pgrst,'reload schema';
