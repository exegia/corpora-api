-- Keep later saves distinct from the initial-import operation namespace.
CREATE TABLE public.corpora_corpus_version_saves (
    operation_id uuid PRIMARY KEY,
    corpus_id uuid NOT NULL REFERENCES public.corpora_corpus_metadata(id) ON DELETE RESTRICT,
    metadata_revision bigint NOT NULL CHECK (metadata_revision > 0),
    archive_size bigint NOT NULL CHECK (archive_size BETWEEN 1 AND 3000000),
    FOREIGN KEY (operation_id, corpus_id) REFERENCES public.corpora_corpus_write_ops(id, corpus_id) ON DELETE RESTRICT
);
ALTER TABLE public.corpora_corpus_version_saves ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.corpora_corpus_version_saves FROM PUBLIC,anon,authenticated,service_role;
GRANT SELECT ON public.corpora_corpus_version_saves TO service_role;
CREATE TRIGGER corpora_corpus_version_saves_immutable BEFORE UPDATE OR DELETE
    ON public.corpora_corpus_version_saves FOR EACH ROW
    EXECUTE FUNCTION public.corpora_corpus_preserve_history();

-- Every finalized version has an immutable stage receipt with its archive
-- digest and size. This supports both existing initial imports and later saves.
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

CREATE FUNCTION corpora_session_guard.version_save_status(
    p_actor uuid,p_session uuid,p_token_exp bigint,p_corpus uuid,p_operation uuid
) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = ''
SET lock_timeout = '3s' AS $$
DECLARE result jsonb;
BEGIN
    PERFORM corpora_session_guard.lock_verified_session(p_actor,p_session,p_token_exp);
    SELECT pg_catalog.jsonb_build_object(
        'status','existing','corpus_id',m.id,'operation_id',o.id,'state',o.state,'sequence',v.sequence
    ) INTO result FROM public.corpora_corpus_metadata m
    JOIN public.corpora_corpus_access a ON a.corpus_id=m.id AND a.deleted_at IS NULL
    JOIN public.corpora_corpus_write_ops o ON o.corpus_id=m.id AND o.id=p_operation
    JOIN public.corpora_corpus_version_saves i ON i.operation_id=o.id AND i.corpus_id=m.id
    LEFT JOIN public.corpora_corpus_versions v ON v.operation_id=o.id AND v.corpus_id=m.id
    WHERE m.id=p_corpus AND o.expected_sequence>0 AND (m.owner_id=p_actor OR EXISTS (
        SELECT 1 FROM corpora_session_guard.superadmins WHERE user_id=p_actor));
    PERFORM corpora_session_guard.lock_verified_session(p_actor,p_session,p_token_exp);
    RETURN coalesce(result,pg_catalog.jsonb_build_object('status','not_found'));
END;
$$;

CREATE FUNCTION corpora_session_guard.begin_version_save(
    p_actor uuid,p_session uuid,p_token_exp bigint,p_corpus uuid,p_operation uuid,
    p_sequence bigint,p_parent text,p_digest text,p_size bigint
) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = ''
SET lock_timeout = '3s' AS $$
DECLARE
    m public.corpora_corpus_metadata%ROWTYPE;
    o public.corpora_corpus_write_ops%ROWTYPE;
    i public.corpora_corpus_version_saves%ROWTYPE;
    h public.corpora_corpus_heads%ROWTYPE;
    claim uuid := pg_catalog.gen_random_uuid();
BEGIN
    PERFORM corpora_session_guard.lock_verified_session(p_actor,p_session,p_token_exp);
    IF p_corpus IS NULL OR p_operation IS NULL OR p_sequence IS NULL
       OR p_sequence < 1 OR p_sequence >= 9223372036854775807
       OR p_parent IS NULL OR p_parent !~ '^[0-9a-f]{40}$'
       OR p_digest IS NULL OR p_digest !~ '^[0-9a-f]{64}$'
       OR p_size IS NULL OR p_size NOT BETWEEN 1 AND 3000000 THEN
        RAISE EXCEPTION 'CORPUS_VERSION_SAVE_INPUT_INVALID';
    END IF;
    SELECT * INTO m FROM public.corpora_corpus_metadata WHERE id=p_corpus AND (
        owner_id=p_actor OR EXISTS (SELECT 1 FROM corpora_session_guard.superadmins WHERE user_id=p_actor)
    ) FOR UPDATE;
    IF NOT FOUND OR NOT EXISTS(SELECT 1 FROM public.corpora_corpus_access
        WHERE corpus_id=p_corpus AND deleted_at IS NULL) THEN
        RETURN pg_catalog.jsonb_build_object('status','not_found');
    END IF;
    SELECT * INTO o FROM public.corpora_corpus_write_ops WHERE id=p_operation;
    IF FOUND THEN
        SELECT * INTO i FROM public.corpora_corpus_version_saves WHERE operation_id=o.id;
        IF NOT FOUND OR o.corpus_id<>m.id OR i.corpus_id<>m.id OR o.actor_id<>p_actor
           OR o.expected_sequence<>p_sequence OR o.expected_parent_sha IS DISTINCT FROM p_parent
           OR o.input_sha256<>p_digest OR o.input_format<>'corpus' OR i.archive_size<>p_size THEN
            RETURN pg_catalog.jsonb_build_object('status','conflict');
        END IF;
        RETURN corpora_session_guard.version_save_status(
            p_actor,p_session,p_token_exp,p_corpus,p_operation);
    END IF;
    SELECT * INTO h FROM public.corpora_corpus_heads WHERE corpus_id=m.id;
    IF NOT FOUND OR m.state<>'ready' OR h.sequence<>p_sequence OR h.commit_sha<>p_parent
       OR EXISTS (SELECT 1 FROM public.corpora_corpus_write_ops WHERE corpus_id=m.id
           AND state IN ('admitted','executing','uncertain')) THEN
        RETURN pg_catalog.jsonb_build_object('status','conflict');
    END IF;
    INSERT INTO public.corpora_corpus_write_ops
        (id,corpus_id,actor_id,session_id,expected_sequence,expected_parent_sha,input_sha256,input_format)
        VALUES(p_operation,m.id,p_actor,p_session,p_sequence,p_parent,p_digest,'corpus')
        ON CONFLICT DO NOTHING;
    IF NOT FOUND THEN RETURN pg_catalog.jsonb_build_object('status','conflict'); END IF;
    INSERT INTO public.corpora_corpus_version_saves VALUES(p_operation,m.id,m.revision,p_size);
    UPDATE public.corpora_corpus_write_ops SET state='executing',worker_claim=claim WHERE id=p_operation;
    PERFORM corpora_session_guard.lock_verified_session(p_actor,p_session,p_token_exp);
    RETURN pg_catalog.jsonb_build_object(
        'status','claimed','corpus_id',m.id,'operation_id',p_operation,'claim_id',claim,
        'actor_id',p_actor,'session_id',p_session,'expected_sequence',p_sequence,
        'expected_parent_sha',p_parent,'hub_parent_sha',p_parent,'input_sha256',p_digest,
        'input_format','corpus','repo_id',m.hf_repo_id,'repo_type',m.hf_repo_type,
        'metadata_revision',m.revision,'retained_ref','corpora-op-'||p_operation::text);
END;
$$;

CREATE FUNCTION corpora_session_guard.stage_version_save(
    p_operation uuid,p_claim uuid,p_stage_sha text,p_publication uuid
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
    SELECT * INTO h FROM public.corpora_corpus_heads WHERE corpus_id=m.id;
    IF m.id IS NULL OR o.id IS NULL OR h.corpus_id IS NULL OR p_claim IS NULL
       OR o.corpus_id<>m.id OR o.worker_claim IS DISTINCT FROM p_claim OR o.state<>'executing'
       OR o.expected_sequence<1 OR o.input_format<>'corpus' OR m.state<>'ready'
       OR h.sequence IS DISTINCT FROM o.expected_sequence OR h.commit_sha IS DISTINCT FROM o.expected_parent_sha
       OR NOT EXISTS(SELECT 1 FROM public.corpora_corpus_access WHERE corpus_id=m.id AND deleted_at IS NULL)
       OR p_stage_sha IS NULL OR p_stage_sha !~ '^[0-9a-f]{40}$' OR p_stage_sha=o.expected_parent_sha
       OR p_publication IS NULL OR p_publication=p_claim
       OR EXISTS(SELECT 1 FROM public.corpora_corpus_staged_inputs WHERE operation_id=o.id) THEN
        RETURN false;
    END IF;
    INSERT INTO public.corpora_corpus_staged_inputs
        (operation_id,corpus_id,worker_claim,repo_id,repo_type,input_format,input_sha256,
         archive_size,requested_base_sha,stage_sha,stage_ref,stage_path)
        VALUES(o.id,m.id,p_claim,m.hf_repo_id,m.hf_repo_type,'corpus',o.input_sha256,
            i.archive_size,o.expected_parent_sha,p_stage_sha,'corpora-stage-'||o.id::text,
            '.corpora-staging/'||o.id::text||'/'||p_claim::text||'.corpus');
    INSERT INTO public.corpora_corpus_publication_claims(operation_id,corpus_id,stage_claim,publication_claim)
        VALUES(o.id,m.id,p_claim,p_publication);
    RETURN true;
END;
$$;

CREATE FUNCTION corpora_session_guard.complete_version_save(
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
    IF h.corpus_id IS NULL OR o.state<>'executing' OR m.state<>'ready'
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

CREATE FUNCTION corpora_session_guard.mark_version_save_uncertain(p_operation uuid,p_claim uuid)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path = ''
SET lock_timeout = '3s' AS $$
DECLARE corpus uuid;
BEGIN
    SELECT corpus_id INTO corpus FROM public.corpora_corpus_write_ops
        WHERE id=p_operation AND expected_sequence>0;
    IF NOT FOUND THEN RETURN false; END IF;
    PERFORM 1 FROM public.corpora_corpus_metadata WHERE id=corpus FOR UPDATE;
    UPDATE public.corpora_corpus_write_ops SET state='uncertain'
        WHERE id=p_operation AND corpus_id=corpus AND worker_claim=p_claim AND state='executing';
    RETURN EXISTS(SELECT 1 FROM public.corpora_corpus_write_ops
        WHERE id=p_operation AND worker_claim=p_claim AND state='uncertain');
END;
$$;

CREATE FUNCTION public.corpora_get_version_save(
    p_actor uuid,p_session uuid,p_token_exp bigint,p_corpus uuid,p_operation uuid
) RETURNS jsonb LANGUAGE sql SECURITY INVOKER SET search_path = '' AS $$
    SELECT corpora_session_guard.version_save_status(p_actor,p_session,p_token_exp,p_corpus,p_operation);
$$;
CREATE FUNCTION public.corpora_begin_version_save(
    p_actor uuid,p_session uuid,p_token_exp bigint,p_corpus uuid,p_operation uuid,
    p_sequence bigint,p_parent text,p_digest text,p_size bigint
) RETURNS jsonb LANGUAGE sql SECURITY INVOKER SET search_path = '' AS $$
    SELECT corpora_session_guard.begin_version_save(
        p_actor,p_session,p_token_exp,p_corpus,p_operation,p_sequence,p_parent,p_digest,p_size);
$$;
-- Trusted workers may retrieve only the immutable base bound to their active claim.
CREATE FUNCTION public.corpora_version_save_base(p_operation uuid,p_claim uuid)
RETURNS jsonb LANGUAGE sql SECURITY INVOKER SET search_path = '' AS $$
    SELECT pg_catalog.jsonb_build_object(
        'commit_sha',h.commit_sha,'sequence',h.sequence,'retained_ref',v.retained_ref,
        'archive_size',s.archive_size,'archive_sha256',base.input_sha256)
    FROM public.corpora_corpus_version_saves i
    JOIN public.corpora_corpus_write_ops o ON o.id=i.operation_id AND o.corpus_id=i.corpus_id
    JOIN public.corpora_corpus_metadata m ON m.id=o.corpus_id AND m.state='ready'
    JOIN public.corpora_corpus_access a ON a.corpus_id=m.id AND a.deleted_at IS NULL
    JOIN public.corpora_corpus_heads h ON h.corpus_id=m.id
        AND h.sequence=o.expected_sequence AND h.commit_sha=o.expected_parent_sha
    JOIN public.corpora_corpus_versions v ON v.corpus_id=h.corpus_id
        AND v.sequence=h.sequence AND v.commit_sha=h.commit_sha
    JOIN public.corpora_corpus_write_ops base ON base.id=v.operation_id
        AND base.corpus_id=v.corpus_id AND base.state='completed' AND base.input_format='corpus'
    JOIN public.corpora_corpus_publication_claims pc ON pc.operation_id=base.id
        AND pc.corpus_id=base.corpus_id AND pc.publication_claim=v.publication_claim
        AND pc.stage_claim=base.worker_claim
    JOIN public.corpora_corpus_staged_inputs s ON s.operation_id=base.id
        AND s.corpus_id=base.corpus_id AND s.worker_claim=base.worker_claim
        AND s.input_sha256=base.input_sha256 AND s.input_format=base.input_format
        AND s.repo_id=m.hf_repo_id AND s.repo_type=m.hf_repo_type
    WHERE o.id=p_operation AND o.worker_claim=p_claim AND o.state='executing'
        AND o.expected_sequence>0;
$$;
CREATE FUNCTION public.corpora_stage_version_save(
    p_operation uuid,p_claim uuid,p_stage_sha text,p_publication uuid
) RETURNS boolean LANGUAGE sql SECURITY INVOKER SET search_path = '' AS $$
    SELECT corpora_session_guard.stage_version_save(p_operation,p_claim,p_stage_sha,p_publication);
$$;
CREATE FUNCTION public.corpora_complete_version_save(
    p_operation uuid,p_claim uuid,p_publication uuid,p_sha text
) RETURNS boolean LANGUAGE sql SECURITY INVOKER SET search_path = '' AS $$
    SELECT corpora_session_guard.complete_version_save(p_operation,p_claim,p_publication,p_sha);
$$;
CREATE FUNCTION public.corpora_mark_version_save_uncertain(p_operation uuid,p_claim uuid)
RETURNS boolean LANGUAGE sql SECURITY INVOKER SET search_path = '' AS $$
    SELECT corpora_session_guard.mark_version_save_uncertain(p_operation,p_claim);
$$;
CREATE FUNCTION public.corpora_find_version_save_result(
    p_operation uuid,p_claim uuid,p_publication uuid,p_sha text
) RETURNS boolean LANGUAGE sql SECURITY INVOKER SET search_path = '' AS $$
    SELECT EXISTS(SELECT 1 FROM public.corpora_corpus_write_ops o
        JOIN public.corpora_corpus_versions v ON v.operation_id=o.id AND v.corpus_id=o.corpus_id
        WHERE o.id=p_operation AND o.expected_sequence>0 AND o.worker_claim=p_claim AND o.state='completed'
            AND v.publication_claim=p_publication AND v.commit_sha=p_sha AND v.sequence=o.expected_sequence+1);
$$;

DO $$ DECLARE fn regprocedure; BEGIN
    FOREACH fn IN ARRAY ARRAY[
        'corpora_session_guard.version_save_status(uuid,uuid,bigint,uuid,uuid)'::regprocedure,
        'corpora_session_guard.begin_version_save(uuid,uuid,bigint,uuid,uuid,bigint,text,text,bigint)'::regprocedure,
        'corpora_session_guard.stage_version_save(uuid,uuid,text,uuid)'::regprocedure,
        'corpora_session_guard.complete_version_save(uuid,uuid,uuid,text)'::regprocedure,
        'corpora_session_guard.mark_version_save_uncertain(uuid,uuid)'::regprocedure,
        'public.corpora_get_version_save(uuid,uuid,bigint,uuid,uuid)'::regprocedure,
        'public.corpora_begin_version_save(uuid,uuid,bigint,uuid,uuid,bigint,text,text,bigint)'::regprocedure,
        'public.corpora_version_save_base(uuid,uuid)'::regprocedure,
        'public.corpora_stage_version_save(uuid,uuid,text,uuid)'::regprocedure,
        'public.corpora_complete_version_save(uuid,uuid,uuid,text)'::regprocedure,
        'public.corpora_mark_version_save_uncertain(uuid,uuid)'::regprocedure,
        'public.corpora_find_version_save_result(uuid,uuid,uuid,text)'::regprocedure
    ] LOOP
        EXECUTE pg_catalog.format('REVOKE ALL ON FUNCTION %s FROM PUBLIC,anon,authenticated',fn);
        EXECUTE pg_catalog.format('GRANT EXECUTE ON FUNCTION %s TO service_role',fn);
    END LOOP;
END $$;
NOTIFY pgrst, 'reload schema';
