-- A corpus owner (or superadmin) may grant read-only access to an individual.
CREATE TABLE public.corpora_corpus_readers (
    corpus_id uuid NOT NULL REFERENCES public.corpora_corpus_metadata(id) ON DELETE RESTRICT,
    user_id uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    added_by uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY(corpus_id,user_id)
);
CREATE INDEX corpora_corpus_readers_user ON public.corpora_corpus_readers(user_id,corpus_id);
ALTER TABLE public.corpora_corpus_readers ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.corpora_corpus_readers FROM PUBLIC,anon,authenticated,service_role;
GRANT SELECT ON public.corpora_corpus_readers TO service_role;

CREATE FUNCTION corpora_session_guard.corpus_reader_set(
    p_actor uuid,p_session uuid,p_token_exp bigint,p_corpus uuid,p_reader uuid,p_present boolean
) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path='' SET lock_timeout='3s' AS $$
DECLARE m public.corpora_corpus_metadata%ROWTYPE;
BEGIN
    PERFORM corpora_session_guard.lock_verified_session(p_actor,p_session,p_token_exp);
    IF p_corpus IS NULL OR p_reader IS NULL OR p_present IS NULL THEN
        RAISE EXCEPTION 'CORPUS_READER_INPUT_INVALID';
    END IF;
    SELECT * INTO m FROM public.corpora_corpus_metadata WHERE id=p_corpus FOR UPDATE;
    IF NOT FOUND OR (m.owner_id IS DISTINCT FROM p_actor AND NOT EXISTS(
        SELECT 1 FROM corpora_session_guard.superadmins WHERE user_id=p_actor))
       OR NOT EXISTS(SELECT 1 FROM public.corpora_corpus_access WHERE corpus_id=m.id AND deleted_at IS NULL)
    THEN RETURN pg_catalog.jsonb_build_object('status','not_found'); END IF;
    IF p_present AND NOT EXISTS(SELECT 1 FROM auth.users WHERE id=p_reader AND deleted_at IS NULL
        AND (email_confirmed_at IS NOT NULL OR phone_confirmed_at IS NOT NULL)) THEN
        RETURN pg_catalog.jsonb_build_object('status','reader_not_found');
    END IF;
    IF p_present THEN
        INSERT INTO public.corpora_corpus_readers(corpus_id,user_id,added_by)
            VALUES(p_corpus,p_reader,p_actor) ON CONFLICT DO NOTHING;
    ELSE
        DELETE FROM public.corpora_corpus_readers WHERE corpus_id=p_corpus AND user_id=p_reader;
    END IF;
    PERFORM corpora_session_guard.lock_verified_session(p_actor,p_session,p_token_exp);
    RETURN pg_catalog.jsonb_build_object('status','ok','corpus_id',p_corpus,
        'user_id',p_reader,'present',p_present);
END;
$$;
CREATE FUNCTION corpora_session_guard.corpus_reader_add_email(
    p_actor uuid,p_session uuid,p_token_exp bigint,p_corpus uuid,p_email text
) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path='' SET lock_timeout='3s' AS $$
DECLARE m public.corpora_corpus_metadata%ROWTYPE; reader_id uuid; matches bigint;
BEGIN
    PERFORM corpora_session_guard.lock_verified_session(p_actor,p_session,p_token_exp);
    IF p_corpus IS NULL OR p_email IS NULL OR length(p_email)>254 OR pg_catalog.btrim(p_email)='' THEN
        RAISE EXCEPTION 'CORPUS_READER_INPUT_INVALID';
    END IF;
    SELECT * INTO m FROM public.corpora_corpus_metadata WHERE id=p_corpus FOR UPDATE;
    IF NOT FOUND OR (m.owner_id IS DISTINCT FROM p_actor AND NOT EXISTS(
        SELECT 1 FROM corpora_session_guard.superadmins WHERE user_id=p_actor))
       OR NOT EXISTS(SELECT 1 FROM public.corpora_corpus_access WHERE corpus_id=m.id AND deleted_at IS NULL)
    THEN RETURN pg_catalog.jsonb_build_object('status','not_found'); END IF;
    SELECT count(*),(pg_catalog.array_agg(id))[1] INTO matches,reader_id FROM auth.users
        WHERE pg_catalog.lower(email)=pg_catalog.lower(pg_catalog.btrim(p_email))
          AND email_confirmed_at IS NOT NULL AND deleted_at IS NULL;
    IF matches<>1 THEN RETURN pg_catalog.jsonb_build_object('status','reader_not_found'); END IF;
    RETURN corpora_session_guard.corpus_reader_set(p_actor,p_session,p_token_exp,p_corpus,reader_id,true);
END;
$$;
CREATE FUNCTION corpora_session_guard.corpus_readers_list(
    p_actor uuid,p_session uuid,p_token_exp bigint,p_corpus uuid,p_after uuid,p_limit integer
) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path='' SET lock_timeout='3s' AS $$
DECLARE m public.corpora_corpus_metadata%ROWTYPE; readers jsonb;
BEGIN
    PERFORM corpora_session_guard.lock_verified_session(p_actor,p_session,p_token_exp);
    IF p_corpus IS NULL OR p_limit IS NULL OR p_limit NOT BETWEEN 1 AND 100 THEN
        RAISE EXCEPTION 'CORPUS_READERS_INPUT_INVALID';
    END IF;
    SELECT * INTO m FROM public.corpora_corpus_metadata WHERE id=p_corpus FOR SHARE;
    IF NOT FOUND OR (m.owner_id IS DISTINCT FROM p_actor AND NOT EXISTS(
        SELECT 1 FROM corpora_session_guard.superadmins WHERE user_id=p_actor))
       OR NOT EXISTS(SELECT 1 FROM public.corpora_corpus_access WHERE corpus_id=m.id AND deleted_at IS NULL)
    THEN RETURN pg_catalog.jsonb_build_object('status','not_found'); END IF;
    SELECT coalesce(pg_catalog.jsonb_agg(pg_catalog.to_jsonb(item) ORDER BY item.user_id),'[]'::jsonb)
        INTO readers FROM (SELECT user_id,created_at FROM public.corpora_corpus_readers
            WHERE corpus_id=p_corpus AND (p_after IS NULL OR user_id>p_after)
            ORDER BY user_id LIMIT p_limit+1) item;
    PERFORM corpora_session_guard.lock_verified_session(p_actor,p_session,p_token_exp);
    RETURN pg_catalog.jsonb_build_object('status','ok','corpus_id',p_corpus,'readers',readers);
END;
$$;
CREATE FUNCTION corpora_session_guard.shared_corpora_list(
    p_actor uuid,p_session uuid,p_token_exp bigint,p_after uuid,p_limit integer
) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path='' SET lock_timeout='3s' AS $$
DECLARE rows jsonb;
BEGIN
    PERFORM corpora_session_guard.lock_verified_session(p_actor,p_session,p_token_exp);
    IF p_limit IS NULL OR p_limit NOT BETWEEN 1 AND 100 THEN RAISE EXCEPTION 'CORPUS_READERS_INPUT_INVALID'; END IF;
    SELECT coalesce(pg_catalog.jsonb_agg(pg_catalog.to_jsonb(item) ORDER BY item.id),'[]'::jsonb)
        INTO rows FROM (SELECT m.id,m.owner_id,m.title,m.language,m.state,m.revision,m.created_at,m.updated_at
            FROM public.corpora_corpus_metadata m JOIN public.corpora_corpus_readers r ON r.corpus_id=m.id
            JOIN public.corpora_corpus_access a ON a.corpus_id=m.id
            WHERE r.user_id=p_actor AND a.deleted_at IS NULL AND m.state='ready'
              AND EXISTS(SELECT 1 FROM public.corpora_corpus_heads h WHERE h.corpus_id=m.id)
              AND (p_after IS NULL OR m.id>p_after)
            ORDER BY m.id LIMIT p_limit+1 FOR SHARE OF m) item;
    PERFORM corpora_session_guard.lock_verified_session(p_actor,p_session,p_token_exp);
    RETURN pg_catalog.jsonb_build_object('status','list','rows',rows);
END;
$$;

CREATE OR REPLACE FUNCTION corpora_session_guard.read_access(
    p_actor uuid,p_session uuid,p_token_exp bigint,p_corpus uuid
) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path='' SET lock_timeout='3s' AS $$
DECLARE m public.corpora_corpus_metadata%ROWTYPE; a public.corpora_corpus_access%ROWTYPE; head jsonb;
BEGIN
    IF p_actor IS NOT NULL OR p_session IS NOT NULL OR p_token_exp IS NOT NULL THEN
        PERFORM corpora_session_guard.lock_verified_session(p_actor,p_session,p_token_exp);
    END IF;
    SELECT * INTO m FROM public.corpora_corpus_metadata WHERE id=p_corpus FOR SHARE;
    SELECT * INTO a FROM public.corpora_corpus_access WHERE corpus_id=m.id AND deleted_at IS NULL;
    IF a.corpus_id IS NULL OR NOT (
        (p_actor IS NOT NULL AND (m.owner_id=p_actor OR EXISTS (
            SELECT 1 FROM corpora_session_guard.superadmins WHERE user_id=p_actor)))
        OR (a.visibility='public' AND m.state='ready' AND EXISTS (
            SELECT 1 FROM public.corpora_corpus_heads WHERE corpus_id=m.id))
        OR (p_actor IS NOT NULL AND m.state='ready' AND EXISTS (
            SELECT 1 FROM public.corpora_corpus_readers r WHERE r.corpus_id=m.id AND r.user_id=p_actor)
            AND EXISTS (SELECT 1 FROM public.corpora_corpus_heads WHERE corpus_id=m.id))
    ) THEN RETURN jsonb_build_object('status','not_found'); END IF;
    SELECT jsonb_build_object('commit_sha',h.commit_sha,'sequence',h.sequence,
        'retained_ref',v.retained_ref,'archive_size',s.archive_size,'archive_sha256',o.input_sha256)
    INTO head FROM public.corpora_corpus_heads h
        JOIN public.corpora_corpus_versions v ON v.corpus_id=h.corpus_id AND v.sequence=h.sequence
            AND v.commit_sha=h.commit_sha
        JOIN public.corpora_corpus_write_ops o ON o.id=v.operation_id AND o.corpus_id=v.corpus_id
            AND o.state='completed' AND o.input_format='corpus'
        JOIN public.corpora_corpus_publication_claims p ON p.operation_id=o.id
            AND p.corpus_id=o.corpus_id AND p.publication_claim=v.publication_claim
            AND p.stage_claim=o.worker_claim
        JOIN public.corpora_corpus_staged_inputs s ON s.operation_id=o.id
            AND s.corpus_id=o.corpus_id AND s.worker_claim=o.worker_claim
            AND s.input_sha256=o.input_sha256 AND s.input_format=o.input_format
            AND s.repo_id=m.hf_repo_id AND s.repo_type=m.hf_repo_type
    WHERE h.corpus_id=m.id;
    IF p_actor IS NOT NULL THEN
        PERFORM corpora_session_guard.lock_verified_session(p_actor,p_session,p_token_exp);
    END IF;
    RETURN jsonb_build_object('status','ok','row',to_jsonb(m),'visibility',a.visibility,'head',head);
END;
$$;

CREATE FUNCTION public.corpora_set_corpus_reader(
    p_actor uuid,p_session uuid,p_token_exp bigint,p_corpus uuid,p_reader uuid,p_present boolean
) RETURNS jsonb LANGUAGE sql SECURITY INVOKER SET search_path='' AS $$
    SELECT corpora_session_guard.corpus_reader_set(p_actor,p_session,p_token_exp,p_corpus,p_reader,p_present);
$$;
CREATE FUNCTION public.corpora_add_corpus_reader_by_email(
    p_actor uuid,p_session uuid,p_token_exp bigint,p_corpus uuid,p_email text
) RETURNS jsonb LANGUAGE sql SECURITY INVOKER SET search_path='' AS $$
    SELECT corpora_session_guard.corpus_reader_add_email(p_actor,p_session,p_token_exp,p_corpus,p_email);
$$;
CREATE FUNCTION public.corpora_list_corpus_readers(
    p_actor uuid,p_session uuid,p_token_exp bigint,p_corpus uuid,p_after uuid,p_limit integer
) RETURNS jsonb LANGUAGE sql SECURITY INVOKER SET search_path='' AS $$
    SELECT corpora_session_guard.corpus_readers_list(p_actor,p_session,p_token_exp,p_corpus,p_after,p_limit);
$$;
CREATE FUNCTION public.corpora_list_shared_corpora(
    p_actor uuid,p_session uuid,p_token_exp bigint,p_after uuid,p_limit integer
) RETURNS jsonb LANGUAGE sql SECURITY INVOKER SET search_path='' AS $$
    SELECT corpora_session_guard.shared_corpora_list(p_actor,p_session,p_token_exp,p_after,p_limit);
$$;
DO $$ DECLARE fn regprocedure; BEGIN
    FOREACH fn IN ARRAY ARRAY[
        'corpora_session_guard.corpus_reader_set(uuid,uuid,bigint,uuid,uuid,boolean)'::regprocedure,
        'corpora_session_guard.corpus_reader_add_email(uuid,uuid,bigint,uuid,text)'::regprocedure,
        'corpora_session_guard.corpus_readers_list(uuid,uuid,bigint,uuid,uuid,integer)'::regprocedure,
        'corpora_session_guard.shared_corpora_list(uuid,uuid,bigint,uuid,integer)'::regprocedure,
        'public.corpora_set_corpus_reader(uuid,uuid,bigint,uuid,uuid,boolean)'::regprocedure,
        'public.corpora_add_corpus_reader_by_email(uuid,uuid,bigint,uuid,text)'::regprocedure,
        'public.corpora_list_corpus_readers(uuid,uuid,bigint,uuid,uuid,integer)'::regprocedure,
        'public.corpora_list_shared_corpora(uuid,uuid,bigint,uuid,integer)'::regprocedure
    ] LOOP
        EXECUTE pg_catalog.format('REVOKE ALL ON FUNCTION %s FROM PUBLIC,anon,authenticated',fn);
        EXECUTE pg_catalog.format('GRANT EXECUTE ON FUNCTION %s TO service_role',fn);
    END LOOP;
END; $$;
NOTIFY pgrst,'reload schema';
