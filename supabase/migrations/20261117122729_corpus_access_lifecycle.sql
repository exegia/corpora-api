-- API visibility is independent of private Hub storage and content readiness.
SET LOCAL lock_timeout='3s';
LOCK TABLE public.corpora_corpus_metadata IN SHARE ROW EXCLUSIVE MODE;
CREATE TABLE public.corpora_corpus_access (
    corpus_id uuid PRIMARY KEY REFERENCES public.corpora_corpus_metadata(id) ON DELETE RESTRICT,
    visibility text NOT NULL DEFAULT 'private' CHECK (visibility IN ('private','public')),
    deleted_at timestamptz,
    CHECK (deleted_at IS NULL OR visibility='private')
);
INSERT INTO public.corpora_corpus_access(corpus_id) SELECT id FROM public.corpora_corpus_metadata;
ALTER TABLE public.corpora_corpus_access ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.corpora_corpus_access FROM PUBLIC,anon,authenticated,service_role;
GRANT SELECT ON public.corpora_corpus_access TO service_role;

CREATE FUNCTION corpora_session_guard.initialize_access()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path='' AS $$
BEGIN
    INSERT INTO public.corpora_corpus_access(corpus_id) VALUES(NEW.id);
    RETURN NEW;
END;
$$;
CREATE TRIGGER corpora_corpus_initialize_access AFTER INSERT ON public.corpora_corpus_metadata
    FOR EACH ROW EXECUTE FUNCTION corpora_session_guard.initialize_access();

CREATE FUNCTION corpora_session_guard.guard_access()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path='' AS $$
BEGIN
    IF TG_OP='DELETE' OR NEW.corpus_id IS DISTINCT FROM OLD.corpus_id
       OR OLD.deleted_at IS NOT NULL THEN RAISE EXCEPTION 'CORPUS_ACCESS_IMMUTABLE'; END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER corpora_corpus_access_guard BEFORE UPDATE OR DELETE ON public.corpora_corpus_access
    FOR EACH ROW EXECUTE FUNCTION corpora_session_guard.guard_access();

CREATE FUNCTION corpora_session_guard.require_live_corpus()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path='' AS $$
DECLARE identifier uuid;
BEGIN
    IF TG_TABLE_NAME='corpora_corpus_metadata' THEN identifier:=NEW.id;
    ELSE identifier:=NEW.corpus_id; END IF;
    PERFORM 1 FROM public.corpora_corpus_metadata WHERE id=identifier FOR UPDATE;
    IF NOT EXISTS (SELECT 1 FROM public.corpora_corpus_access
        WHERE corpus_id=identifier AND deleted_at IS NULL) THEN
        RAISE EXCEPTION 'CORPUS_DELETED';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER corpora_corpus_live_metadata BEFORE UPDATE ON public.corpora_corpus_metadata
    FOR EACH ROW EXECUTE FUNCTION corpora_session_guard.require_live_corpus();
CREATE TRIGGER corpora_corpus_live_provision BEFORE INSERT ON public.corpora_corpus_provision_ops
    FOR EACH ROW EXECUTE FUNCTION corpora_session_guard.require_live_corpus();
CREATE TRIGGER corpora_corpus_live_write BEFORE INSERT ON public.corpora_corpus_write_ops
    FOR EACH ROW EXECUTE FUNCTION corpora_session_guard.require_live_corpus();

CREATE TABLE public.corpora_corpus_deletions (
    id uuid PRIMARY KEY,
    corpus_id uuid NOT NULL UNIQUE REFERENCES public.corpora_corpus_metadata(id) ON DELETE RESTRICT,
    actor_id uuid NOT NULL REFERENCES auth.users(id) ON DELETE RESTRICT,
    expected_revision bigint NOT NULL CHECK(expected_revision>0),
    worker_claim uuid NOT NULL UNIQUE,
    state text NOT NULL DEFAULT 'executing' CHECK(state IN ('executing','uncertain','completed')),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE public.corpora_corpus_deletions ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.corpora_corpus_deletions FROM PUBLIC,anon,authenticated,service_role;
GRANT SELECT ON public.corpora_corpus_deletions TO service_role;
CREATE FUNCTION corpora_session_guard.guard_deletion()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path='' AS $$
BEGIN
    IF TG_OP='DELETE' OR NEW.id IS DISTINCT FROM OLD.id
       OR NEW.corpus_id IS DISTINCT FROM OLD.corpus_id OR NEW.actor_id IS DISTINCT FROM OLD.actor_id
       OR NEW.expected_revision IS DISTINCT FROM OLD.expected_revision
       OR NEW.worker_claim IS DISTINCT FROM OLD.worker_claim OR NEW.created_at IS DISTINCT FROM OLD.created_at
       OR (OLD.state='completed' AND NEW.state<>'completed')
       OR (OLD.state='uncertain' AND NEW.state='executing') THEN
        RAISE EXCEPTION 'CORPUS_DELETION_IMMUTABLE';
    END IF;
    NEW.updated_at:=clock_timestamp();
    RETURN NEW;
END;
$$;
CREATE TRIGGER corpora_corpus_deletion_guard BEFORE UPDATE OR DELETE ON public.corpora_corpus_deletions
    FOR EACH ROW EXECUTE FUNCTION corpora_session_guard.guard_deletion();

-- Only the server may supply verified optional JWT claims; browser roles cannot call this.
CREATE FUNCTION corpora_session_guard.read_access(
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
        'retained_ref',v.retained_ref,'archive_size',i.archive_size,'archive_sha256',o.input_sha256)
    INTO head FROM public.corpora_corpus_heads h
        JOIN public.corpora_corpus_versions v ON v.corpus_id=h.corpus_id AND v.sequence=h.sequence
            AND v.commit_sha=h.commit_sha
        JOIN public.corpora_corpus_write_ops o ON o.id=v.operation_id AND o.state='completed'
        JOIN public.corpora_corpus_imports i ON i.operation_id=o.id
    WHERE h.corpus_id=m.id;
    IF p_actor IS NOT NULL THEN
        PERFORM corpora_session_guard.lock_verified_session(p_actor,p_session,p_token_exp);
    END IF;
    RETURN jsonb_build_object('status','ok','row',to_jsonb(m),'visibility',a.visibility,'head',head);
END;
$$;

CREATE FUNCTION corpora_session_guard.set_visibility(
    p_actor uuid,p_session uuid,p_token_exp bigint,p_corpus uuid,p_revision bigint,p_visibility text
) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path='' SET lock_timeout='3s' AS $$
DECLARE m public.corpora_corpus_metadata%ROWTYPE; a public.corpora_corpus_access%ROWTYPE;
BEGIN
    PERFORM corpora_session_guard.lock_verified_session(p_actor,p_session,p_token_exp);
    IF p_revision IS NULL OR p_revision<1 OR p_visibility IS NULL OR p_visibility NOT IN ('private','public') THEN
        RAISE EXCEPTION 'CORPUS_ACCESS_INPUT_INVALID'; END IF;
    SELECT * INTO m FROM public.corpora_corpus_metadata WHERE id=p_corpus AND (
        owner_id=p_actor OR EXISTS(SELECT 1 FROM corpora_session_guard.superadmins WHERE user_id=p_actor)) FOR UPDATE;
    SELECT * INTO a FROM public.corpora_corpus_access WHERE corpus_id=m.id AND deleted_at IS NULL;
    IF a.corpus_id IS NULL THEN RETURN jsonb_build_object('status','not_found'); END IF;
    IF m.revision<>p_revision OR (p_visibility='public' AND (m.state<>'ready' OR NOT EXISTS (
        SELECT 1 FROM public.corpora_corpus_heads WHERE corpus_id=m.id))) THEN
        RETURN jsonb_build_object('status','conflict'); END IF;
    IF a.visibility<>p_visibility THEN
        UPDATE public.corpora_corpus_access SET visibility=p_visibility WHERE corpus_id=m.id;
        UPDATE public.corpora_corpus_metadata SET title=title WHERE id=m.id RETURNING * INTO m;
    END IF;
    PERFORM corpora_session_guard.lock_verified_session(p_actor,p_session,p_token_exp);
    RETURN jsonb_build_object('status','ok','corpus_id',m.id,'revision',m.revision,'visibility',p_visibility);
END;
$$;

CREATE FUNCTION corpora_session_guard.deletion_status(
    p_actor uuid,p_session uuid,p_token_exp bigint,p_corpus uuid,p_operation uuid
) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path='' SET lock_timeout='3s' AS $$
DECLARE result jsonb;
BEGIN
    PERFORM corpora_session_guard.lock_verified_session(p_actor,p_session,p_token_exp);
    SELECT jsonb_build_object('status','existing','corpus_id',m.id,'operation_id',d.id,'state',d.state)
    INTO result FROM public.corpora_corpus_metadata m JOIN public.corpora_corpus_deletions d ON d.corpus_id=m.id
    WHERE m.id=p_corpus AND d.id=p_operation AND (m.owner_id=p_actor OR EXISTS (
        SELECT 1 FROM corpora_session_guard.superadmins WHERE user_id=p_actor));
    RETURN coalesce(result,jsonb_build_object('status','not_found'));
END;
$$;

CREATE FUNCTION corpora_session_guard.begin_deletion(
    p_actor uuid,p_session uuid,p_token_exp bigint,p_corpus uuid,p_operation uuid,p_revision bigint
) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path='' SET lock_timeout='3s' AS $$
DECLARE m public.corpora_corpus_metadata%ROWTYPE; d public.corpora_corpus_deletions%ROWTYPE; claim uuid:=gen_random_uuid();
BEGIN
    PERFORM corpora_session_guard.lock_verified_session(p_actor,p_session,p_token_exp);
    IF p_operation IS NULL OR p_revision IS NULL OR p_revision<1 THEN RAISE EXCEPTION 'CORPUS_DELETE_INPUT_INVALID'; END IF;
    SELECT * INTO m FROM public.corpora_corpus_metadata WHERE id=p_corpus AND (
        owner_id=p_actor OR EXISTS(SELECT 1 FROM corpora_session_guard.superadmins WHERE user_id=p_actor)) FOR UPDATE;
    IF NOT FOUND THEN RETURN jsonb_build_object('status','not_found'); END IF;
    SELECT * INTO d FROM public.corpora_corpus_deletions WHERE id=p_operation;
    IF FOUND THEN
        IF d.corpus_id<>m.id OR d.actor_id<>p_actor OR d.expected_revision<>p_revision THEN
            RETURN jsonb_build_object('status','conflict'); END IF;
        RETURN corpora_session_guard.deletion_status(p_actor,p_session,p_token_exp,p_corpus,p_operation);
    END IF;
    IF m.revision<>p_revision OR NOT EXISTS(SELECT 1 FROM public.corpora_corpus_access
        WHERE corpus_id=m.id AND deleted_at IS NULL)
       OR NOT EXISTS(SELECT 1 FROM public.corpora_corpus_bootstrap WHERE corpus_id=m.id)
       OR EXISTS(SELECT 1 FROM public.corpora_corpus_provision_ops WHERE corpus_id=m.id AND state IN ('admitted','executing','uncertain'))
       OR EXISTS(SELECT 1 FROM public.corpora_corpus_write_ops WHERE corpus_id=m.id AND state IN ('admitted','executing','uncertain')) THEN
        RETURN jsonb_build_object('status','conflict'); END IF;
    INSERT INTO public.corpora_corpus_deletions(id,corpus_id,actor_id,expected_revision,worker_claim)
        VALUES(p_operation,m.id,p_actor,p_revision,claim) ON CONFLICT DO NOTHING;
    IF NOT FOUND THEN RETURN jsonb_build_object('status','conflict'); END IF;
    -- Stamp revision while still live, then hide every normal read before any remote deletion.
    UPDATE public.corpora_corpus_metadata SET title=title WHERE id=m.id;
    UPDATE public.corpora_corpus_access SET visibility='private',deleted_at=clock_timestamp() WHERE corpus_id=m.id;
    PERFORM corpora_session_guard.lock_verified_session(p_actor,p_session,p_token_exp);
    RETURN jsonb_build_object('status','claimed','corpus_id',m.id,'operation_id',p_operation,
        'claim_id',claim,'repo_id',m.hf_repo_id,'repo_type',m.hf_repo_type);
END;
$$;

CREATE FUNCTION corpora_session_guard.finish_deletion(p_operation uuid,p_claim uuid,p_completed boolean)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path='' SET lock_timeout='3s' AS $$
BEGIN
    IF p_completed IS NULL THEN RETURN false; END IF;
    PERFORM 1 FROM public.corpora_corpus_metadata WHERE id=(
        SELECT corpus_id FROM public.corpora_corpus_deletions WHERE id=p_operation) FOR UPDATE;
    UPDATE public.corpora_corpus_deletions SET state=CASE WHEN p_completed THEN 'completed' ELSE 'uncertain' END
        WHERE id=p_operation AND worker_claim=p_claim AND state IN ('executing','uncertain');
    RETURN FOUND OR EXISTS(SELECT 1 FROM public.corpora_corpus_deletions
        WHERE id=p_operation AND worker_claim=p_claim AND state='completed' AND p_completed);
END;
$$;

-- Preserve the existing contracts while excluding tombstones before admission or pagination.
CREATE OR REPLACE FUNCTION corpora_session_guard.get_metadata(
    p_actor uuid, p_session uuid, p_token_exp bigint, p_corpus uuid
) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = ''
SET lock_timeout = '3s' AS $$
DECLARE m public.corpora_corpus_metadata%ROWTYPE;
BEGIN
    PERFORM corpora_session_guard.lock_verified_session(p_actor,p_session,p_token_exp);
    SELECT * INTO m FROM public.corpora_corpus_metadata WHERE id=p_corpus AND EXISTS (SELECT 1 FROM public.corpora_corpus_access a WHERE a.corpus_id=p_corpus AND a.deleted_at IS NULL) AND (
        owner_id=p_actor OR EXISTS (
            SELECT 1 FROM corpora_session_guard.superadmins WHERE user_id=p_actor));
    PERFORM corpora_session_guard.lock_verified_session(p_actor,p_session,p_token_exp);
    IF m.id IS NULL THEN RETURN jsonb_build_object('status','not_found'); END IF;
    RETURN jsonb_build_object('status','ok','row',to_jsonb(m));
END;
$$;

CREATE OR REPLACE FUNCTION corpora_session_guard.list_metadata(
    p_actor uuid, p_session uuid, p_token_exp bigint,
    p_owner uuid, p_after uuid, p_limit integer
) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = ''
SET lock_timeout = '3s' AS $$
DECLARE rows jsonb;
BEGIN
    PERFORM corpora_session_guard.lock_verified_session(p_actor,p_session,p_token_exp);
    IF p_owner IS NULL OR p_limit IS NULL OR p_limit NOT BETWEEN 1 AND 100 THEN
        RAISE EXCEPTION 'CORPUS_METADATA_INPUT_INVALID';
    END IF;
    IF p_owner<>p_actor AND NOT EXISTS (
        SELECT 1 FROM corpora_session_guard.superadmins WHERE user_id=p_actor) THEN
        RETURN jsonb_build_object('status','not_found');
    END IF;
    SELECT coalesce(jsonb_agg(to_jsonb(summary) ORDER BY summary.id),'[]'::jsonb) INTO rows
    FROM (SELECT id,owner_id,title,language,state,revision,created_at,updated_at
          FROM public.corpora_corpus_metadata WHERE owner_id=p_owner AND EXISTS (SELECT 1 FROM public.corpora_corpus_access a WHERE a.corpus_id=corpora_corpus_metadata.id AND a.deleted_at IS NULL)
          AND (p_after IS NULL OR id>p_after) ORDER BY id LIMIT p_limit+1) summary;
    PERFORM corpora_session_guard.lock_verified_session(p_actor,p_session,p_token_exp);
    RETURN jsonb_build_object('status','list','rows',rows);
END;
$$;

CREATE OR REPLACE FUNCTION corpora_session_guard.patch_metadata(
    p_actor uuid, p_session uuid, p_token_exp bigint,
    p_corpus uuid, p_revision bigint, p_fields jsonb
) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = ''
SET lock_timeout = '3s' AS $$
DECLARE m public.corpora_corpus_metadata%ROWTYPE;
BEGIN
    PERFORM corpora_session_guard.lock_verified_session(p_actor,p_session,p_token_exp);
    IF p_revision IS NULL OR p_revision<1 OR p_revision>=9223372036854775807
       OR p_fields IS NULL OR jsonb_typeof(p_fields)<>'object' OR p_fields='{}'::jsonb
       OR p_fields - ARRAY['title','description','language','properties'] <> '{}'::jsonb
       OR (p_fields ? 'title' AND jsonb_typeof(p_fields->'title')<>'string')
       OR (p_fields ? 'description' AND jsonb_typeof(p_fields->'description') NOT IN ('null','string'))
       OR (p_fields ? 'language' AND jsonb_typeof(p_fields->'language') NOT IN ('null','string'))
       OR (p_fields ? 'properties' AND jsonb_typeof(p_fields->'properties')<>'object') THEN
        RAISE EXCEPTION 'CORPUS_METADATA_INPUT_INVALID';
    END IF;
    SELECT * INTO m FROM public.corpora_corpus_metadata WHERE id=p_corpus AND EXISTS (SELECT 1 FROM public.corpora_corpus_access a WHERE a.corpus_id=p_corpus AND a.deleted_at IS NULL) AND (
        owner_id=p_actor OR EXISTS (
            SELECT 1 FROM corpora_session_guard.superadmins WHERE user_id=p_actor)) FOR UPDATE;
    IF m.id IS NULL THEN RETURN jsonb_build_object('status','not_found'); END IF;
    IF m.revision<>p_revision THEN RETURN jsonb_build_object('status','conflict'); END IF;
    UPDATE public.corpora_corpus_metadata SET
        title=CASE WHEN p_fields ? 'title' THEN btrim(p_fields->>'title') ELSE title END,
        description=CASE WHEN p_fields ? 'description' THEN p_fields->>'description' ELSE description END,
        language=CASE WHEN p_fields ? 'language' THEN p_fields->>'language' ELSE language END,
        properties=CASE WHEN p_fields ? 'properties' THEN p_fields->'properties' ELSE properties END
        WHERE id=p_corpus RETURNING * INTO m;
    PERFORM corpora_session_guard.lock_verified_session(p_actor,p_session,p_token_exp);
    RETURN jsonb_build_object('status','ok','row',to_jsonb(m));
END;
$$;

CREATE OR REPLACE FUNCTION corpora_session_guard.creation_status(
    p_actor uuid, p_session uuid, p_token_exp bigint, p_corpus uuid
) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = ''
SET lock_timeout = '3s' AS $$
DECLARE
    m public.corpora_corpus_metadata%ROWTYPE;
    o public.corpora_corpus_provision_ops%ROWTYPE;
    b public.corpora_corpus_bootstrap%ROWTYPE;
    creation_state text := 'reserved';
BEGIN
    PERFORM corpora_session_guard.lock_verified_session(p_actor, p_session, p_token_exp);
    SELECT * INTO m FROM public.corpora_corpus_metadata WHERE id = p_corpus AND EXISTS (SELECT 1 FROM public.corpora_corpus_access a WHERE a.corpus_id=p_corpus AND a.deleted_at IS NULL) AND (
        owner_id = p_actor OR EXISTS (
            SELECT 1 FROM corpora_session_guard.superadmins WHERE user_id = p_actor
        )
    );
    IF NOT FOUND THEN
        RETURN pg_catalog.jsonb_build_object('status', 'not_found');
    END IF;
    SELECT * INTO o FROM public.corpora_corpus_provision_ops WHERE corpus_id = m.id;
    IF FOUND THEN
        IF o.repo_id <> m.hf_repo_id OR o.repo_type <> m.hf_repo_type THEN
            RAISE EXCEPTION 'CORPUS_CREATION_INVALID';
        END IF;
        CASE o.state
        WHEN 'completed' THEN
            SELECT * INTO b FROM public.corpora_corpus_bootstrap
                WHERE corpus_id = m.id AND operation_id = o.id AND worker_claim = o.worker_claim;
            IF NOT FOUND THEN RAISE EXCEPTION 'CORPUS_CREATION_INVALID'; END IF;
            creation_state := 'repository_created';
        WHEN 'uncertain' THEN creation_state := 'needs_reconciliation';
        ELSE creation_state := 'provisioning';
        END CASE;
    END IF;
    PERFORM corpora_session_guard.lock_verified_session(p_actor, p_session, p_token_exp);
    RETURN pg_catalog.jsonb_build_object(
        'status', 'existing', 'id', m.id, 'title', coalesce(o.creation_title,m.title),
        'state', creation_state, 'bootstrap_sha', b.commit_sha
    );
END;
$$;

CREATE OR REPLACE FUNCTION corpora_session_guard.begin_creation(
    p_actor uuid, p_session uuid, p_token_exp bigint, p_corpus uuid,
    p_title text, p_organization text
) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = ''
SET lock_timeout = '3s' AS $$
DECLARE
    m public.corpora_corpus_metadata%ROWTYPE;
    snapshot jsonb;
    admission jsonb;
    repo text;
BEGIN
    PERFORM corpora_session_guard.lock_verified_session(p_actor, p_session, p_token_exp);
    IF p_corpus IS NULL OR p_title IS NULL OR length(btrim(p_title)) NOT BETWEEN 1 AND 512
       OR p_organization IS NULL
       OR p_organization !~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$' THEN
        RAISE EXCEPTION 'CORPUS_CREATION_INPUT_INVALID';
    END IF;
    repo := p_organization || '/corpus-' || p_corpus::text;
    INSERT INTO public.corpora_corpus_metadata (id,owner_id,title,hf_repo_id,hf_repo_type)
        VALUES (p_corpus,p_actor,btrim(p_title),repo,'dataset') ON CONFLICT DO NOTHING;
    SELECT * INTO m FROM public.corpora_corpus_metadata WHERE id = p_corpus AND EXISTS (SELECT 1 FROM public.corpora_corpus_access a WHERE a.corpus_id=p_corpus AND a.deleted_at IS NULL) FOR UPDATE;
    IF NOT FOUND OR m.owner_id <> p_actor OR coalesce((SELECT creation_title FROM public.corpora_corpus_provision_ops WHERE corpus_id=m.id),m.title) <> btrim(p_title)
       OR m.hf_repo_id <> repo OR m.hf_repo_type <> 'dataset' THEN
        RETURN pg_catalog.jsonb_build_object('status', 'conflict');
    END IF;
    snapshot := corpora_session_guard.creation_status(p_actor,p_session,p_token_exp,p_corpus);
    IF snapshot->>'state' <> 'reserved' THEN RETURN snapshot; END IF;
    admission := corpora_session_guard.admit_provision(p_actor,p_session,p_token_exp,p_corpus);
    IF admission->>'status' <> 'ok' THEN
        RAISE EXCEPTION 'CORPUS_CREATION_INVALID';
    END IF;
    PERFORM corpora_session_guard.lock_verified_session(p_actor,p_session,p_token_exp);
    RETURN admission || pg_catalog.jsonb_build_object('status','claimed','id',m.id,'title',m.title);
END;
$$;

CREATE OR REPLACE FUNCTION corpora_session_guard.import_status(
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
    WHERE m.id=p_corpus AND EXISTS (SELECT 1 FROM public.corpora_corpus_access a WHERE a.corpus_id=m.id AND a.deleted_at IS NULL) AND (m.owner_id=p_actor OR EXISTS (
        SELECT 1 FROM corpora_session_guard.superadmins WHERE user_id=p_actor));
    PERFORM corpora_session_guard.lock_verified_session(p_actor,p_session,p_token_exp);
    RETURN coalesce(result,pg_catalog.jsonb_build_object('status','not_found'));
END;
$$;

CREATE OR REPLACE FUNCTION corpora_session_guard.begin_import(
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
    SELECT * INTO m FROM public.corpora_corpus_metadata WHERE id=p_corpus AND EXISTS (SELECT 1 FROM public.corpora_corpus_access a WHERE a.corpus_id=p_corpus AND a.deleted_at IS NULL) AND (
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

CREATE OR REPLACE FUNCTION corpora_session_guard.admit_provision(
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
        WHERE id = p_corpus AND EXISTS (SELECT 1 FROM public.corpora_corpus_access a WHERE a.corpus_id=p_corpus AND a.deleted_at IS NULL) AND (
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

CREATE FUNCTION public.corpora_read_corpus_access(p_actor uuid,p_session uuid,p_token_exp bigint,p_corpus uuid)
RETURNS jsonb LANGUAGE sql SECURITY INVOKER SET search_path='' AS $$
 SELECT corpora_session_guard.read_access(p_actor,p_session,p_token_exp,p_corpus);
$$;

ALTER FUNCTION public.corpora_read_corpus_access(uuid,uuid,bigint,uuid) OWNER TO postgres;
REVOKE ALL ON FUNCTION public.corpora_read_corpus_access(uuid,uuid,bigint,uuid) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION public.corpora_read_corpus_access(uuid,uuid,bigint,uuid) TO service_role;

ALTER FUNCTION corpora_session_guard.read_access(uuid,uuid,bigint,uuid) OWNER TO postgres;
REVOKE ALL ON FUNCTION corpora_session_guard.read_access(uuid,uuid,bigint,uuid) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION corpora_session_guard.read_access(uuid,uuid,bigint,uuid) TO service_role;

CREATE FUNCTION public.corpora_set_corpus_visibility(p_actor uuid,p_session uuid,p_token_exp bigint,p_corpus uuid,p_revision bigint,p_visibility text)
RETURNS jsonb LANGUAGE sql SECURITY INVOKER SET search_path='' AS $$
 SELECT corpora_session_guard.set_visibility(p_actor,p_session,p_token_exp,p_corpus,p_revision,p_visibility);
$$;

ALTER FUNCTION public.corpora_set_corpus_visibility(uuid,uuid,bigint,uuid,bigint,text) OWNER TO postgres;
REVOKE ALL ON FUNCTION public.corpora_set_corpus_visibility(uuid,uuid,bigint,uuid,bigint,text) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION public.corpora_set_corpus_visibility(uuid,uuid,bigint,uuid,bigint,text) TO service_role;

ALTER FUNCTION corpora_session_guard.set_visibility(uuid,uuid,bigint,uuid,bigint,text) OWNER TO postgres;
REVOKE ALL ON FUNCTION corpora_session_guard.set_visibility(uuid,uuid,bigint,uuid,bigint,text) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION corpora_session_guard.set_visibility(uuid,uuid,bigint,uuid,bigint,text) TO service_role;

CREATE FUNCTION public.corpora_get_corpus_deletion(p_actor uuid,p_session uuid,p_token_exp bigint,p_corpus uuid,p_operation uuid)
RETURNS jsonb LANGUAGE sql SECURITY INVOKER SET search_path='' AS $$
 SELECT corpora_session_guard.deletion_status(p_actor,p_session,p_token_exp,p_corpus,p_operation);
$$;

ALTER FUNCTION public.corpora_get_corpus_deletion(uuid,uuid,bigint,uuid,uuid) OWNER TO postgres;
REVOKE ALL ON FUNCTION public.corpora_get_corpus_deletion(uuid,uuid,bigint,uuid,uuid) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION public.corpora_get_corpus_deletion(uuid,uuid,bigint,uuid,uuid) TO service_role;

ALTER FUNCTION corpora_session_guard.deletion_status(uuid,uuid,bigint,uuid,uuid) OWNER TO postgres;
REVOKE ALL ON FUNCTION corpora_session_guard.deletion_status(uuid,uuid,bigint,uuid,uuid) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION corpora_session_guard.deletion_status(uuid,uuid,bigint,uuid,uuid) TO service_role;

CREATE FUNCTION public.corpora_begin_corpus_deletion(p_actor uuid,p_session uuid,p_token_exp bigint,p_corpus uuid,p_operation uuid,p_revision bigint)
RETURNS jsonb LANGUAGE sql SECURITY INVOKER SET search_path='' AS $$
 SELECT corpora_session_guard.begin_deletion(p_actor,p_session,p_token_exp,p_corpus,p_operation,p_revision);
$$;

ALTER FUNCTION public.corpora_begin_corpus_deletion(uuid,uuid,bigint,uuid,uuid,bigint) OWNER TO postgres;
REVOKE ALL ON FUNCTION public.corpora_begin_corpus_deletion(uuid,uuid,bigint,uuid,uuid,bigint) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION public.corpora_begin_corpus_deletion(uuid,uuid,bigint,uuid,uuid,bigint) TO service_role;

ALTER FUNCTION corpora_session_guard.begin_deletion(uuid,uuid,bigint,uuid,uuid,bigint) OWNER TO postgres;
REVOKE ALL ON FUNCTION corpora_session_guard.begin_deletion(uuid,uuid,bigint,uuid,uuid,bigint) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION corpora_session_guard.begin_deletion(uuid,uuid,bigint,uuid,uuid,bigint) TO service_role;

CREATE FUNCTION public.corpora_finish_corpus_deletion(p_operation uuid,p_claim uuid,p_completed boolean)
RETURNS boolean LANGUAGE sql SECURITY INVOKER SET search_path='' AS $$
 SELECT corpora_session_guard.finish_deletion(p_operation,p_claim,p_completed);
$$;

ALTER FUNCTION public.corpora_finish_corpus_deletion(uuid,uuid,boolean) OWNER TO postgres;
REVOKE ALL ON FUNCTION public.corpora_finish_corpus_deletion(uuid,uuid,boolean) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION public.corpora_finish_corpus_deletion(uuid,uuid,boolean) TO service_role;

ALTER FUNCTION corpora_session_guard.finish_deletion(uuid,uuid,boolean) OWNER TO postgres;
REVOKE ALL ON FUNCTION corpora_session_guard.finish_deletion(uuid,uuid,boolean) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION corpora_session_guard.finish_deletion(uuid,uuid,boolean) TO service_role;

ALTER FUNCTION corpora_session_guard.initialize_access() OWNER TO postgres;
REVOKE ALL ON FUNCTION corpora_session_guard.initialize_access() FROM PUBLIC,anon,authenticated,service_role;

ALTER FUNCTION corpora_session_guard.guard_access() OWNER TO postgres;
REVOKE ALL ON FUNCTION corpora_session_guard.guard_access() FROM PUBLIC,anon,authenticated,service_role;

ALTER FUNCTION corpora_session_guard.require_live_corpus() OWNER TO postgres;
REVOKE ALL ON FUNCTION corpora_session_guard.require_live_corpus() FROM PUBLIC,anon,authenticated,service_role;

ALTER FUNCTION corpora_session_guard.guard_deletion() OWNER TO postgres;
REVOKE ALL ON FUNCTION corpora_session_guard.guard_deletion() FROM PUBLIC,anon,authenticated,service_role;

NOTIFY pgrst,'reload schema';
