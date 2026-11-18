CREATE OR REPLACE FUNCTION corpora_session_guard.project_link_decide(
    p_actor uuid,p_session uuid,p_token_exp bigint,p_project uuid,p_reference uuid,
    p_expected bigint,p_reason text,p_action text
) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path='' SET lock_timeout='3s' AS $$
DECLARE previous record; item uuid; access jsonb; value jsonb; next_version bigint;
BEGIN
    IF NOT corpora_session_guard.project_link_access(p_actor,p_session,p_token_exp,p_project,true) THEN
        RETURN pg_catalog.jsonb_build_object('status','not_found');
    END IF;
    IF p_reference IS NULL OR p_expected IS NULL OR p_expected < 1 OR p_reason IS NULL
       OR length(p_reason) NOT BETWEEN 1 AND 2000 OR btrim(p_reason)=''
       OR p_action NOT IN ('reject','reopen','resolve','approve') OR p_action IS NULL THEN
        RAISE EXCEPTION 'PROJECT_LINK_INPUT_INVALID';
    END IF;
    SELECT h.version,r.reference,r.source_corpus,r.target_corpus INTO previous
        FROM public.corpora_project_link_heads h
        JOIN public.corpora_project_link_revisions r USING(project_id,reference_id,version)
        WHERE h.project_id=p_project AND h.reference_id=p_reference FOR UPDATE OF h;
    IF NOT FOUND THEN RETURN pg_catalog.jsonb_build_object('status','not_found'); END IF;
    IF p_expected <> previous.version THEN RETURN pg_catalog.jsonb_build_object('status','conflict'); END IF;
    FOR item IN SELECT DISTINCT id FROM (VALUES(previous.source_corpus),(previous.target_corpus)) AS ids(id)
        ORDER BY id LOOP
        access := corpora_session_guard.read_access(p_actor,p_session,p_token_exp,item);
        IF access->>'status' IS DISTINCT FROM 'ok' OR access->'head' IS NULL
           OR access->'head' = 'null'::jsonb THEN
            RETURN pg_catalog.jsonb_build_object('status','not_found');
        END IF;
    END LOOP;
    IF p_action IN ('resolve','approve') AND (
        access IS NULL OR NOT EXISTS (
            SELECT 1 FROM public.corpora_corpus_heads
            WHERE corpus_id=previous.source_corpus AND commit_sha=previous.reference#>>'{source,revision}') OR
        NOT EXISTS (
            SELECT 1 FROM public.corpora_corpus_heads
            WHERE corpus_id=previous.target_corpus AND commit_sha=previous.reference#>>'{target,revision}')) THEN
        RETURN pg_catalog.jsonb_build_object('status','stale_corpus');
    END IF;
    IF (p_action='reject' AND previous.reference->>'review' IS DISTINCT FROM 'pending') OR
       (p_action='reopen' AND previous.reference->>'review' IS DISTINCT FROM 'rejected') OR
       (p_action='resolve' AND (previous.reference->>'review' IS DISTINCT FROM 'pending' OR
           previous.reference->>'resolution' IS DISTINCT FROM 'unresolved')) OR
       (p_action='approve' AND (previous.reference->>'review' IS DISTINCT FROM 'pending' OR
           previous.reference->>'resolution' IS DISTINCT FROM 'resolved')) THEN
        RETURN pg_catalog.jsonb_build_object('status','invalid_transition');
    END IF;
    value := CASE p_action
        WHEN 'resolve' THEN pg_catalog.jsonb_set(previous.reference,'{resolution}', '"resolved"'::jsonb)
        WHEN 'approve' THEN pg_catalog.jsonb_set(previous.reference,'{review}', '"approved"'::jsonb)
        WHEN 'reject' THEN pg_catalog.jsonb_set(previous.reference,'{review}', '"rejected"'::jsonb)
        ELSE pg_catalog.jsonb_set(previous.reference,'{review}', '"pending"'::jsonb)
    END;
    next_version := previous.version+1;
    UPDATE public.corpora_project_link_heads SET version=next_version
        WHERE project_id=p_project AND reference_id=p_reference;
    INSERT INTO public.corpora_project_link_revisions(project_id,reference_id,version,actor_id,reason,
        action,source_corpus,target_corpus,reference)
        VALUES(p_project,p_reference,next_version,p_actor,p_reason,p_action,
            previous.source_corpus,previous.target_corpus,value);
    PERFORM corpora_session_guard.lock_verified_session(p_actor,p_session,p_token_exp);
    RETURN pg_catalog.jsonb_build_object('status','ok','version',next_version);
END;
$$;

NOTIFY pgrst,'reload schema';

-- Resolution examines the current revision only; history has its own broader
-- per-revision access checks for callers who request older records.
CREATE FUNCTION corpora_session_guard.project_link_current(
    p_actor uuid,p_session uuid,p_token_exp bigint,p_project uuid,p_reference uuid
) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path='' SET lock_timeout='3s' AS $$
DECLARE current_row record; item uuid; access jsonb;
BEGIN
    IF NOT corpora_session_guard.project_link_access(p_actor,p_session,p_token_exp,p_project,true) THEN
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
CREATE FUNCTION public.corpora_current_project_link(
    p_actor uuid,p_session uuid,p_token_exp bigint,p_project uuid,p_reference uuid
) RETURNS jsonb LANGUAGE sql SECURITY INVOKER SET search_path='' AS $$
    SELECT corpora_session_guard.project_link_current(p_actor,p_session,p_token_exp,p_project,p_reference);
$$;
REVOKE ALL ON FUNCTION corpora_session_guard.project_link_current(uuid,uuid,bigint,uuid,uuid)
    FROM PUBLIC,anon,authenticated;
REVOKE ALL ON FUNCTION public.corpora_current_project_link(uuid,uuid,bigint,uuid,uuid)
    FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION corpora_session_guard.project_link_current(uuid,uuid,bigint,uuid,uuid) TO service_role;
GRANT EXECUTE ON FUNCTION public.corpora_current_project_link(uuid,uuid,bigint,uuid,uuid) TO service_role;
NOTIFY pgrst,'reload schema';
