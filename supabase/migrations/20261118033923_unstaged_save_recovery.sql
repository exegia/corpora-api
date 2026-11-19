CREATE OR REPLACE FUNCTION corpora_session_guard.reconciliation_target(
    p_actor uuid,p_session uuid,p_token_exp bigint,p_corpus uuid,p_operation uuid
) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path='' SET lock_timeout='3s' AS $$
DECLARE m public.corpora_corpus_metadata%ROWTYPE; o public.corpora_corpus_write_ops%ROWTYPE; target jsonb;
BEGIN
    PERFORM corpora_session_guard.lock_verified_session(p_actor,p_session,p_token_exp);
    SELECT * INTO m FROM public.corpora_corpus_metadata WHERE id=p_corpus AND (
        owner_id=p_actor OR EXISTS(SELECT 1 FROM corpora_session_guard.superadmins WHERE user_id=p_actor)
    ) FOR UPDATE;
    IF NOT FOUND OR NOT EXISTS(SELECT 1 FROM public.corpora_corpus_access
        WHERE corpus_id=p_corpus AND deleted_at IS NULL) THEN
        RETURN pg_catalog.jsonb_build_object('status','not_found');
    END IF;
    SELECT * INTO o FROM public.corpora_corpus_write_ops
        WHERE id=p_operation AND corpus_id=m.id AND expected_sequence>0 FOR UPDATE;
    IF NOT FOUND OR NOT EXISTS(SELECT 1 FROM public.corpora_corpus_version_intents
        WHERE operation_id=p_operation AND corpus_id=m.id) THEN
        RETURN pg_catalog.jsonb_build_object('status','not_found');
    END IF;
    IF o.state IN ('completed','failed') THEN
        RETURN corpora_session_guard.version_save_status(p_actor,p_session,p_token_exp,p_corpus,p_operation);
    END IF;
    IF o.state<>'uncertain' THEN
        RETURN pg_catalog.jsonb_build_object('status','unresolved');
    END IF;
    -- An absent staging receipt and publication claim are authoritative database
    -- facts, not a guess based on a missing Hub ref. Both are created atomically
    -- under the same metadata and operation locks. Once failed, a late worker
    -- cannot register a stage or publish a version from this operation.
    IF m.state='ready' AND EXISTS(SELECT 1 FROM public.corpora_corpus_heads h
        WHERE h.corpus_id=m.id AND h.sequence=o.expected_sequence
            AND h.commit_sha=o.expected_parent_sha)
       AND NOT EXISTS(SELECT 1 FROM public.corpora_corpus_staged_inputs WHERE operation_id=o.id)
       AND NOT EXISTS(SELECT 1 FROM public.corpora_corpus_publication_claims WHERE operation_id=o.id)
       AND NOT EXISTS(SELECT 1 FROM public.corpora_corpus_versions WHERE operation_id=o.id) THEN
        UPDATE public.corpora_corpus_write_ops SET state='failed' WHERE id=o.id;
        RETURN corpora_session_guard.version_save_status(p_actor,p_session,p_token_exp,p_corpus,p_operation);
    END IF;
    SELECT pg_catalog.jsonb_build_object(
        'status','ready','corpus_id',m.id,'operation_id',o.id,'claim_id',o.worker_claim,
        'publication_claim',p.publication_claim,'repo_id',m.hf_repo_id,'repo_type',m.hf_repo_type,
        'expected_sequence',o.expected_sequence,'expected_parent_sha',o.expected_parent_sha,
        'input_sha256',o.input_sha256,'archive_size',s.archive_size,
        'stage_sha',s.stage_sha,'stage_path',s.stage_path
    ) INTO target FROM public.corpora_corpus_heads h
        JOIN public.corpora_corpus_staged_inputs s ON s.operation_id=o.id AND s.corpus_id=o.corpus_id
            AND s.worker_claim=o.worker_claim AND s.repo_id=m.hf_repo_id AND s.repo_type=m.hf_repo_type
            AND s.input_sha256=o.input_sha256 AND s.input_format='corpus'
            AND s.requested_base_sha=o.expected_parent_sha
        JOIN public.corpora_corpus_publication_claims p ON p.operation_id=o.id
            AND p.corpus_id=o.corpus_id AND p.stage_claim=o.worker_claim
        WHERE h.corpus_id=m.id AND h.sequence=o.expected_sequence
            AND h.commit_sha=o.expected_parent_sha AND m.state='ready';
    PERFORM corpora_session_guard.lock_verified_session(p_actor,p_session,p_token_exp);
    RETURN coalesce(target,pg_catalog.jsonb_build_object('status','unresolved'));
END;
$$;

NOTIFY pgrst, 'reload schema';
