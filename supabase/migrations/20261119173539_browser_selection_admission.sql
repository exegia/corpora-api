-- Mapping is read-only and admits project members with current corpus access.
CREATE FUNCTION corpora_session_guard.browser_selection_admit(
    p_actor uuid,p_session uuid,p_token_exp bigint,p_project uuid,p_corpus uuid
) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path='' SET lock_timeout='3s' AS $$
DECLARE access jsonb;
BEGIN
    IF NOT corpora_session_guard.project_link_access(p_actor,p_session,p_token_exp,p_project,false) THEN
        RETURN pg_catalog.jsonb_build_object('status','not_found');
    END IF;
    access := corpora_session_guard.read_access(p_actor,p_session,p_token_exp,p_corpus);
    IF access->>'status' IS DISTINCT FROM 'ok' OR access->'head' IS NULL
       OR access->'head' = 'null'::jsonb THEN
        RETURN pg_catalog.jsonb_build_object('status','not_found');
    END IF;
    PERFORM corpora_session_guard.lock_verified_session(p_actor,p_session,p_token_exp);
    RETURN pg_catalog.jsonb_build_object('status','ok');
END;
$$;
CREATE FUNCTION public.corpora_admit_browser_selection(
    p_actor uuid,p_session uuid,p_token_exp bigint,p_project uuid,p_corpus uuid
) RETURNS jsonb LANGUAGE sql SECURITY INVOKER SET search_path='' AS $$
    SELECT corpora_session_guard.browser_selection_admit(p_actor,p_session,p_token_exp,p_project,p_corpus);
$$;
REVOKE ALL ON FUNCTION corpora_session_guard.browser_selection_admit(uuid,uuid,bigint,uuid,uuid)
    FROM PUBLIC,anon,authenticated;
REVOKE ALL ON FUNCTION public.corpora_admit_browser_selection(uuid,uuid,bigint,uuid,uuid)
    FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION corpora_session_guard.browser_selection_admit(uuid,uuid,bigint,uuid,uuid) TO service_role;
GRANT EXECUTE ON FUNCTION public.corpora_admit_browser_selection(uuid,uuid,bigint,uuid,uuid) TO service_role;
NOTIFY pgrst,'reload schema';
