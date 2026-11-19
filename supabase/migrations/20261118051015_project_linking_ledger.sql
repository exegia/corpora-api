-- Project link decisions are append-only and cannot be written by API roles.
CREATE TABLE public.corpora_project_link_heads (
    project_id uuid NOT NULL REFERENCES public.projects(id) ON DELETE RESTRICT,
    reference_id uuid NOT NULL,
    version bigint NOT NULL CHECK (version > 0),
    PRIMARY KEY(project_id,reference_id)
);
CREATE TABLE public.corpora_project_link_revisions (
    project_id uuid NOT NULL,
    reference_id uuid NOT NULL,
    version bigint NOT NULL CHECK (version > 0),
    actor_id uuid NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    reason text NOT NULL CHECK (length(reason) BETWEEN 1 AND 2000 AND btrim(reason) <> ''),
    action text NOT NULL CHECK (action IN ('edit','resolve','approve','reject','reopen')),
    source_corpus uuid NOT NULL REFERENCES public.corpora_corpus_metadata(id) ON DELETE RESTRICT,
    target_corpus uuid NOT NULL REFERENCES public.corpora_corpus_metadata(id) ON DELETE RESTRICT,
    reference jsonb NOT NULL CHECK (pg_column_size(reference) <= 65536),
    PRIMARY KEY(project_id,reference_id,version),
    FOREIGN KEY(project_id,reference_id) REFERENCES public.corpora_project_link_heads(project_id,reference_id)
        ON DELETE RESTRICT
);
ALTER TABLE public.corpora_project_link_heads ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.corpora_project_link_revisions ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.corpora_project_link_heads,public.corpora_project_link_revisions
    FROM PUBLIC,anon,authenticated,service_role;
GRANT SELECT ON public.corpora_project_link_heads,public.corpora_project_link_revisions TO service_role;

-- Lock the project before corpus rows. Membership and sharing changes take an
-- exclusive lock on those same rows, so authorization is checked after revocation.
CREATE FUNCTION corpora_session_guard.project_link_access(
    p_actor uuid,p_session uuid,p_token_exp bigint,p_project uuid,p_edit boolean
) RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path='' SET lock_timeout='3s' AS $$
DECLARE project public.projects%ROWTYPE; manager boolean;
BEGIN
    PERFORM corpora_session_guard.lock_verified_session(p_actor,p_session,p_token_exp);
    IF p_project IS NULL OR p_edit IS NULL THEN RAISE EXCEPTION 'PROJECT_LINK_INPUT_INVALID'; END IF;
    SELECT * INTO project FROM public.projects WHERE id=p_project FOR SHARE;
    IF NOT FOUND THEN RETURN false; END IF;
    manager := project.owner_id IS NOT DISTINCT FROM p_actor OR EXISTS (
        SELECT 1 FROM corpora_session_guard.superadmins WHERE user_id=p_actor);
    RETURN manager OR (NOT p_edit AND EXISTS (
        SELECT 1 FROM public.corpora_project_members WHERE project_id=p_project AND user_id=p_actor));
END;
$$;
REVOKE ALL ON FUNCTION corpora_session_guard.project_link_access(uuid,uuid,bigint,uuid,boolean)
    FROM PUBLIC,anon,authenticated,service_role;

CREATE FUNCTION corpora_session_guard.project_link_save(
    p_actor uuid,p_session uuid,p_token_exp bigint,p_project uuid,p_reference uuid,
    p_expected bigint,p_reason text,p_value jsonb
) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path='' SET lock_timeout='3s' AS $$
DECLARE source_id uuid; target_id uuid; item uuid; access jsonb; previous record; next_version bigint;
BEGIN
    IF NOT corpora_session_guard.project_link_access(p_actor,p_session,p_token_exp,p_project,true) THEN
        RETURN pg_catalog.jsonb_build_object('status','not_found');
    END IF;
    IF p_reference IS NULL OR p_reason IS NULL OR length(p_reason) NOT BETWEEN 1 AND 2000
       OR btrim(p_reason)='' OR p_value IS NULL OR pg_column_size(p_value)>65536
       OR p_value->>'id' IS DISTINCT FROM p_reference::text
       OR p_value->>'resolution' IS DISTINCT FROM 'unresolved'
       OR p_value->>'review' IS DISTINCT FROM 'pending'
       OR p_value->>'publication' IS DISTINCT FROM 'draft'
       OR (p_expected IS NOT NULL AND p_expected < 1) THEN
        RAISE EXCEPTION 'PROJECT_LINK_INPUT_INVALID';
    END IF;
    source_id := (p_value#>>'{source,package_id}')::uuid;
    target_id := (p_value#>>'{target,package_id}')::uuid;
    IF source_id IS NULL OR target_id IS NULL THEN RAISE EXCEPTION 'PROJECT_LINK_INPUT_INVALID'; END IF;
    SELECT h.version,r.reference,r.source_corpus,r.target_corpus INTO previous
        FROM public.corpora_project_link_heads h
        JOIN public.corpora_project_link_revisions r USING(project_id,reference_id,version)
        WHERE h.project_id=p_project AND h.reference_id=p_reference FOR UPDATE OF h;
    IF FOUND AND p_expected IS DISTINCT FROM previous.version THEN
        RETURN pg_catalog.jsonb_build_object('status','conflict');
    ELSIF NOT FOUND AND p_expected IS NOT NULL THEN
        RETURN pg_catalog.jsonb_build_object('status','conflict');
    END IF;
    FOR item IN SELECT DISTINCT id FROM (VALUES(source_id),(target_id),
        (previous.source_corpus),(previous.target_corpus)) AS ids(id)
        WHERE id IS NOT NULL ORDER BY id LOOP
        access := corpora_session_guard.read_access(p_actor,p_session,p_token_exp,item);
        IF access->>'status' IS DISTINCT FROM 'ok' OR access->'head' IS NULL
           OR access->'head' = 'null'::jsonb THEN
            RETURN pg_catalog.jsonb_build_object('status','not_found');
        END IF;
    END LOOP;
    IF NOT EXISTS (SELECT 1 FROM public.corpora_corpus_versions
        WHERE corpus_id=source_id AND commit_sha=p_value#>>'{source,revision}') OR NOT EXISTS (
        SELECT 1 FROM public.corpora_corpus_versions
        WHERE corpus_id=target_id AND commit_sha=p_value#>>'{target,revision}') THEN
        RETURN pg_catalog.jsonb_build_object('status','not_found');
    END IF;
    IF previous.version IS NOT NULL THEN
        IF previous.reference->>'review' IS DISTINCT FROM 'pending' THEN
            RETURN pg_catalog.jsonb_build_object('status','invalid_transition');
        END IF;
        -- A later edit retains the first author's provenance.
        p_value := pg_catalog.jsonb_set(p_value,'{provenance}',previous.reference->'provenance');
        next_version := previous.version+1;
        UPDATE public.corpora_project_link_heads SET version=next_version
            WHERE project_id=p_project AND reference_id=p_reference;
    ELSE
        IF p_value#>>'{provenance,agent_id}' IS DISTINCT FROM p_actor::text OR
           p_value#>>'{provenance,origin}' IS DISTINCT FROM 'manual' THEN
            RAISE EXCEPTION 'PROJECT_LINK_INPUT_INVALID';
        END IF;
        next_version := 1;
        INSERT INTO public.corpora_project_link_heads(project_id,reference_id,version)
            VALUES(p_project,p_reference,next_version) ON CONFLICT DO NOTHING;
        IF NOT FOUND THEN RETURN pg_catalog.jsonb_build_object('status','conflict'); END IF;
    END IF;
    INSERT INTO public.corpora_project_link_revisions(project_id,reference_id,version,actor_id,reason,
        action,source_corpus,target_corpus,reference)
        VALUES(p_project,p_reference,next_version,p_actor,p_reason,'edit',source_id,target_id,p_value);
    PERFORM corpora_session_guard.lock_verified_session(p_actor,p_session,p_token_exp);
    RETURN pg_catalog.jsonb_build_object('status','ok','version',next_version);
END;
$$;

CREATE FUNCTION corpora_session_guard.project_link_history(
    p_actor uuid,p_session uuid,p_token_exp bigint,p_project uuid,p_reference uuid,
    p_before bigint,p_limit integer
) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path='' SET lock_timeout='3s' AS $$
DECLARE items jsonb; item uuid; access jsonb; ids uuid[];
BEGIN
    IF NOT corpora_session_guard.project_link_access(p_actor,p_session,p_token_exp,p_project,false) THEN
        RETURN pg_catalog.jsonb_build_object('status','not_found');
    END IF;
    IF p_reference IS NULL OR p_limit IS NULL OR p_limit NOT BETWEEN 1 AND 10
       OR (p_before IS NOT NULL AND p_before < 1) THEN
        RAISE EXCEPTION 'PROJECT_LINK_INPUT_INVALID';
    END IF;
    PERFORM 1 FROM public.corpora_project_link_heads
        WHERE project_id=p_project AND reference_id=p_reference FOR SHARE;
    IF NOT FOUND THEN
        RETURN pg_catalog.jsonb_build_object('status','not_found');
    END IF;
    SELECT pg_catalog.array_agg(DISTINCT x.corpus ORDER BY x.corpus) INTO ids
    FROM (SELECT source_corpus,target_corpus FROM public.corpora_project_link_revisions
        WHERE project_id=p_project AND reference_id=p_reference AND version IN (
            SELECT version FROM public.corpora_project_link_heads
                WHERE project_id=p_project AND reference_id=p_reference
            UNION
            (SELECT version FROM public.corpora_project_link_revisions
                WHERE project_id=p_project AND reference_id=p_reference
                    AND (p_before IS NULL OR version<p_before)
                ORDER BY version DESC LIMIT p_limit+1))) r
    CROSS JOIN LATERAL (VALUES(r.source_corpus),(r.target_corpus)) AS x(corpus);
    FOREACH item IN ARRAY coalesce(ids,ARRAY[]::uuid[]) LOOP
        access := corpora_session_guard.read_access(p_actor,p_session,p_token_exp,item);
        IF access->>'status' IS DISTINCT FROM 'ok' OR access->'head' IS NULL
           OR access->'head' = 'null'::jsonb THEN
            RETURN pg_catalog.jsonb_build_object('status','not_found');
        END IF;
    END LOOP;
    SELECT coalesce(pg_catalog.jsonb_agg(pg_catalog.to_jsonb(r) ORDER BY r.version DESC),'[]'::jsonb)
    INTO items FROM (SELECT version,actor_id,recorded_at,reason,action,reference
        FROM public.corpora_project_link_revisions
        WHERE project_id=p_project AND reference_id=p_reference
          AND (p_before IS NULL OR version<p_before)
        ORDER BY version DESC LIMIT p_limit+1) r;
    PERFORM corpora_session_guard.lock_verified_session(p_actor,p_session,p_token_exp);
    RETURN pg_catalog.jsonb_build_object('status','ok','revisions',items);
END;
$$;

CREATE FUNCTION public.corpora_save_project_link(
    p_actor uuid,p_session uuid,p_token_exp bigint,p_project uuid,p_reference uuid,
    p_expected bigint,p_reason text,p_value jsonb
) RETURNS jsonb LANGUAGE sql SECURITY INVOKER SET search_path='' AS $$
    SELECT corpora_session_guard.project_link_save(
        p_actor,p_session,p_token_exp,p_project,p_reference,p_expected,p_reason,p_value);
$$;
CREATE FUNCTION public.corpora_project_link_history(
    p_actor uuid,p_session uuid,p_token_exp bigint,p_project uuid,p_reference uuid,
    p_before bigint,p_limit integer
) RETURNS jsonb LANGUAGE sql SECURITY INVOKER SET search_path='' AS $$
    SELECT corpora_session_guard.project_link_history(
        p_actor,p_session,p_token_exp,p_project,p_reference,p_before,p_limit);
$$;
REVOKE ALL ON FUNCTION corpora_session_guard.project_link_save(uuid,uuid,bigint,uuid,uuid,bigint,text,jsonb)
    FROM PUBLIC,anon,authenticated;
REVOKE ALL ON FUNCTION corpora_session_guard.project_link_history(uuid,uuid,bigint,uuid,uuid,bigint,integer)
    FROM PUBLIC,anon,authenticated;
REVOKE ALL ON FUNCTION public.corpora_save_project_link(uuid,uuid,bigint,uuid,uuid,bigint,text,jsonb)
    FROM PUBLIC,anon,authenticated;
REVOKE ALL ON FUNCTION public.corpora_project_link_history(uuid,uuid,bigint,uuid,uuid,bigint,integer)
    FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION corpora_session_guard.project_link_save(uuid,uuid,bigint,uuid,uuid,bigint,text,jsonb) TO service_role;
GRANT EXECUTE ON FUNCTION corpora_session_guard.project_link_history(uuid,uuid,bigint,uuid,uuid,bigint,integer) TO service_role;
GRANT EXECUTE ON FUNCTION public.corpora_save_project_link(uuid,uuid,bigint,uuid,uuid,bigint,text,jsonb) TO service_role;
GRANT EXECUTE ON FUNCTION public.corpora_project_link_history(uuid,uuid,bigint,uuid,uuid,bigint,integer) TO service_role;
NOTIFY pgrst,'reload schema';

CREATE FUNCTION corpora_session_guard.project_link_decide(
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
       OR p_action NOT IN ('reject','reopen') OR p_action IS NULL THEN
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
    IF (p_action='reject' AND previous.reference->>'review' IS DISTINCT FROM 'pending') OR
       (p_action='reopen' AND previous.reference->>'review' IS DISTINCT FROM 'rejected') THEN
        RETURN pg_catalog.jsonb_build_object('status','invalid_transition');
    END IF;
    value := pg_catalog.jsonb_set(previous.reference,'{review}',
        pg_catalog.to_jsonb(CASE WHEN p_action='reject' THEN 'rejected'::text ELSE 'pending'::text END));
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
CREATE FUNCTION public.corpora_decide_project_link(
    p_actor uuid,p_session uuid,p_token_exp bigint,p_project uuid,p_reference uuid,
    p_expected bigint,p_reason text,p_action text
) RETURNS jsonb LANGUAGE sql SECURITY INVOKER SET search_path='' AS $$
    SELECT corpora_session_guard.project_link_decide(
        p_actor,p_session,p_token_exp,p_project,p_reference,p_expected,p_reason,p_action);
$$;
REVOKE ALL ON FUNCTION corpora_session_guard.project_link_decide(uuid,uuid,bigint,uuid,uuid,bigint,text,text)
    FROM PUBLIC,anon,authenticated;
REVOKE ALL ON FUNCTION public.corpora_decide_project_link(uuid,uuid,bigint,uuid,uuid,bigint,text,text)
    FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION corpora_session_guard.project_link_decide(uuid,uuid,bigint,uuid,uuid,bigint,text,text) TO service_role;
GRANT EXECUTE ON FUNCTION public.corpora_decide_project_link(uuid,uuid,bigint,uuid,uuid,bigint,text,text) TO service_role;
NOTIFY pgrst,'reload schema';
