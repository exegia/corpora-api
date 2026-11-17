-- Only the trusted API may supply verified JWT claims. Browser roles cannot
-- call these RPCs or obtain worker claims. All privileged SQL stays private.
CREATE FUNCTION corpora_session_guard.creation_status(
    p_actor uuid, p_session uuid, p_token_exp bigint, p_corpus uuid
) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = ''
SET lock_timeout = '3s' AS $$
DECLARE
    m public.corpora_corpus_metadata%ROWTYPE;
    o public.corpora_corpus_provision_ops%ROWTYPE;
    b public.corpora_corpus_bootstrap%ROWTYPE;
    creation_state text := 'reserved';
BEGIN
    PERFORM corpora_session_guard.lock_verified_session(p_actor, p_session, p_token_exp);
    SELECT * INTO m FROM public.corpora_corpus_metadata WHERE id = p_corpus AND (
        owner_id = p_actor OR EXISTS (
            SELECT 1 FROM corpora_session_guard.superadmins WHERE user_id = p_actor
        )
    );
    IF NOT FOUND THEN
        RETURN pg_catalog.jsonb_build_object('status', 'not_found');
    END IF;
    SELECT * INTO o FROM public.corpora_corpus_provision_ops WHERE corpus_id = m.id;
    IF FOUND THEN
        IF o.repo_id <> m.hf_repo_id OR o.repo_type <> m.hf_repo_type THEN
            RAISE EXCEPTION 'CORPUS_CREATION_INVALID';
        END IF;
        CASE o.state
        WHEN 'completed' THEN
            SELECT * INTO b FROM public.corpora_corpus_bootstrap
                WHERE corpus_id = m.id AND operation_id = o.id AND worker_claim = o.worker_claim;
            IF NOT FOUND THEN RAISE EXCEPTION 'CORPUS_CREATION_INVALID'; END IF;
            creation_state := 'repository_created';
        WHEN 'uncertain' THEN creation_state := 'needs_reconciliation';
        ELSE creation_state := 'provisioning';
        END CASE;
    END IF;
    PERFORM corpora_session_guard.lock_verified_session(p_actor, p_session, p_token_exp);
    RETURN pg_catalog.jsonb_build_object(
        'status', 'existing', 'id', m.id, 'title', m.title,
        'state', creation_state, 'bootstrap_sha', b.commit_sha
    );
END;
$$;

CREATE FUNCTION corpora_session_guard.begin_creation(
    p_actor uuid, p_session uuid, p_token_exp bigint, p_corpus uuid,
    p_title text, p_organization text
) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = ''
SET lock_timeout = '3s' AS $$
DECLARE
    m public.corpora_corpus_metadata%ROWTYPE;
    snapshot jsonb;
    admission jsonb;
    repo text;
BEGIN
    PERFORM corpora_session_guard.lock_verified_session(p_actor, p_session, p_token_exp);
    IF p_corpus IS NULL OR p_title IS NULL OR length(btrim(p_title)) NOT BETWEEN 1 AND 512
       OR p_organization IS NULL
       OR p_organization !~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$' THEN
        RAISE EXCEPTION 'CORPUS_CREATION_INPUT_INVALID';
    END IF;
    repo := p_organization || '/corpus-' || p_corpus::text;
    INSERT INTO public.corpora_corpus_metadata (id,owner_id,title,hf_repo_id,hf_repo_type)
        VALUES (p_corpus,p_actor,btrim(p_title),repo,'dataset') ON CONFLICT DO NOTHING;
    SELECT * INTO m FROM public.corpora_corpus_metadata WHERE id = p_corpus FOR UPDATE;
    IF NOT FOUND OR m.owner_id <> p_actor OR m.title <> btrim(p_title)
       OR m.hf_repo_id <> repo OR m.hf_repo_type <> 'dataset' THEN
        RETURN pg_catalog.jsonb_build_object('status', 'conflict');
    END IF;
    snapshot := corpora_session_guard.creation_status(p_actor,p_session,p_token_exp,p_corpus);
    IF snapshot->>'state' <> 'reserved' THEN RETURN snapshot; END IF;
    admission := corpora_session_guard.admit_provision(p_actor,p_session,p_token_exp,p_corpus);
    IF admission->>'status' <> 'ok' THEN
        RAISE EXCEPTION 'CORPUS_CREATION_INVALID';
    END IF;
    PERFORM corpora_session_guard.lock_verified_session(p_actor,p_session,p_token_exp);
    RETURN admission || pg_catalog.jsonb_build_object('status','claimed','id',m.id,'title',m.title);
END;
$$;

ALTER FUNCTION corpora_session_guard.creation_status(uuid,uuid,bigint,uuid) OWNER TO postgres;
ALTER FUNCTION corpora_session_guard.begin_creation(uuid,uuid,bigint,uuid,text,text) OWNER TO postgres;
REVOKE ALL ON FUNCTION corpora_session_guard.creation_status(uuid,uuid,bigint,uuid)
    FROM PUBLIC,anon,authenticated;
REVOKE ALL ON FUNCTION corpora_session_guard.begin_creation(uuid,uuid,bigint,uuid,text,text)
    FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION corpora_session_guard.creation_status(uuid,uuid,bigint,uuid)
    TO service_role;
GRANT EXECUTE ON FUNCTION corpora_session_guard.begin_creation(uuid,uuid,bigint,uuid,text,text)
    TO service_role;

CREATE FUNCTION public.corpora_begin_creation(
    p_actor uuid,p_session uuid,p_token_exp bigint,p_corpus uuid,p_title text,p_organization text
) RETURNS jsonb LANGUAGE sql SECURITY INVOKER SET search_path = '' AS $$
    SELECT corpora_session_guard.begin_creation(
        p_actor,p_session,p_token_exp,p_corpus,p_title,p_organization);
$$;
CREATE FUNCTION public.corpora_get_creation(
    p_actor uuid,p_session uuid,p_token_exp bigint,p_corpus uuid
) RETURNS jsonb LANGUAGE sql SECURITY INVOKER SET search_path = '' AS $$
    SELECT corpora_session_guard.creation_status(p_actor,p_session,p_token_exp,p_corpus);
$$;
CREATE FUNCTION public.corpora_observe_provision(p_operation uuid)
RETURNS jsonb LANGUAGE sql SECURITY INVOKER SET search_path = '' AS $$
    SELECT pg_catalog.jsonb_build_object('operation',row_to_json(o),'metadata',row_to_json(m))
    FROM public.corpora_corpus_provision_ops o
    JOIN public.corpora_corpus_metadata m ON m.id=o.corpus_id
    WHERE o.id=p_operation AND o.state IN ('executing','uncertain')
        AND o.worker_claim IS NOT NULL;
$$;
CREATE FUNCTION public.corpora_finalize_creation(
    p_corpus uuid,p_operation uuid,p_claim uuid,p_repo text,p_type text,p_sha text
) RETURNS boolean LANGUAGE sql SECURITY INVOKER SET search_path = '' AS $$
    SELECT corpora_session_guard.finalize_provision(
        p_corpus,p_operation,p_claim,p_repo,p_type,p_sha);
$$;
CREATE FUNCTION public.corpora_find_creation_result(
    p_corpus uuid,p_operation uuid,p_claim uuid,p_repo text,p_type text,p_sha text
) RETURNS text LANGUAGE sql SECURITY INVOKER SET search_path = '' AS $$
    SELECT b.commit_sha FROM public.corpora_corpus_bootstrap b
    JOIN public.corpora_corpus_provision_ops o ON o.id=b.operation_id
    JOIN public.corpora_corpus_metadata m ON m.id=b.corpus_id
    WHERE b.corpus_id=p_corpus AND b.operation_id=p_operation AND b.worker_claim=p_claim
        AND b.commit_sha=p_sha AND o.corpus_id=b.corpus_id AND o.worker_claim=b.worker_claim
        AND o.repo_id=p_repo AND o.repo_type=p_type AND o.state='completed'
        AND m.hf_repo_id=p_repo AND m.hf_repo_type=p_type;
$$;
CREATE FUNCTION public.corpora_mark_creation_uncertain(p_operation uuid,p_claim uuid)
RETURNS boolean LANGUAGE sql SECURITY INVOKER SET search_path = '' AS $$
    SELECT corpora_session_guard.mark_provision_uncertain(p_operation,p_claim);
$$;

REVOKE ALL ON FUNCTION public.corpora_begin_creation(uuid,uuid,bigint,uuid,text,text)
    FROM PUBLIC,anon,authenticated;
REVOKE ALL ON FUNCTION public.corpora_get_creation(uuid,uuid,bigint,uuid)
    FROM PUBLIC,anon,authenticated;
REVOKE ALL ON FUNCTION public.corpora_observe_provision(uuid) FROM PUBLIC,anon,authenticated;
REVOKE ALL ON FUNCTION public.corpora_finalize_creation(uuid,uuid,uuid,text,text,text)
    FROM PUBLIC,anon,authenticated;
REVOKE ALL ON FUNCTION public.corpora_find_creation_result(uuid,uuid,uuid,text,text,text)
    FROM PUBLIC,anon,authenticated;
REVOKE ALL ON FUNCTION public.corpora_mark_creation_uncertain(uuid,uuid)
    FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION public.corpora_begin_creation(uuid,uuid,bigint,uuid,text,text)
    TO service_role;
GRANT EXECUTE ON FUNCTION public.corpora_get_creation(uuid,uuid,bigint,uuid) TO service_role;
GRANT EXECUTE ON FUNCTION public.corpora_observe_provision(uuid) TO service_role;
GRANT EXECUTE ON FUNCTION public.corpora_finalize_creation(uuid,uuid,uuid,text,text,text)
    TO service_role;
GRANT EXECUTE ON FUNCTION public.corpora_find_creation_result(uuid,uuid,uuid,text,text,text)
    TO service_role;
GRANT EXECUTE ON FUNCTION public.corpora_mark_creation_uncertain(uuid,uuid) TO service_role;
NOTIFY pgrst, 'reload schema';
