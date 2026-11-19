-- A verified late result may complete the original one-shot import claim.
CREATE OR REPLACE FUNCTION corpora_session_guard.complete_import(
    p_operation uuid,p_claim uuid,p_publication uuid,p_sha text
) RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path = ''
SET lock_timeout = '3s' AS $$
DECLARE
    m public.corpora_corpus_metadata%ROWTYPE;
    o public.corpora_corpus_write_ops%ROWTYPE;
    i public.corpora_corpus_imports%ROWTYPE;
BEGIN
    SELECT * INTO i FROM public.corpora_corpus_imports WHERE operation_id=p_operation;
    IF NOT FOUND THEN RETURN false; END IF;
    SELECT * INTO m FROM public.corpora_corpus_metadata WHERE id=i.corpus_id FOR UPDATE;
    SELECT * INTO o FROM public.corpora_corpus_write_ops WHERE id=p_operation FOR UPDATE;
    IF o.corpus_id<>m.id OR o.worker_claim IS DISTINCT FROM p_claim
       OR p_sha IS NULL OR p_sha !~ '^[0-9a-f]{40}$' OR o.expected_sequence<>0
       OR NOT EXISTS (SELECT 1 FROM public.corpora_corpus_publication_claims
           WHERE operation_id=o.id AND corpus_id=m.id AND stage_claim=p_claim
               AND publication_claim=p_publication) THEN RETURN false; END IF;
    IF o.state='completed' THEN
        RETURN EXISTS (SELECT 1 FROM public.corpora_corpus_versions
            WHERE operation_id=o.id AND corpus_id=m.id AND publication_claim=p_publication
                AND commit_sha=p_sha AND sequence=1);
    END IF;
    IF o.state NOT IN ('executing','uncertain') OR m.state<>'provisioning'
       OR EXISTS(SELECT 1 FROM public.corpora_corpus_heads WHERE corpus_id=m.id)
       OR NOT EXISTS (SELECT 1 FROM public.corpora_corpus_staged_inputs s
            JOIN public.corpora_corpus_bootstrap b ON b.corpus_id=s.corpus_id
            WHERE s.operation_id=o.id AND s.corpus_id=m.id AND s.worker_claim=p_claim
                AND s.requested_base_sha=b.commit_sha AND s.stage_sha<>p_sha
                AND b.commit_sha<>p_sha AND s.input_sha256=o.input_sha256
                AND s.archive_size=i.archive_size AND s.repo_id=m.hf_repo_id
                AND s.repo_type=m.hf_repo_type) THEN RETURN false; END IF;
    INSERT INTO public.corpora_corpus_versions
        (corpus_id,sequence,commit_sha,parent_sha,operation_id,publication_claim,retained_ref)
        VALUES(m.id,1,p_sha,NULL,o.id,p_publication,'corpora-op-'||o.id::text);
    INSERT INTO public.corpora_corpus_heads VALUES(m.id,1,p_sha);
    UPDATE public.corpora_corpus_metadata SET state='ready' WHERE id=m.id;
    UPDATE public.corpora_corpus_write_ops SET state='completed' WHERE id=o.id;
    RETURN true;
END;
$$;

-- Return immutable receipts only after rechecking the caller and corpus owner.
CREATE FUNCTION corpora_session_guard.import_reconciliation_target(
    p_actor uuid,p_session uuid,p_token_exp bigint,p_corpus uuid,p_operation uuid
) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path='' SET lock_timeout='3s' AS $$
DECLARE m public.corpora_corpus_metadata%ROWTYPE; o public.corpora_corpus_write_ops%ROWTYPE;
    target jsonb; baseline text;
BEGIN
    PERFORM corpora_session_guard.lock_verified_session(p_actor,p_session,p_token_exp);
    SELECT * INTO m FROM public.corpora_corpus_metadata WHERE id=p_corpus AND (
        owner_id=p_actor OR EXISTS(SELECT 1 FROM corpora_session_guard.superadmins WHERE user_id=p_actor)
    ) FOR UPDATE;
    IF NOT FOUND OR NOT EXISTS(SELECT 1 FROM public.corpora_corpus_access
        WHERE corpus_id=p_corpus AND deleted_at IS NULL) THEN
        RETURN pg_catalog.jsonb_build_object('status','not_found');
    END IF;
    SELECT * INTO o FROM public.corpora_corpus_write_ops WHERE id=p_operation AND corpus_id=m.id
        AND expected_sequence=0 FOR UPDATE;
    IF NOT FOUND OR NOT EXISTS(SELECT 1 FROM public.corpora_corpus_imports
        WHERE operation_id=p_operation AND corpus_id=m.id) THEN
        RETURN pg_catalog.jsonb_build_object('status','not_found');
    END IF;
    IF o.state IN ('completed','failed') THEN
        RETURN corpora_session_guard.import_status(p_actor,p_session,p_token_exp,p_corpus,p_operation);
    END IF;
    IF o.state<>'uncertain' THEN RETURN pg_catalog.jsonb_build_object('status','unresolved'); END IF;
    SELECT b.commit_sha INTO baseline FROM public.corpora_corpus_bootstrap b
        JOIN public.corpora_corpus_provision_ops p ON p.id=b.operation_id
            AND p.corpus_id=b.corpus_id AND p.worker_claim=b.worker_claim AND p.state='completed'
        WHERE b.corpus_id=m.id;
    IF m.state='provisioning' AND baseline IS NOT NULL
       AND NOT EXISTS(SELECT 1 FROM public.corpora_corpus_heads WHERE corpus_id=m.id)
       AND NOT EXISTS(SELECT 1 FROM public.corpora_corpus_staged_inputs WHERE operation_id=o.id)
       AND NOT EXISTS(SELECT 1 FROM public.corpora_corpus_publication_claims WHERE operation_id=o.id)
       AND NOT EXISTS(SELECT 1 FROM public.corpora_corpus_versions WHERE operation_id=o.id) THEN
        UPDATE public.corpora_corpus_write_ops SET state='failed' WHERE id=o.id;
        RETURN corpora_session_guard.import_status(p_actor,p_session,p_token_exp,p_corpus,p_operation);
    END IF;
    SELECT pg_catalog.jsonb_build_object(
        'status','ready','mode','import','corpus_id',m.id,'operation_id',o.id,'claim_id',o.worker_claim,
        'publication_claim',p.publication_claim,'repo_id',m.hf_repo_id,'repo_type',m.hf_repo_type,
        'expected_sequence',0,'expected_parent_sha',baseline,'input_sha256',o.input_sha256,
        'archive_size',s.archive_size,'stage_sha',s.stage_sha,'stage_path',s.stage_path
    ) INTO target FROM public.corpora_corpus_staged_inputs s
        JOIN public.corpora_corpus_publication_claims p ON p.operation_id=o.id
            AND p.corpus_id=o.corpus_id AND p.stage_claim=o.worker_claim
        JOIN public.corpora_corpus_imports i ON i.operation_id=o.id AND i.corpus_id=m.id
        WHERE s.operation_id=o.id AND s.corpus_id=m.id AND s.worker_claim=o.worker_claim
            AND s.repo_id=m.hf_repo_id AND s.repo_type=m.hf_repo_type
            AND s.input_sha256=o.input_sha256 AND s.input_format='corpus'
            AND s.archive_size=i.archive_size AND s.requested_base_sha=baseline
            AND m.state='provisioning' AND NOT EXISTS(
                SELECT 1 FROM public.corpora_corpus_heads WHERE corpus_id=m.id);
    PERFORM corpora_session_guard.lock_verified_session(p_actor,p_session,p_token_exp);
    RETURN coalesce(target,pg_catalog.jsonb_build_object('status','unresolved'));
END;
$$;
CREATE FUNCTION public.corpora_get_import_reconciliation(
    p_actor uuid,p_session uuid,p_token_exp bigint,p_corpus uuid,p_operation uuid
) RETURNS jsonb LANGUAGE sql SECURITY INVOKER SET search_path='' AS $$
    SELECT corpora_session_guard.import_reconciliation_target(p_actor,p_session,p_token_exp,p_corpus,p_operation);
$$;
REVOKE ALL ON FUNCTION corpora_session_guard.import_reconciliation_target(uuid,uuid,bigint,uuid,uuid)
    FROM PUBLIC,anon,authenticated;
REVOKE ALL ON FUNCTION public.corpora_get_import_reconciliation(uuid,uuid,bigint,uuid,uuid)
    FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION corpora_session_guard.import_reconciliation_target(uuid,uuid,bigint,uuid,uuid) TO service_role;
GRANT EXECUTE ON FUNCTION public.corpora_get_import_reconciliation(uuid,uuid,bigint,uuid,uuid) TO service_role;
NOTIFY pgrst,'reload schema';
