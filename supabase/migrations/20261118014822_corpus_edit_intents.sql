-- Bind retry identity to the requested edit, not nondeterministic cache bytes.
ALTER TABLE public.corpora_corpus_version_saves
    ADD CONSTRAINT corpora_corpus_version_saves_identity UNIQUE (operation_id,corpus_id);
CREATE TABLE public.corpora_corpus_version_intents (
    operation_id uuid PRIMARY KEY,
    corpus_id uuid NOT NULL,
    intent_sha256 text NOT NULL CHECK (intent_sha256 ~ '^[0-9a-f]{64}$'),
    kind text NOT NULL CHECK (kind IN ('manifest','node','restore')),
    restore_sequence bigint,
    CHECK ((kind='restore') = (restore_sequence IS NOT NULL)),
    FOREIGN KEY (operation_id,corpus_id)
        REFERENCES public.corpora_corpus_version_saves(operation_id,corpus_id) ON DELETE RESTRICT,
    FOREIGN KEY (corpus_id,restore_sequence)
        REFERENCES public.corpora_corpus_versions(corpus_id,sequence) ON DELETE RESTRICT
);
ALTER TABLE public.corpora_corpus_version_intents ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.corpora_corpus_version_intents FROM PUBLIC,anon,authenticated,service_role;
GRANT SELECT ON public.corpora_corpus_version_intents TO service_role;
CREATE TRIGGER corpora_corpus_version_intents_immutable BEFORE UPDATE OR DELETE
    ON public.corpora_corpus_version_intents FOR EACH ROW
    EXECUTE FUNCTION public.corpora_corpus_preserve_history();

CREATE FUNCTION corpora_session_guard.edit_intent_status(
    p_actor uuid,p_session uuid,p_token_exp bigint,p_corpus uuid,p_operation uuid,p_intent text
) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path='' SET lock_timeout='3s' AS $$
DECLARE m public.corpora_corpus_metadata%ROWTYPE; o public.corpora_corpus_write_ops%ROWTYPE;
        i public.corpora_corpus_version_intents%ROWTYPE;
BEGIN
    PERFORM corpora_session_guard.lock_verified_session(p_actor,p_session,p_token_exp);
    IF p_corpus IS NULL OR p_operation IS NULL OR p_intent IS NULL
        OR p_intent !~ '^[0-9a-f]{64}$' THEN RAISE EXCEPTION 'CORPUS_EDIT_INPUT_INVALID'; END IF;
    SELECT * INTO m FROM public.corpora_corpus_metadata WHERE id=p_corpus AND (
        owner_id=p_actor OR EXISTS(SELECT 1 FROM corpora_session_guard.superadmins WHERE user_id=p_actor)
    ) FOR UPDATE;
    IF NOT FOUND OR NOT EXISTS(SELECT 1 FROM public.corpora_corpus_access
        WHERE corpus_id=p_corpus AND deleted_at IS NULL) THEN
        RETURN pg_catalog.jsonb_build_object('status','not_found');
    END IF;
    SELECT * INTO o FROM public.corpora_corpus_write_ops WHERE id=p_operation;
    IF NOT FOUND THEN RETURN pg_catalog.jsonb_build_object('status','absent'); END IF;
    SELECT * INTO i FROM public.corpora_corpus_version_intents WHERE operation_id=p_operation;
    IF NOT FOUND OR o.corpus_id<>m.id OR o.actor_id<>p_actor OR i.corpus_id<>m.id
       OR i.intent_sha256<>p_intent THEN
        RETURN pg_catalog.jsonb_build_object('status','conflict');
    END IF;
    RETURN corpora_session_guard.version_save_status(p_actor,p_session,p_token_exp,p_corpus,p_operation);
END;
$$;

CREATE FUNCTION corpora_session_guard.begin_corpus_edit(
    p_actor uuid,p_session uuid,p_token_exp bigint,p_corpus uuid,p_operation uuid,
    p_sequence bigint,p_parent text,p_digest text,p_size bigint,p_intent text,p_kind text,p_restore bigint
) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path='' SET lock_timeout='3s' AS $$
DECLARE status jsonb; result jsonb;
BEGIN
    IF p_kind IS NULL OR p_kind NOT IN ('manifest','node','restore')
       OR ((p_kind='restore') IS DISTINCT FROM (p_restore IS NOT NULL))
       OR (p_restore IS NOT NULL AND p_restore<1) THEN
        RAISE EXCEPTION 'CORPUS_EDIT_INPUT_INVALID';
    END IF;
    -- Locks the authorized metadata row before inspecting or claiming an intent.
    status := corpora_session_guard.edit_intent_status(
        p_actor,p_session,p_token_exp,p_corpus,p_operation,p_intent);
    IF status->>'status'<>'absent' THEN RETURN status; END IF;
    IF p_kind='restore' AND NOT EXISTS(SELECT 1 FROM public.corpora_corpus_versions
        WHERE corpus_id=p_corpus AND sequence=p_restore) THEN
        RETURN pg_catalog.jsonb_build_object('status','not_found');
    END IF;
    result := corpora_session_guard.begin_version_save(
        p_actor,p_session,p_token_exp,p_corpus,p_operation,p_sequence,p_parent,p_digest,p_size);
    IF result->>'status'='claimed' THEN
        INSERT INTO public.corpora_corpus_version_intents
            (operation_id,corpus_id,intent_sha256,kind,restore_sequence)
            VALUES(p_operation,p_corpus,p_intent,p_kind,p_restore);
    END IF;
    RETURN result;
END;
$$;

CREATE FUNCTION corpora_session_guard.restore_source(
    p_actor uuid,p_session uuid,p_token_exp bigint,p_corpus uuid,p_sequence bigint
) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path='' SET lock_timeout='3s' AS $$
DECLARE m public.corpora_corpus_metadata%ROWTYPE; result jsonb;
BEGIN
    PERFORM corpora_session_guard.lock_verified_session(p_actor,p_session,p_token_exp);
    SELECT * INTO m FROM public.corpora_corpus_metadata WHERE id=p_corpus AND (
        owner_id=p_actor OR EXISTS(SELECT 1 FROM corpora_session_guard.superadmins WHERE user_id=p_actor)
    ) FOR SHARE;
    IF NOT FOUND OR NOT EXISTS(SELECT 1 FROM public.corpora_corpus_access
        WHERE corpus_id=p_corpus AND deleted_at IS NULL) THEN
        RETURN pg_catalog.jsonb_build_object('status','not_found');
    END IF;
    SELECT pg_catalog.jsonb_build_object(
        'status','ok','commit_sha',v.commit_sha,'sequence',v.sequence,'retained_ref',v.retained_ref,
        'archive_size',s.archive_size,'archive_sha256',o.input_sha256
    ) INTO result FROM public.corpora_corpus_versions v
        JOIN public.corpora_corpus_write_ops o ON o.id=v.operation_id AND o.corpus_id=v.corpus_id
            AND o.state='completed' AND o.input_format='corpus'
        JOIN public.corpora_corpus_publication_claims p ON p.operation_id=o.id
            AND p.corpus_id=o.corpus_id AND p.publication_claim=v.publication_claim
            AND p.stage_claim=o.worker_claim
        JOIN public.corpora_corpus_staged_inputs s ON s.operation_id=o.id
            AND s.corpus_id=o.corpus_id AND s.worker_claim=o.worker_claim
            AND s.input_sha256=o.input_sha256 AND s.input_format=o.input_format
            AND s.repo_id=m.hf_repo_id AND s.repo_type=m.hf_repo_type
        WHERE v.corpus_id=m.id AND v.sequence=p_sequence;
    PERFORM corpora_session_guard.lock_verified_session(p_actor,p_session,p_token_exp);
    RETURN coalesce(result,pg_catalog.jsonb_build_object('status','not_found'));
END;
$$;

CREATE FUNCTION public.corpora_get_restore_source(
    p_actor uuid,p_session uuid,p_token_exp bigint,p_corpus uuid,p_sequence bigint
) RETURNS jsonb LANGUAGE sql SECURITY INVOKER SET search_path='' AS $$
    SELECT corpora_session_guard.restore_source(p_actor,p_session,p_token_exp,p_corpus,p_sequence);
$$;
CREATE FUNCTION public.corpora_get_edit_intent(
    p_actor uuid,p_session uuid,p_token_exp bigint,p_corpus uuid,p_operation uuid,p_intent text
) RETURNS jsonb LANGUAGE sql SECURITY INVOKER SET search_path='' AS $$
    SELECT corpora_session_guard.edit_intent_status(
        p_actor,p_session,p_token_exp,p_corpus,p_operation,p_intent);
$$;
CREATE FUNCTION public.corpora_begin_corpus_edit(
    p_actor uuid,p_session uuid,p_token_exp bigint,p_corpus uuid,p_operation uuid,
    p_sequence bigint,p_parent text,p_digest text,p_size bigint,p_intent text,p_kind text,p_restore bigint
) RETURNS jsonb LANGUAGE sql SECURITY INVOKER SET search_path='' AS $$
    SELECT corpora_session_guard.begin_corpus_edit(p_actor,p_session,p_token_exp,p_corpus,p_operation,
        p_sequence,p_parent,p_digest,p_size,p_intent,p_kind,p_restore);
$$;
DO $$ DECLARE fn regprocedure; BEGIN
    FOREACH fn IN ARRAY ARRAY[
        'corpora_session_guard.restore_source(uuid,uuid,bigint,uuid,bigint)'::regprocedure,
        'public.corpora_get_restore_source(uuid,uuid,bigint,uuid,bigint)'::regprocedure,
        'corpora_session_guard.edit_intent_status(uuid,uuid,bigint,uuid,uuid,text)'::regprocedure,
        'corpora_session_guard.begin_corpus_edit(uuid,uuid,bigint,uuid,uuid,bigint,text,text,bigint,text,text,bigint)'::regprocedure,
        'public.corpora_get_edit_intent(uuid,uuid,bigint,uuid,uuid,text)'::regprocedure,
        'public.corpora_begin_corpus_edit(uuid,uuid,bigint,uuid,uuid,bigint,text,text,bigint,text,text,bigint)'::regprocedure
    ] LOOP
        EXECUTE pg_catalog.format('REVOKE ALL ON FUNCTION %s FROM PUBLIC,anon,authenticated',fn);
        EXECUTE pg_catalog.format('GRANT EXECUTE ON FUNCTION %s TO service_role',fn);
    END LOOP;
END $$;
NOTIFY pgrst, 'reload schema';
