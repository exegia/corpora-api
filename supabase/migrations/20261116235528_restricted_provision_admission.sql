-- Server-controlled identities only. The API cannot promote users by changing
-- user metadata or passing a caller-selected administrator flag.
CREATE TABLE corpora_session_guard.superadmins (
    user_id uuid PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE
);
REVOKE ALL ON corpora_session_guard.superadmins FROM PUBLIC, anon, authenticated, service_role;

-- The caller is the trusted API, which must verify the JWT signature before
-- supplying its subject, session and expiry. This is not a public RPC.
CREATE FUNCTION corpora_session_guard.admit_provision(
    p_actor uuid, p_session uuid, p_token_exp bigint, p_corpus uuid
) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE
    m public.corpora_corpus_metadata%ROWTYPE;
    new_operation uuid := pg_catalog.gen_random_uuid();
    new_claim uuid := pg_catalog.gen_random_uuid();
    inserted uuid;
BEGIN
    PERFORM corpora_session_guard.lock_verified_session(p_actor, p_session, p_token_exp);
    SELECT * INTO m FROM public.corpora_corpus_metadata
        WHERE id = p_corpus AND (
            owner_id = p_actor OR EXISTS (
                SELECT 1 FROM corpora_session_guard.superadmins WHERE user_id = p_actor
            )
        ) FOR UPDATE;
    IF NOT FOUND THEN
        PERFORM corpora_session_guard.lock_verified_session(p_actor, p_session, p_token_exp);
        RETURN pg_catalog.jsonb_build_object('status', 'not_found');
    END IF;
    IF m.state <> 'provisioning'
       OR EXISTS (SELECT 1 FROM public.corpora_corpus_heads WHERE corpus_id = p_corpus)
       OR EXISTS (SELECT 1 FROM public.corpora_corpus_bootstrap WHERE corpus_id = p_corpus) THEN
        PERFORM corpora_session_guard.lock_verified_session(p_actor, p_session, p_token_exp);
        RETURN pg_catalog.jsonb_build_object('status', 'conflict');
    END IF;

    INSERT INTO public.corpora_corpus_provision_ops
        (id, corpus_id, actor_id, session_id, repo_id, repo_type)
        VALUES (new_operation, m.id, p_actor, p_session, m.hf_repo_id, m.hf_repo_type)
        ON CONFLICT DO NOTHING RETURNING id INTO inserted;
    IF inserted IS NULL THEN
        PERFORM corpora_session_guard.lock_verified_session(p_actor, p_session, p_token_exp);
        RETURN pg_catalog.jsonb_build_object('status', 'conflict');
    END IF;
    UPDATE public.corpora_corpus_provision_ops
        SET state = 'executing', worker_claim = new_claim
        WHERE id = new_operation AND state = 'admitted'
        RETURNING id INTO inserted;
    IF inserted IS NULL THEN
        RAISE EXCEPTION 'CORPUS_PROVISION_CLAIM_INVALID';
    END IF;
    PERFORM corpora_session_guard.lock_verified_session(p_actor, p_session, p_token_exp);
    RETURN pg_catalog.jsonb_build_object(
        'status', 'ok', 'operation_id', new_operation, 'claim_id', new_claim,
        'repo_id', m.hf_repo_id, 'repo_type', m.hf_repo_type, 'revision', m.revision
    );
END;
$$;
ALTER FUNCTION corpora_session_guard.admit_provision(uuid, uuid, bigint, uuid) OWNER TO postgres;
REVOKE ALL ON FUNCTION corpora_session_guard.admit_provision(uuid, uuid, bigint, uuid)
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION corpora_session_guard.admit_provision(uuid, uuid, bigint, uuid)
    TO service_role;
