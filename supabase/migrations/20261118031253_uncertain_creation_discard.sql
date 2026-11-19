-- A superadmin may explicitly discard an uncertain creation after the API
-- has verified the exact private Hub repository and its empty bootstrap tree.
CREATE TABLE public.corpora_corpus_uncertain_discards (
    deletion_id uuid PRIMARY KEY REFERENCES public.corpora_corpus_deletions(id) ON DELETE RESTRICT,
    corpus_id uuid NOT NULL REFERENCES public.corpora_corpus_metadata(id) ON DELETE RESTRICT,
    provision_id uuid NOT NULL REFERENCES public.corpora_corpus_provision_ops(id) ON DELETE RESTRICT,
    observed_sha text NOT NULL CHECK(observed_sha ~ '^[0-9a-f]{40}$')
);
ALTER TABLE public.corpora_corpus_uncertain_discards ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.corpora_corpus_uncertain_discards FROM PUBLIC,anon,authenticated,service_role;
GRANT SELECT ON public.corpora_corpus_uncertain_discards TO service_role;
CREATE TRIGGER corpora_corpus_uncertain_discards_immutable BEFORE UPDATE OR DELETE
    ON public.corpora_corpus_uncertain_discards FOR EACH ROW
    EXECUTE FUNCTION public.corpora_corpus_preserve_history();

CREATE FUNCTION corpora_session_guard.begin_uncertain_discard(
    p_actor uuid,p_session uuid,p_token_exp bigint,p_corpus uuid,p_operation uuid,
    p_revision bigint,p_observed_sha text
) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path='' SET lock_timeout='3s' AS $$
DECLARE m public.corpora_corpus_metadata%ROWTYPE; d public.corpora_corpus_deletions%ROWTYPE;
        provision public.corpora_corpus_provision_ops%ROWTYPE; claim uuid:=pg_catalog.gen_random_uuid();
BEGIN
    PERFORM corpora_session_guard.lock_verified_session(p_actor,p_session,p_token_exp);
    IF p_corpus IS NULL OR p_operation IS NULL OR p_revision IS NULL OR p_revision<1
       OR p_observed_sha IS NULL OR p_observed_sha !~ '^[0-9a-f]{40}$' THEN
        RAISE EXCEPTION 'CORPUS_DISCARD_INPUT_INVALID';
    END IF;
    IF NOT EXISTS(SELECT 1 FROM corpora_session_guard.superadmins WHERE user_id=p_actor) THEN
        RETURN pg_catalog.jsonb_build_object('status','not_found');
    END IF;
    SELECT * INTO m FROM public.corpora_corpus_metadata WHERE id=p_corpus FOR UPDATE;
    IF NOT FOUND THEN RETURN pg_catalog.jsonb_build_object('status','not_found'); END IF;
    SELECT * INTO d FROM public.corpora_corpus_deletions WHERE id=p_operation;
    IF FOUND THEN
        IF d.corpus_id<>m.id OR d.actor_id<>p_actor OR d.expected_revision<>p_revision
           OR NOT EXISTS(SELECT 1 FROM public.corpora_corpus_uncertain_discards
                WHERE deletion_id=d.id AND corpus_id=m.id AND observed_sha=p_observed_sha) THEN
            RETURN pg_catalog.jsonb_build_object('status','conflict');
        END IF;
        RETURN corpora_session_guard.deletion_status(p_actor,p_session,p_token_exp,p_corpus,p_operation);
    END IF;
    SELECT * INTO provision FROM public.corpora_corpus_provision_ops
        WHERE corpus_id=m.id AND state='uncertain' FOR UPDATE;
    IF NOT FOUND OR provision.worker_claim IS NULL OR provision.claimed_at IS NULL
       OR provision.claimed_at > pg_catalog.clock_timestamp() - interval '60 seconds'
       OR m.state<>'provisioning' OR m.revision<>p_revision
       OR provision.repo_id<>m.hf_repo_id OR provision.repo_type<>m.hf_repo_type
       OR NOT EXISTS(SELECT 1 FROM public.corpora_corpus_access WHERE corpus_id=m.id AND deleted_at IS NULL)
       OR EXISTS(SELECT 1 FROM public.corpora_corpus_bootstrap WHERE corpus_id=m.id)
       OR EXISTS(SELECT 1 FROM public.corpora_corpus_heads WHERE corpus_id=m.id)
       OR EXISTS(SELECT 1 FROM public.corpora_corpus_write_ops WHERE corpus_id=m.id) THEN
        RETURN pg_catalog.jsonb_build_object('status','conflict');
    END IF;
    INSERT INTO public.corpora_corpus_deletions(id,corpus_id,actor_id,expected_revision,worker_claim)
        VALUES(p_operation,m.id,p_actor,p_revision,claim) ON CONFLICT DO NOTHING;
    IF NOT FOUND THEN RETURN pg_catalog.jsonb_build_object('status','conflict'); END IF;
    INSERT INTO public.corpora_corpus_uncertain_discards(deletion_id,corpus_id,provision_id,observed_sha)
        VALUES(p_operation,m.id,provision.id,p_observed_sha);
    UPDATE public.corpora_corpus_metadata SET title=title WHERE id=m.id;
    UPDATE public.corpora_corpus_access SET visibility='private',deleted_at=clock_timestamp() WHERE corpus_id=m.id;
    PERFORM corpora_session_guard.lock_verified_session(p_actor,p_session,p_token_exp);
    RETURN pg_catalog.jsonb_build_object('status','claimed','corpus_id',m.id,'operation_id',p_operation,
        'claim_id',claim,'repo_id',m.hf_repo_id,'repo_type',m.hf_repo_type,'observed_sha',p_observed_sha);
END;
$$;
CREATE FUNCTION public.corpora_begin_uncertain_discard(
    p_actor uuid,p_session uuid,p_token_exp bigint,p_corpus uuid,p_operation uuid,
    p_revision bigint,p_observed_sha text
) RETURNS jsonb LANGUAGE sql SECURITY INVOKER SET search_path='' AS $$
    SELECT corpora_session_guard.begin_uncertain_discard(
        p_actor,p_session,p_token_exp,p_corpus,p_operation,p_revision,p_observed_sha);
$$;
REVOKE ALL ON FUNCTION corpora_session_guard.begin_uncertain_discard(uuid,uuid,bigint,uuid,uuid,bigint,text)
    FROM PUBLIC,anon,authenticated;
REVOKE ALL ON FUNCTION public.corpora_begin_uncertain_discard(uuid,uuid,bigint,uuid,uuid,bigint,text)
    FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION corpora_session_guard.begin_uncertain_discard(uuid,uuid,bigint,uuid,uuid,bigint,text)
    TO service_role;
GRANT EXECUTE ON FUNCTION public.corpora_begin_uncertain_discard(uuid,uuid,bigint,uuid,uuid,bigint,text)
    TO service_role;
CREATE FUNCTION corpora_session_guard.uncertain_discard_target(
    p_actor uuid,p_session uuid,p_token_exp bigint,p_corpus uuid,p_revision bigint
) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path='' SET lock_timeout='3s' AS $$
DECLARE m public.corpora_corpus_metadata%ROWTYPE; provision public.corpora_corpus_provision_ops%ROWTYPE;
BEGIN
    PERFORM corpora_session_guard.lock_verified_session(p_actor,p_session,p_token_exp);
    IF NOT EXISTS(SELECT 1 FROM corpora_session_guard.superadmins WHERE user_id=p_actor) THEN
        RETURN pg_catalog.jsonb_build_object('status','not_found');
    END IF;
    SELECT * INTO m FROM public.corpora_corpus_metadata WHERE id=p_corpus FOR UPDATE;
    IF NOT FOUND THEN RETURN pg_catalog.jsonb_build_object('status','not_found'); END IF;
    SELECT * INTO provision FROM public.corpora_corpus_provision_ops
        WHERE corpus_id=m.id AND state='uncertain' FOR UPDATE;
    IF NOT FOUND OR provision.worker_claim IS NULL OR provision.claimed_at IS NULL
       OR provision.claimed_at > pg_catalog.clock_timestamp() - interval '60 seconds'
       OR m.state<>'provisioning' OR m.revision<>p_revision
       OR provision.repo_id<>m.hf_repo_id OR provision.repo_type<>m.hf_repo_type
       OR NOT EXISTS(SELECT 1 FROM public.corpora_corpus_access WHERE corpus_id=m.id AND deleted_at IS NULL)
       OR EXISTS(SELECT 1 FROM public.corpora_corpus_bootstrap WHERE corpus_id=m.id)
       OR EXISTS(SELECT 1 FROM public.corpora_corpus_heads WHERE corpus_id=m.id)
       OR EXISTS(SELECT 1 FROM public.corpora_corpus_write_ops WHERE corpus_id=m.id) THEN
        RETURN pg_catalog.jsonb_build_object('status','conflict');
    END IF;
    PERFORM corpora_session_guard.lock_verified_session(p_actor,p_session,p_token_exp);
    RETURN pg_catalog.jsonb_build_object('status','ok','corpus_id',m.id,
        'repo_id',m.hf_repo_id,'repo_type',m.hf_repo_type);
END;
$$;
CREATE FUNCTION public.corpora_uncertain_discard_target(
    p_actor uuid,p_session uuid,p_token_exp bigint,p_corpus uuid,p_revision bigint
) RETURNS jsonb LANGUAGE sql SECURITY INVOKER SET search_path='' AS $$
    SELECT corpora_session_guard.uncertain_discard_target(p_actor,p_session,p_token_exp,p_corpus,p_revision);
$$;
REVOKE ALL ON FUNCTION corpora_session_guard.uncertain_discard_target(uuid,uuid,bigint,uuid,bigint) FROM PUBLIC,anon,authenticated;
REVOKE ALL ON FUNCTION public.corpora_uncertain_discard_target(uuid,uuid,bigint,uuid,bigint) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION corpora_session_guard.uncertain_discard_target(uuid,uuid,bigint,uuid,bigint) TO service_role;
GRANT EXECUTE ON FUNCTION public.corpora_uncertain_discard_target(uuid,uuid,bigint,uuid,bigint) TO service_role;
CREATE FUNCTION public.corpora_uncertain_discard_status(
    p_actor uuid,p_session uuid,p_token_exp bigint,p_corpus uuid,p_operation uuid,p_revision bigint
) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path='' SET lock_timeout='3s' AS $$
DECLARE d public.corpora_corpus_deletions%ROWTYPE;
BEGIN
    PERFORM corpora_session_guard.lock_verified_session(p_actor,p_session,p_token_exp);
    IF NOT EXISTS(SELECT 1 FROM corpora_session_guard.superadmins WHERE user_id=p_actor) THEN
        RETURN pg_catalog.jsonb_build_object('status','not_found');
    END IF;
    SELECT * INTO d FROM public.corpora_corpus_deletions WHERE id=p_operation;
    IF NOT FOUND THEN RETURN pg_catalog.jsonb_build_object('status','absent'); END IF;
    IF d.corpus_id<>p_corpus OR d.actor_id<>p_actor OR d.expected_revision<>p_revision
       OR NOT EXISTS(SELECT 1 FROM public.corpora_corpus_uncertain_discards WHERE deletion_id=d.id) THEN
        RETURN pg_catalog.jsonb_build_object('status','conflict');
    END IF;
    RETURN corpora_session_guard.deletion_status(p_actor,p_session,p_token_exp,p_corpus,p_operation);
END;
$$;
REVOKE ALL ON FUNCTION public.corpora_uncertain_discard_status(uuid,uuid,bigint,uuid,uuid,bigint) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION public.corpora_uncertain_discard_status(uuid,uuid,bigint,uuid,uuid,bigint) TO service_role;
NOTIFY pgrst,'reload schema';
