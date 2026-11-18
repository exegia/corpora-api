-- Reconciliation may finalize a verified candidate from an uncertain operation.
CREATE OR REPLACE FUNCTION public.corpora_corpus_validate_version()
RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER SET search_path = '' AS $$
DECLARE
    request_row public.corpora_corpus_write_ops%ROWTYPE;
BEGIN
    PERFORM 1 FROM public.corpora_corpus_metadata WHERE id=NEW.corpus_id FOR UPDATE;
    PERFORM 1 FROM public.corpora_corpus_write_ops WHERE id=NEW.operation_id FOR UPDATE;
    SELECT * INTO request_row FROM public.corpora_corpus_write_ops
        WHERE id = NEW.operation_id AND corpus_id = NEW.corpus_id;
    IF NOT FOUND OR request_row.state NOT IN ('executing','uncertain')
       OR NEW.sequence <> request_row.expected_sequence + 1
       OR NEW.parent_sha IS DISTINCT FROM request_row.expected_parent_sha
       OR NOT EXISTS (
           SELECT 1 FROM public.corpora_corpus_staged_inputs s
           WHERE s.operation_id = NEW.operation_id AND s.corpus_id = NEW.corpus_id
             AND s.worker_claim = request_row.worker_claim AND s.stage_sha <> NEW.commit_sha
       )
       OR (NEW.sequence = 1 AND NOT EXISTS (
           SELECT 1 FROM public.corpora_corpus_bootstrap b WHERE b.corpus_id = NEW.corpus_id
       )) THEN
        RAISE EXCEPTION 'CORPUS_VERSION_OPERATION_INVALID';
    END IF;
    RETURN NEW;
END;
$$;

-- Return only the immutable receipts of an authorized uncertain save.
CREATE FUNCTION corpora_session_guard.reconciliation_target(
    p_actor uuid,p_session uuid,p_token_exp bigint,p_corpus uuid,p_operation uuid
) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path='' SET lock_timeout='3s' AS $$
DECLARE m public.corpora_corpus_metadata%ROWTYPE; o public.corpora_corpus_write_ops%ROWTYPE; target jsonb;
BEGIN
    PERFORM corpora_session_guard.lock_verified_session(p_actor,p_session,p_token_exp);
    SELECT * INTO m FROM public.corpora_corpus_metadata WHERE id=p_corpus AND (
        owner_id=p_actor OR EXISTS(SELECT 1 FROM corpora_session_guard.superadmins WHERE user_id=p_actor)
    ) FOR SHARE;
    IF NOT FOUND OR NOT EXISTS(SELECT 1 FROM public.corpora_corpus_access
        WHERE corpus_id=p_corpus AND deleted_at IS NULL) THEN
        RETURN pg_catalog.jsonb_build_object('status','not_found');
    END IF;
    SELECT * INTO o FROM public.corpora_corpus_write_ops
        WHERE id=p_operation AND corpus_id=m.id AND expected_sequence>0;
    IF NOT FOUND OR NOT EXISTS(SELECT 1 FROM public.corpora_corpus_version_intents
        WHERE operation_id=p_operation AND corpus_id=m.id) THEN
        RETURN pg_catalog.jsonb_build_object('status','not_found');
    END IF;
    IF o.state='completed' THEN
        RETURN corpora_session_guard.version_save_status(p_actor,p_session,p_token_exp,p_corpus,p_operation);
    END IF;
    IF o.state<>'uncertain' THEN
        RETURN pg_catalog.jsonb_build_object('status','unresolved');
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

CREATE FUNCTION corpora_session_guard.reconcile_version_save(
    p_operation uuid,p_claim uuid,p_publication uuid,p_sha text
) RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path = ''
SET lock_timeout = '3s' AS $$
DECLARE
    m public.corpora_corpus_metadata%ROWTYPE;
    o public.corpora_corpus_write_ops%ROWTYPE;
    i public.corpora_corpus_version_saves%ROWTYPE;
    h public.corpora_corpus_heads%ROWTYPE;
BEGIN
    SELECT * INTO i FROM public.corpora_corpus_version_saves WHERE operation_id=p_operation;
    IF NOT FOUND THEN RETURN false; END IF;
    SELECT * INTO m FROM public.corpora_corpus_metadata WHERE id=i.corpus_id FOR UPDATE;
    SELECT * INTO o FROM public.corpora_corpus_write_ops WHERE id=p_operation FOR UPDATE;
    IF m.id IS NULL OR o.id IS NULL OR p_claim IS NULL OR p_publication IS NULL
       OR o.corpus_id<>m.id OR o.worker_claim IS DISTINCT FROM p_claim OR o.expected_sequence<1
       OR p_sha IS NULL OR p_sha !~ '^[0-9a-f]{40}$'
       OR NOT EXISTS(SELECT 1 FROM public.corpora_corpus_publication_claims
           WHERE operation_id=o.id AND corpus_id=m.id AND stage_claim=p_claim
               AND publication_claim=p_publication) THEN RETURN false; END IF;
    IF o.state='completed' THEN
        RETURN EXISTS(SELECT 1 FROM public.corpora_corpus_versions
            WHERE operation_id=o.id AND corpus_id=m.id AND publication_claim=p_publication
                AND commit_sha=p_sha AND sequence=o.expected_sequence+1);
    END IF;
    SELECT * INTO h FROM public.corpora_corpus_heads WHERE corpus_id=m.id;
    IF h.corpus_id IS NULL OR o.state NOT IN ('executing','uncertain') OR m.state<>'ready'
       OR h.sequence IS DISTINCT FROM o.expected_sequence OR h.commit_sha IS DISTINCT FROM o.expected_parent_sha
       OR NOT EXISTS(SELECT 1 FROM public.corpora_corpus_access WHERE corpus_id=m.id AND deleted_at IS NULL)
       OR NOT EXISTS(SELECT 1 FROM public.corpora_corpus_staged_inputs s
           WHERE s.operation_id=o.id AND s.corpus_id=m.id AND s.worker_claim=p_claim
               AND s.requested_base_sha=o.expected_parent_sha AND s.stage_sha<>p_sha
               AND o.expected_parent_sha<>p_sha AND s.input_sha256=o.input_sha256
               AND s.archive_size=i.archive_size AND s.repo_id=m.hf_repo_id AND s.repo_type=m.hf_repo_type)
       THEN RETURN false; END IF;
    INSERT INTO public.corpora_corpus_versions
        (corpus_id,sequence,commit_sha,parent_sha,operation_id,publication_claim,retained_ref)
        VALUES(m.id,o.expected_sequence+1,p_sha,o.expected_parent_sha,o.id,p_publication,
               'corpora-op-'||o.id::text);
    UPDATE public.corpora_corpus_heads SET sequence=o.expected_sequence+1,commit_sha=p_sha
        WHERE corpus_id=m.id AND sequence=o.expected_sequence AND commit_sha=o.expected_parent_sha;
    IF NOT FOUND THEN RAISE EXCEPTION 'CORPUS_VERSION_HEAD_CHANGED'; END IF;
    UPDATE public.corpora_corpus_write_ops SET state='completed' WHERE id=o.id;
    RETURN true;
END;
$$;

CREATE FUNCTION public.corpora_get_save_reconciliation(
    p_actor uuid,p_session uuid,p_token_exp bigint,p_corpus uuid,p_operation uuid
) RETURNS jsonb LANGUAGE sql SECURITY INVOKER SET search_path='' AS $$
    SELECT corpora_session_guard.reconciliation_target(p_actor,p_session,p_token_exp,p_corpus,p_operation);
$$;
CREATE FUNCTION public.corpora_finish_save_reconciliation(
    p_operation uuid,p_claim uuid,p_publication uuid,p_sha text
) RETURNS boolean LANGUAGE sql SECURITY INVOKER SET search_path='' AS $$
    SELECT corpora_session_guard.reconcile_version_save(p_operation,p_claim,p_publication,p_sha);
$$;
DO $$ DECLARE fn regprocedure; BEGIN
    FOREACH fn IN ARRAY ARRAY[
        'corpora_session_guard.reconciliation_target(uuid,uuid,bigint,uuid,uuid)'::regprocedure,
        'corpora_session_guard.reconcile_version_save(uuid,uuid,uuid,text)'::regprocedure,
        'public.corpora_get_save_reconciliation(uuid,uuid,bigint,uuid,uuid)'::regprocedure,
        'public.corpora_finish_save_reconciliation(uuid,uuid,uuid,text)'::regprocedure
    ] LOOP
        EXECUTE pg_catalog.format('REVOKE ALL ON FUNCTION %s FROM PUBLIC,anon,authenticated',fn);
        EXECUTE pg_catalog.format('GRANT EXECUTE ON FUNCTION %s TO service_role',fn);
    END LOOP;
END $$;
NOTIFY pgrst, 'reload schema';
