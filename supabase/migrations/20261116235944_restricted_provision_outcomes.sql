-- Internal outcome operations accept only claims already held by the trusted
-- dispatcher. They do not verify Hub provenance or authorize any new write.
CREATE FUNCTION corpora_session_guard.mark_provision_uncertain(p_operation uuid, p_claim uuid)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE changed uuid;
BEGIN
    UPDATE public.corpora_corpus_provision_ops SET state = 'uncertain'
        WHERE id = p_operation AND worker_claim = p_claim AND state IN ('executing', 'uncertain')
        RETURNING id INTO changed;
    RETURN changed IS NOT NULL;
END;
$$;
ALTER FUNCTION corpora_session_guard.mark_provision_uncertain(uuid, uuid) OWNER TO postgres;
REVOKE ALL ON FUNCTION corpora_session_guard.mark_provision_uncertain(uuid, uuid)
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION corpora_session_guard.mark_provision_uncertain(uuid, uuid)
    TO service_role;

CREATE FUNCTION corpora_session_guard.finalize_provision(
    p_corpus uuid, p_operation uuid, p_claim uuid, p_repo text, p_type text, p_sha text
) RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE
    m public.corpora_corpus_metadata%ROWTYPE;
    o public.corpora_corpus_provision_ops%ROWTYPE;
BEGIN
    IF p_corpus IS NULL OR p_operation IS NULL OR p_claim IS NULL
       OR p_repo IS NULL OR p_type IS NULL OR p_type NOT IN ('dataset', 'model')
       OR p_sha IS NULL OR p_sha !~ '^[0-9a-f]{40}$' THEN
        RETURN false;
    END IF;
    SELECT * INTO m FROM public.corpora_corpus_metadata WHERE id = p_corpus FOR UPDATE;
    IF NOT FOUND OR m.hf_repo_id <> p_repo OR m.hf_repo_type <> p_type THEN
        RETURN false;
    END IF;
    SELECT * INTO o FROM public.corpora_corpus_provision_ops
        WHERE id = p_operation FOR UPDATE;
    IF NOT FOUND OR o.corpus_id <> p_corpus OR o.worker_claim IS DISTINCT FROM p_claim
       OR o.repo_id <> p_repo OR o.repo_type <> p_type THEN
        RETURN false;
    END IF;
    IF o.state = 'completed' THEN
        RETURN EXISTS (
            SELECT 1 FROM public.corpora_corpus_bootstrap
            WHERE corpus_id = p_corpus AND operation_id = p_operation
              AND worker_claim = p_claim AND commit_sha = p_sha
        );
    END IF;
    IF o.state <> 'executing' OR m.state <> 'provisioning'
       OR EXISTS (SELECT 1 FROM public.corpora_corpus_heads WHERE corpus_id = p_corpus) THEN
        RETURN false;
    END IF;
    INSERT INTO public.corpora_corpus_bootstrap (corpus_id, operation_id, worker_claim, commit_sha)
        VALUES (p_corpus, p_operation, p_claim, p_sha);
    UPDATE public.corpora_corpus_provision_ops SET state = 'completed'
        WHERE id = p_operation AND worker_claim = p_claim AND state = 'executing';
    IF NOT FOUND THEN
        RAISE EXCEPTION 'CORPUS_PROVISION_CLAIM_INVALID';
    END IF;
    -- Deferred SECURITY INVOKER triggers otherwise run as the unprivileged
    -- caller after this function returns. Validate while still the definer.
    SET CONSTRAINTS public.corpora_corpus_bootstrap_finalized IMMEDIATE;
    RETURN true;
END;
$$;
ALTER FUNCTION corpora_session_guard.finalize_provision(uuid, uuid, uuid, text, text, text)
    OWNER TO postgres;
REVOKE ALL ON FUNCTION corpora_session_guard.finalize_provision(uuid, uuid, uuid, text, text, text)
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION corpora_session_guard.finalize_provision(uuid, uuid, uuid, text, text, text)
    TO service_role;
