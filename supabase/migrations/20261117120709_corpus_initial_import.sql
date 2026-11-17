-- API-only admission and completion of the first validated corpus version.
-- The API verifies JWT signatures and real corpus bytes before admission;
-- completion additionally requires independently verified private Hub content.
CREATE TABLE public.corpora_corpus_imports (
    operation_id uuid PRIMARY KEY REFERENCES public.corpora_corpus_write_ops(id) ON DELETE RESTRICT,
    corpus_id uuid NOT NULL REFERENCES public.corpora_corpus_metadata(id) ON DELETE RESTRICT,
    metadata_revision bigint NOT NULL CHECK (metadata_revision > 0),
    archive_size bigint NOT NULL CHECK (archive_size BETWEEN 1 AND 3000000)
);
ALTER TABLE public.corpora_corpus_imports ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.corpora_corpus_imports FROM PUBLIC,anon,authenticated,service_role;
GRANT SELECT ON public.corpora_corpus_imports TO service_role;
CREATE TRIGGER corpora_corpus_imports_immutable BEFORE UPDATE OR DELETE
    ON public.corpora_corpus_imports FOR EACH ROW
    EXECUTE FUNCTION public.corpora_corpus_preserve_history();

CREATE FUNCTION corpora_session_guard.import_status(
    p_actor uuid,p_session uuid,p_token_exp bigint,p_corpus uuid,p_operation uuid
) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = ''
SET lock_timeout = '3s' AS $$
DECLARE result jsonb;
BEGIN
    PERFORM corpora_session_guard.lock_verified_session(p_actor,p_session,p_token_exp);
    SELECT pg_catalog.jsonb_build_object(
        'status','existing','corpus_id',m.id,'operation_id',o.id,'state',o.state,
        'sequence',v.sequence
    ) INTO result
    FROM public.corpora_corpus_metadata m
    JOIN public.corpora_corpus_write_ops o ON o.corpus_id=m.id AND o.id=p_operation
    JOIN public.corpora_corpus_imports i ON i.operation_id=o.id AND i.corpus_id=m.id
    LEFT JOIN public.corpora_corpus_versions v ON v.operation_id=o.id AND v.corpus_id=m.id
    WHERE m.id=p_corpus AND (m.owner_id=p_actor OR EXISTS (
        SELECT 1 FROM corpora_session_guard.superadmins WHERE user_id=p_actor));
    PERFORM corpora_session_guard.lock_verified_session(p_actor,p_session,p_token_exp);
    RETURN coalesce(result,pg_catalog.jsonb_build_object('status','not_found'));
END;
$$;

CREATE FUNCTION corpora_session_guard.begin_import(
    p_actor uuid,p_session uuid,p_token_exp bigint,p_corpus uuid,p_operation uuid,
    p_revision bigint,p_digest text,p_size bigint
) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = ''
SET lock_timeout = '3s' AS $$
DECLARE
    m public.corpora_corpus_metadata%ROWTYPE;
    o public.corpora_corpus_write_ops%ROWTYPE;
    i public.corpora_corpus_imports%ROWTYPE;
    baseline text;
    claim uuid := pg_catalog.gen_random_uuid();
BEGIN
    PERFORM corpora_session_guard.lock_verified_session(p_actor,p_session,p_token_exp);
    IF p_corpus IS NULL OR p_operation IS NULL OR p_revision IS NULL OR p_revision < 1
       OR p_digest IS NULL OR p_digest !~ '^[0-9a-f]{64}$'
       OR p_size IS NULL OR p_size NOT BETWEEN 1 AND 3000000 THEN
        RAISE EXCEPTION 'CORPUS_IMPORT_INPUT_INVALID';
    END IF;
    SELECT * INTO m FROM public.corpora_corpus_metadata WHERE id=p_corpus AND (
        owner_id=p_actor OR EXISTS (
            SELECT 1 FROM corpora_session_guard.superadmins WHERE user_id=p_actor)) FOR UPDATE;
    IF NOT FOUND THEN RETURN pg_catalog.jsonb_build_object('status','not_found'); END IF;
    SELECT * INTO o FROM public.corpora_corpus_write_ops WHERE id=p_operation;
    IF FOUND THEN
        SELECT * INTO i FROM public.corpora_corpus_imports WHERE operation_id=o.id;
        IF NOT FOUND OR o.corpus_id<>m.id OR i.corpus_id<>m.id OR o.actor_id<>p_actor
           OR o.input_sha256<>p_digest OR o.input_format<>'corpus' OR o.expected_sequence<>0
           OR i.metadata_revision<>p_revision OR i.archive_size<>p_size THEN
            RETURN pg_catalog.jsonb_build_object('status','conflict');
        END IF;
        RETURN corpora_session_guard.import_status(
            p_actor,p_session,p_token_exp,p_corpus,p_operation);
    END IF;
    SELECT b.commit_sha INTO baseline FROM public.corpora_corpus_bootstrap b
    JOIN public.corpora_corpus_provision_ops p ON p.id=b.operation_id
        AND p.corpus_id=b.corpus_id AND p.worker_claim=b.worker_claim AND p.state='completed'
    WHERE b.corpus_id=m.id;
    IF baseline IS NULL OR m.revision<>p_revision OR m.state<>'provisioning'
       OR EXISTS (SELECT 1 FROM public.corpora_corpus_heads WHERE corpus_id=m.id)
       OR EXISTS (SELECT 1 FROM public.corpora_corpus_write_ops WHERE corpus_id=m.id
           AND state IN ('admitted','executing','uncertain')) THEN
        RETURN pg_catalog.jsonb_build_object('status','conflict');
    END IF;
    INSERT INTO public.corpora_corpus_write_ops
        (id,corpus_id,actor_id,session_id,expected_sequence,input_sha256,input_format)
        VALUES(p_operation,m.id,p_actor,p_session,0,p_digest,'corpus') ON CONFLICT DO NOTHING;
    IF NOT FOUND THEN RETURN pg_catalog.jsonb_build_object('status','conflict'); END IF;
    INSERT INTO public.corpora_corpus_imports VALUES(p_operation,m.id,p_revision,p_size);
    UPDATE public.corpora_corpus_write_ops SET state='executing',worker_claim=claim
        WHERE id=p_operation;
    PERFORM corpora_session_guard.lock_verified_session(p_actor,p_session,p_token_exp);
    RETURN pg_catalog.jsonb_build_object(
        'status','claimed','corpus_id',m.id,'operation_id',p_operation,
        'claim_id',claim,'actor_id',p_actor,'session_id',p_session,
        'expected_sequence',0,'expected_parent_sha',NULL,'hub_parent_sha',baseline,
        'input_sha256',p_digest,'input_format','corpus','repo_id',m.hf_repo_id,
        'repo_type',m.hf_repo_type,'metadata_revision',m.revision,
        'retained_ref','corpora-op-'||p_operation::text);
END;
$$;

CREATE FUNCTION corpora_session_guard.stage_import(
    p_operation uuid,p_claim uuid,p_stage_sha text,p_publication uuid
) RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path = ''
SET lock_timeout = '3s' AS $$
DECLARE
    m public.corpora_corpus_metadata%ROWTYPE;
    o public.corpora_corpus_write_ops%ROWTYPE;
    i public.corpora_corpus_imports%ROWTYPE;
    baseline text;
BEGIN
    SELECT * INTO i FROM public.corpora_corpus_imports WHERE operation_id=p_operation;
    IF NOT FOUND THEN RETURN false; END IF;
    SELECT * INTO m FROM public.corpora_corpus_metadata WHERE id=i.corpus_id FOR UPDATE;
    SELECT * INTO o FROM public.corpora_corpus_write_ops WHERE id=p_operation FOR UPDATE;
    SELECT commit_sha INTO baseline FROM public.corpora_corpus_bootstrap WHERE corpus_id=m.id;
    IF o.corpus_id<>m.id OR o.worker_claim IS DISTINCT FROM p_claim OR o.state<>'executing'
       OR o.expected_sequence<>0 OR o.input_format<>'corpus' OR m.state<>'provisioning'
       OR baseline IS NULL OR p_stage_sha IS NULL OR p_stage_sha !~ '^[0-9a-f]{40}$'
       OR p_stage_sha=baseline OR p_publication IS NULL OR p_publication=p_claim
       OR EXISTS(SELECT 1 FROM public.corpora_corpus_heads WHERE corpus_id=m.id)
       OR EXISTS(SELECT 1 FROM public.corpora_corpus_staged_inputs WHERE operation_id=o.id) THEN
        RETURN false;
    END IF;
    INSERT INTO public.corpora_corpus_staged_inputs
        (operation_id,corpus_id,worker_claim,repo_id,repo_type,input_format,input_sha256,
         archive_size,requested_base_sha,stage_sha,stage_ref,stage_path)
        VALUES(o.id,m.id,p_claim,m.hf_repo_id,m.hf_repo_type,'corpus',o.input_sha256,
            i.archive_size,baseline,p_stage_sha,'corpora-stage-'||o.id::text,
            '.corpora-staging/'||o.id::text||'/'||p_claim::text||'.corpus');
    INSERT INTO public.corpora_corpus_publication_claims
        (operation_id,corpus_id,stage_claim,publication_claim)
        VALUES(o.id,m.id,p_claim,p_publication);
    RETURN true;
END;
$$;

CREATE FUNCTION corpora_session_guard.complete_import(
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
    IF o.state<>'executing' OR m.state<>'provisioning'
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

CREATE FUNCTION corpora_session_guard.mark_import_uncertain(p_operation uuid,p_claim uuid)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path = ''
SET lock_timeout = '3s' AS $$
DECLARE corpus uuid;
BEGIN
    SELECT corpus_id INTO corpus FROM public.corpora_corpus_imports WHERE operation_id=p_operation;
    IF NOT FOUND THEN RETURN false; END IF;
    PERFORM 1 FROM public.corpora_corpus_metadata WHERE id=corpus FOR UPDATE;
    UPDATE public.corpora_corpus_write_ops SET state='uncertain'
        WHERE id=p_operation AND corpus_id=corpus AND worker_claim=p_claim AND state='executing';
    RETURN EXISTS(SELECT 1 FROM public.corpora_corpus_write_ops
        WHERE id=p_operation AND worker_claim=p_claim AND state='uncertain');
END;
$$;

CREATE FUNCTION public.corpora_get_import(
    p_actor uuid,p_session uuid,p_token_exp bigint,p_corpus uuid,p_operation uuid
) RETURNS jsonb LANGUAGE sql SECURITY INVOKER SET search_path = '' AS $$
    SELECT corpora_session_guard.import_status(p_actor,p_session,p_token_exp,p_corpus,p_operation);
$$;
CREATE FUNCTION public.corpora_begin_import(
    p_actor uuid,p_session uuid,p_token_exp bigint,p_corpus uuid,p_operation uuid,
    p_revision bigint,p_digest text,p_size bigint
) RETURNS jsonb LANGUAGE sql SECURITY INVOKER SET search_path = '' AS $$
    SELECT corpora_session_guard.begin_import(
        p_actor,p_session,p_token_exp,p_corpus,p_operation,p_revision,p_digest,p_size);
$$;
CREATE FUNCTION public.corpora_stage_import(
    p_operation uuid,p_claim uuid,p_stage_sha text,p_publication uuid
) RETURNS boolean LANGUAGE sql SECURITY INVOKER SET search_path = '' AS $$
    SELECT corpora_session_guard.stage_import(p_operation,p_claim,p_stage_sha,p_publication);
$$;
CREATE FUNCTION public.corpora_complete_import(
    p_operation uuid,p_claim uuid,p_publication uuid,p_sha text
) RETURNS boolean LANGUAGE sql SECURITY INVOKER SET search_path = '' AS $$
    SELECT corpora_session_guard.complete_import(p_operation,p_claim,p_publication,p_sha);
$$;
CREATE FUNCTION public.corpora_mark_import_uncertain(p_operation uuid,p_claim uuid)
RETURNS boolean LANGUAGE sql SECURITY INVOKER SET search_path = '' AS $$
    SELECT corpora_session_guard.mark_import_uncertain(p_operation,p_claim);
$$;
CREATE FUNCTION public.corpora_find_import_result(
    p_operation uuid,p_claim uuid,p_publication uuid,p_sha text
) RETURNS boolean LANGUAGE sql SECURITY INVOKER SET search_path = '' AS $$
    SELECT EXISTS (SELECT 1 FROM public.corpora_corpus_write_ops o
        JOIN public.corpora_corpus_versions v ON v.operation_id=o.id AND v.corpus_id=o.corpus_id
        WHERE o.id=p_operation AND o.worker_claim=p_claim AND o.state='completed'
            AND v.publication_claim=p_publication AND v.commit_sha=p_sha AND v.sequence=1);
$$;

DO $$ DECLARE fn regprocedure; BEGIN
    FOREACH fn IN ARRAY ARRAY[
        'corpora_session_guard.import_status(uuid,uuid,bigint,uuid,uuid)'::regprocedure,
        'corpora_session_guard.begin_import(uuid,uuid,bigint,uuid,uuid,bigint,text,bigint)'::regprocedure,
        'corpora_session_guard.stage_import(uuid,uuid,text,uuid)'::regprocedure,
        'corpora_session_guard.complete_import(uuid,uuid,uuid,text)'::regprocedure,
        'corpora_session_guard.mark_import_uncertain(uuid,uuid)'::regprocedure,
        'public.corpora_get_import(uuid,uuid,bigint,uuid,uuid)'::regprocedure,
        'public.corpora_begin_import(uuid,uuid,bigint,uuid,uuid,bigint,text,bigint)'::regprocedure,
        'public.corpora_stage_import(uuid,uuid,text,uuid)'::regprocedure,
        'public.corpora_complete_import(uuid,uuid,uuid,text)'::regprocedure,
        'public.corpora_mark_import_uncertain(uuid,uuid)'::regprocedure,
        'public.corpora_find_import_result(uuid,uuid,uuid,text)'::regprocedure
    ] LOOP
        EXECUTE format('ALTER FUNCTION %s OWNER TO postgres',fn);
        EXECUTE format('REVOKE ALL ON FUNCTION %s FROM PUBLIC,anon,authenticated',fn);
        EXECUTE format('GRANT EXECUTE ON FUNCTION %s TO service_role',fn);
    END LOOP;
END $$;
NOTIFY pgrst,'reload schema';
