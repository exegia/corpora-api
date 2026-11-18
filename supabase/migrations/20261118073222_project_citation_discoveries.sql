-- Citation evidence belongs to one project and one immutable corpus revision.
CREATE TABLE public.corpora_project_discoveries (
    project_id uuid NOT NULL REFERENCES public.projects(id) ON DELETE RESTRICT,
    discovery_id uuid NOT NULL,
    source_corpus uuid NOT NULL REFERENCES public.corpora_corpus_metadata(id) ON DELETE RESTRICT,
    version bigint NOT NULL CHECK (version > 0),
    PRIMARY KEY (project_id, discovery_id)
);
CREATE TABLE public.corpora_project_discovery_revisions (
    project_id uuid NOT NULL,
    discovery_id uuid NOT NULL,
    version bigint NOT NULL CHECK (version > 0),
    actor_id uuid NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    reason text NOT NULL CHECK (length(reason) BETWEEN 1 AND 2000 AND btrim(reason) <> ''),
    action text NOT NULL CHECK (action IN ('register', 'accept', 'reject')),
    evidence jsonb NOT NULL CHECK (pg_column_size(evidence) <= 65536),
    PRIMARY KEY (project_id, discovery_id, version),
    FOREIGN KEY (project_id, discovery_id)
        REFERENCES public.corpora_project_discoveries(project_id, discovery_id) ON DELETE RESTRICT
);
ALTER TABLE public.corpora_project_discoveries ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.corpora_project_discovery_revisions ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.corpora_project_discoveries, public.corpora_project_discovery_revisions
    FROM PUBLIC, anon, authenticated, service_role;
GRANT SELECT ON public.corpora_project_discoveries, public.corpora_project_discovery_revisions TO service_role;

-- Admission avoids reading a corpus for a non-editor. A retry can recover its
-- original receipt after the current corpus head has advanced.
CREATE FUNCTION corpora_session_guard.project_discovery_admit(
    p_actor uuid, p_session uuid, p_token_exp bigint, p_project uuid, p_discovery uuid,
    p_reason text, p_evidence jsonb
) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path='' SET lock_timeout='3s' AS $$
DECLARE prior record; access jsonb;
BEGIN
    IF NOT corpora_session_guard.project_link_access(p_actor,p_session,p_token_exp,p_project,true) THEN
        RETURN pg_catalog.jsonb_build_object('status','not_found');
    END IF;
    IF p_discovery IS NULL OR p_reason IS NULL OR length(p_reason) NOT BETWEEN 1 AND 2000
       OR btrim(p_reason)='' OR p_evidence IS NULL OR pg_column_size(p_evidence)>65536
       OR p_evidence->>'id' IS DISTINCT FROM p_discovery::text
       OR p_evidence->>'status' IS DISTINCT FROM 'pending' THEN
        RAISE EXCEPTION 'PROJECT_LINK_INPUT_INVALID';
    END IF;
    SELECT d.source_corpus,r.actor_id,r.reason,r.evidence INTO prior
        FROM public.corpora_project_discoveries d JOIN public.corpora_project_discovery_revisions r
        USING(project_id,discovery_id)
        WHERE d.project_id=p_project AND d.discovery_id=p_discovery AND r.version=1 FOR SHARE OF d;
    IF NOT FOUND THEN
        PERFORM corpora_session_guard.lock_verified_session(p_actor,p_session,p_token_exp);
        RETURN pg_catalog.jsonb_build_object('status','ok','exists',false);
    END IF;
    access := corpora_session_guard.read_access(p_actor,p_session,p_token_exp,prior.source_corpus);
    IF access->>'status' IS DISTINCT FROM 'ok' OR access->'head' IS NULL
       OR access->'head' = 'null'::jsonb THEN
        RETURN pg_catalog.jsonb_build_object('status','not_found');
    END IF;
    IF prior.actor_id IS DISTINCT FROM p_actor OR prior.reason IS DISTINCT FROM p_reason
       OR prior.evidence IS DISTINCT FROM p_evidence THEN
        RETURN pg_catalog.jsonb_build_object('status','conflict');
    END IF;
    PERFORM corpora_session_guard.lock_verified_session(p_actor,p_session,p_token_exp);
    RETURN pg_catalog.jsonb_build_object('status','ok','exists',true);
END;
$$;

CREATE FUNCTION corpora_session_guard.project_discovery_register(
    p_actor uuid, p_session uuid, p_token_exp bigint, p_project uuid, p_discovery uuid,
    p_reason text, p_evidence jsonb
) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path='' SET lock_timeout='3s' AS $$
DECLARE corpus uuid; access jsonb; prior record;
BEGIN
    IF NOT corpora_session_guard.project_link_access(p_actor,p_session,p_token_exp,p_project,true) THEN
        RETURN pg_catalog.jsonb_build_object('status','not_found');
    END IF;
    IF p_discovery IS NULL OR p_reason IS NULL OR length(p_reason) NOT BETWEEN 1 AND 2000
       OR btrim(p_reason)='' OR p_evidence IS NULL OR pg_column_size(p_evidence)>65536
       OR p_evidence->>'id' IS DISTINCT FROM p_discovery::text
       OR p_evidence->>'status' IS DISTINCT FROM 'pending' THEN
        RAISE EXCEPTION 'PROJECT_LINK_INPUT_INVALID';
    END IF;
    corpus := (p_evidence#>>'{source,package_id}')::uuid;
    IF corpus IS NULL THEN RAISE EXCEPTION 'PROJECT_LINK_INPUT_INVALID'; END IF;
    access := corpora_session_guard.read_access(p_actor,p_session,p_token_exp,corpus);
    IF access->>'status' IS DISTINCT FROM 'ok' OR access->'head' IS NULL
       OR access->'head' = 'null'::jsonb THEN
        RETURN pg_catalog.jsonb_build_object('status','not_found');
    END IF;
    IF NOT EXISTS (SELECT 1 FROM public.corpora_corpus_heads
        WHERE corpus_id=corpus AND commit_sha=p_evidence#>>'{source,revision}') THEN
        RETURN pg_catalog.jsonb_build_object('status','stale_corpus');
    END IF;
    INSERT INTO public.corpora_project_discoveries(project_id,discovery_id,source_corpus,version)
        VALUES(p_project,p_discovery,corpus,1) ON CONFLICT DO NOTHING;
    IF NOT FOUND THEN
        SELECT r.actor_id,r.reason,r.evidence,d.source_corpus INTO prior
        FROM public.corpora_project_discovery_revisions r
        JOIN public.corpora_project_discoveries d USING(project_id,discovery_id)
        WHERE r.project_id=p_project AND r.discovery_id=p_discovery AND r.version=1;
        access := corpora_session_guard.read_access(p_actor,p_session,p_token_exp,prior.source_corpus);
        IF access->>'status' IS DISTINCT FROM 'ok' OR access->'head' IS NULL
           OR access->'head' = 'null'::jsonb THEN
            RETURN pg_catalog.jsonb_build_object('status','not_found');
        END IF;
        IF prior.actor_id IS DISTINCT FROM p_actor OR prior.reason IS DISTINCT FROM p_reason
           OR prior.evidence IS DISTINCT FROM p_evidence THEN
            RETURN pg_catalog.jsonb_build_object('status','conflict');
        END IF;
        PERFORM corpora_session_guard.lock_verified_session(p_actor,p_session,p_token_exp);
        RETURN pg_catalog.jsonb_build_object('status','ok','version',1);
    END IF;
    INSERT INTO public.corpora_project_discovery_revisions
        (project_id,discovery_id,version,actor_id,reason,action,evidence)
        VALUES(p_project,p_discovery,1,p_actor,p_reason,'register',p_evidence);
    PERFORM corpora_session_guard.lock_verified_session(p_actor,p_session,p_token_exp);
    RETURN pg_catalog.jsonb_build_object('status','ok','version',1);
END;
$$;

CREATE FUNCTION corpora_session_guard.project_discovery_history(
    p_actor uuid, p_session uuid, p_token_exp bigint, p_project uuid, p_discovery uuid,
    p_before bigint, p_limit integer
) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path='' SET lock_timeout='3s' AS $$
DECLARE corpus uuid; access jsonb; items jsonb;
BEGIN
    IF NOT corpora_session_guard.project_link_access(p_actor,p_session,p_token_exp,p_project,false) THEN
        RETURN pg_catalog.jsonb_build_object('status','not_found');
    END IF;
    IF p_discovery IS NULL OR p_limit IS NULL OR p_limit NOT BETWEEN 1 AND 10
       OR (p_before IS NOT NULL AND p_before<1) THEN RAISE EXCEPTION 'PROJECT_LINK_INPUT_INVALID'; END IF;
    SELECT source_corpus INTO corpus FROM public.corpora_project_discoveries
        WHERE project_id=p_project AND discovery_id=p_discovery FOR SHARE;
    IF NOT FOUND THEN RETURN pg_catalog.jsonb_build_object('status','not_found'); END IF;
    access := corpora_session_guard.read_access(p_actor,p_session,p_token_exp,corpus);
    IF access->>'status' IS DISTINCT FROM 'ok' OR access->'head' IS NULL
       OR access->'head' = 'null'::jsonb THEN
        RETURN pg_catalog.jsonb_build_object('status','not_found');
    END IF;
    SELECT coalesce(pg_catalog.jsonb_agg(pg_catalog.to_jsonb(r) ORDER BY r.version DESC),'[]'::jsonb)
    INTO items FROM (SELECT version,actor_id,recorded_at,reason,action,evidence
        FROM public.corpora_project_discovery_revisions
        WHERE project_id=p_project AND discovery_id=p_discovery AND (p_before IS NULL OR version<p_before)
        ORDER BY version DESC LIMIT p_limit+1) r;
    PERFORM corpora_session_guard.lock_verified_session(p_actor,p_session,p_token_exp);
    RETURN pg_catalog.jsonb_build_object('status','ok','revisions',items);
END;
$$;

CREATE FUNCTION corpora_session_guard.project_discovery_decide(
    p_actor uuid, p_session uuid, p_token_exp bigint, p_project uuid, p_discovery uuid,
    p_expected bigint, p_reason text, p_action text
) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path='' SET lock_timeout='3s' AS $$
DECLARE current_row record; access jsonb; evidence jsonb; next_version bigint;
BEGIN
    IF NOT corpora_session_guard.project_link_access(p_actor,p_session,p_token_exp,p_project,true) THEN
        RETURN pg_catalog.jsonb_build_object('status','not_found');
    END IF;
    IF p_discovery IS NULL OR p_expected IS NULL OR p_expected<1 OR p_reason IS NULL
       OR length(p_reason) NOT BETWEEN 1 AND 2000 OR btrim(p_reason)=''
       OR p_action IS NULL OR p_action NOT IN ('accept','reject') THEN
        RAISE EXCEPTION 'PROJECT_LINK_INPUT_INVALID';
    END IF;
    SELECT h.version,h.source_corpus,r.evidence INTO current_row
        FROM public.corpora_project_discoveries h JOIN public.corpora_project_discovery_revisions r
        USING(project_id,discovery_id,version)
        WHERE h.project_id=p_project AND h.discovery_id=p_discovery FOR UPDATE OF h;
    IF NOT FOUND THEN RETURN pg_catalog.jsonb_build_object('status','not_found'); END IF;
    access := corpora_session_guard.read_access(p_actor,p_session,p_token_exp,current_row.source_corpus);
    IF access->>'status' IS DISTINCT FROM 'ok' OR access->'head' IS NULL
       OR access->'head' = 'null'::jsonb THEN
        RETURN pg_catalog.jsonb_build_object('status','not_found');
    END IF;
    IF p_expected<>current_row.version THEN RETURN pg_catalog.jsonb_build_object('status','conflict'); END IF;
    IF p_action='accept' AND NOT EXISTS (SELECT 1 FROM public.corpora_corpus_heads
        WHERE corpus_id=current_row.source_corpus AND commit_sha=current_row.evidence#>>'{source,revision}') THEN
        RETURN pg_catalog.jsonb_build_object('status','stale_corpus');
    END IF;
    IF current_row.evidence->>'status' IS DISTINCT FROM 'pending' THEN
        RETURN pg_catalog.jsonb_build_object('status','invalid_transition');
    END IF;
    evidence := pg_catalog.jsonb_set(current_row.evidence,'{status}',pg_catalog.to_jsonb(
        CASE p_action WHEN 'accept' THEN 'accepted'::text ELSE 'rejected'::text END));
    next_version := current_row.version+1;
    UPDATE public.corpora_project_discoveries SET version=next_version
        WHERE project_id=p_project AND discovery_id=p_discovery;
    INSERT INTO public.corpora_project_discovery_revisions
        (project_id,discovery_id,version,actor_id,reason,action,evidence)
        VALUES(p_project,p_discovery,next_version,p_actor,p_reason,p_action,evidence);
    PERFORM corpora_session_guard.lock_verified_session(p_actor,p_session,p_token_exp);
    RETURN pg_catalog.jsonb_build_object('status','ok','version',next_version);
END;
$$;

CREATE FUNCTION public.corpora_admit_project_discovery(
    p_actor uuid,p_session uuid,p_token_exp bigint,p_project uuid,p_discovery uuid,p_reason text,p_evidence jsonb
) RETURNS jsonb LANGUAGE sql SECURITY INVOKER SET search_path='' AS $$
    SELECT corpora_session_guard.project_discovery_admit(p_actor,p_session,p_token_exp,p_project,p_discovery,p_reason,p_evidence);
$$;
CREATE FUNCTION public.corpora_register_project_discovery(
    p_actor uuid,p_session uuid,p_token_exp bigint,p_project uuid,p_discovery uuid,p_reason text,p_evidence jsonb
) RETURNS jsonb LANGUAGE sql SECURITY INVOKER SET search_path='' AS $$
    SELECT corpora_session_guard.project_discovery_register(p_actor,p_session,p_token_exp,p_project,p_discovery,p_reason,p_evidence);
$$;
CREATE FUNCTION public.corpora_project_discovery_history(
    p_actor uuid,p_session uuid,p_token_exp bigint,p_project uuid,p_discovery uuid,p_before bigint,p_limit integer
) RETURNS jsonb LANGUAGE sql SECURITY INVOKER SET search_path='' AS $$
    SELECT corpora_session_guard.project_discovery_history(p_actor,p_session,p_token_exp,p_project,p_discovery,p_before,p_limit);
$$;
CREATE FUNCTION public.corpora_decide_project_discovery(
    p_actor uuid,p_session uuid,p_token_exp bigint,p_project uuid,p_discovery uuid,p_expected bigint,p_reason text,p_action text
) RETURNS jsonb LANGUAGE sql SECURITY INVOKER SET search_path='' AS $$
    SELECT corpora_session_guard.project_discovery_decide(p_actor,p_session,p_token_exp,p_project,p_discovery,p_expected,p_reason,p_action);
$$;
REVOKE ALL ON FUNCTION corpora_session_guard.project_discovery_admit(uuid,uuid,bigint,uuid,uuid,text,jsonb),
    corpora_session_guard.project_discovery_register(uuid,uuid,bigint,uuid,uuid,text,jsonb),
    corpora_session_guard.project_discovery_history(uuid,uuid,bigint,uuid,uuid,bigint,integer),
    corpora_session_guard.project_discovery_decide(uuid,uuid,bigint,uuid,uuid,bigint,text,text)
    FROM PUBLIC,anon,authenticated;
REVOKE ALL ON FUNCTION public.corpora_admit_project_discovery(uuid,uuid,bigint,uuid,uuid,text,jsonb),
    public.corpora_register_project_discovery(uuid,uuid,bigint,uuid,uuid,text,jsonb),
    public.corpora_project_discovery_history(uuid,uuid,bigint,uuid,uuid,bigint,integer),
    public.corpora_decide_project_discovery(uuid,uuid,bigint,uuid,uuid,bigint,text,text)
    FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION corpora_session_guard.project_discovery_admit(uuid,uuid,bigint,uuid,uuid,text,jsonb),
    corpora_session_guard.project_discovery_register(uuid,uuid,bigint,uuid,uuid,text,jsonb),
    corpora_session_guard.project_discovery_history(uuid,uuid,bigint,uuid,uuid,bigint,integer),
    corpora_session_guard.project_discovery_decide(uuid,uuid,bigint,uuid,uuid,bigint,text,text) TO service_role;
GRANT EXECUTE ON FUNCTION public.corpora_admit_project_discovery(uuid,uuid,bigint,uuid,uuid,text,jsonb),
    public.corpora_register_project_discovery(uuid,uuid,bigint,uuid,uuid,text,jsonb),
    public.corpora_project_discovery_history(uuid,uuid,bigint,uuid,uuid,bigint,integer),
    public.corpora_decide_project_discovery(uuid,uuid,bigint,uuid,uuid,bigint,text,text) TO service_role;
NOTIFY pgrst,'reload schema';
