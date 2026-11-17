-- The API verifies JWT signatures; only the server role can supply claims here.
CREATE FUNCTION corpora_session_guard.get_metadata(
    p_actor uuid, p_session uuid, p_token_exp bigint, p_corpus uuid
) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = ''
SET lock_timeout = '3s' AS $$
DECLARE m public.corpora_corpus_metadata%ROWTYPE;
BEGIN
    PERFORM corpora_session_guard.lock_verified_session(p_actor,p_session,p_token_exp);
    SELECT * INTO m FROM public.corpora_corpus_metadata WHERE id=p_corpus AND (
        owner_id=p_actor OR EXISTS (
            SELECT 1 FROM corpora_session_guard.superadmins WHERE user_id=p_actor));
    PERFORM corpora_session_guard.lock_verified_session(p_actor,p_session,p_token_exp);
    IF m.id IS NULL THEN RETURN jsonb_build_object('status','not_found'); END IF;
    RETURN jsonb_build_object('status','ok','row',to_jsonb(m));
END;
$$;

CREATE FUNCTION corpora_session_guard.list_metadata(
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
          FROM public.corpora_corpus_metadata WHERE owner_id=p_owner
          AND (p_after IS NULL OR id>p_after) ORDER BY id LIMIT p_limit+1) summary;
    PERFORM corpora_session_guard.lock_verified_session(p_actor,p_session,p_token_exp);
    RETURN jsonb_build_object('status','list','rows',rows);
END;
$$;

CREATE FUNCTION corpora_session_guard.patch_metadata(
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
    SELECT * INTO m FROM public.corpora_corpus_metadata WHERE id=p_corpus AND (
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

ALTER FUNCTION corpora_session_guard.get_metadata(uuid,uuid,bigint,uuid) OWNER TO postgres;
ALTER FUNCTION corpora_session_guard.list_metadata(uuid,uuid,bigint,uuid,uuid,integer) OWNER TO postgres;
ALTER FUNCTION corpora_session_guard.patch_metadata(uuid,uuid,bigint,uuid,bigint,jsonb) OWNER TO postgres;
REVOKE ALL ON FUNCTION corpora_session_guard.get_metadata(uuid,uuid,bigint,uuid) FROM PUBLIC,anon,authenticated;
REVOKE ALL ON FUNCTION corpora_session_guard.list_metadata(uuid,uuid,bigint,uuid,uuid,integer) FROM PUBLIC,anon,authenticated;
REVOKE ALL ON FUNCTION corpora_session_guard.patch_metadata(uuid,uuid,bigint,uuid,bigint,jsonb) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION corpora_session_guard.get_metadata(uuid,uuid,bigint,uuid) TO service_role;
GRANT EXECUTE ON FUNCTION corpora_session_guard.list_metadata(uuid,uuid,bigint,uuid,uuid,integer) TO service_role;
GRANT EXECUTE ON FUNCTION corpora_session_guard.patch_metadata(uuid,uuid,bigint,uuid,bigint,jsonb) TO service_role;

CREATE FUNCTION public.corpora_get_metadata(
    p_actor uuid,p_session uuid,p_token_exp bigint,p_corpus uuid
) RETURNS jsonb LANGUAGE sql SECURITY INVOKER SET search_path = '' AS $$
    SELECT corpora_session_guard.get_metadata(p_actor,p_session,p_token_exp,p_corpus);
$$;
CREATE FUNCTION public.corpora_list_metadata(
    p_actor uuid,p_session uuid,p_token_exp bigint,p_owner uuid,p_after uuid,p_limit integer
) RETURNS jsonb LANGUAGE sql SECURITY INVOKER SET search_path = '' AS $$
    SELECT corpora_session_guard.list_metadata(p_actor,p_session,p_token_exp,p_owner,p_after,p_limit);
$$;
CREATE FUNCTION public.corpora_patch_metadata(
    p_actor uuid,p_session uuid,p_token_exp bigint,p_corpus uuid,p_revision bigint,p_fields jsonb
) RETURNS jsonb LANGUAGE sql SECURITY INVOKER SET search_path = '' AS $$
    SELECT corpora_session_guard.patch_metadata(p_actor,p_session,p_token_exp,p_corpus,p_revision,p_fields);
$$;
REVOKE ALL ON FUNCTION public.corpora_get_metadata(uuid,uuid,bigint,uuid) FROM PUBLIC,anon,authenticated;
REVOKE ALL ON FUNCTION public.corpora_list_metadata(uuid,uuid,bigint,uuid,uuid,integer) FROM PUBLIC,anon,authenticated;
REVOKE ALL ON FUNCTION public.corpora_patch_metadata(uuid,uuid,bigint,uuid,bigint,jsonb) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION public.corpora_get_metadata(uuid,uuid,bigint,uuid) TO service_role;
GRANT EXECUTE ON FUNCTION public.corpora_list_metadata(uuid,uuid,bigint,uuid,uuid,integer) TO service_role;
GRANT EXECUTE ON FUNCTION public.corpora_patch_metadata(uuid,uuid,bigint,uuid,bigint,jsonb) TO service_role;

-- All exposed metadata mutations now go through a live-session transaction.
REVOKE INSERT (id,owner_id,title,hf_repo_id,hf_repo_type),
    UPDATE (title,description,language,properties)
    ON public.corpora_corpus_metadata FROM service_role;

-- Title edits must not change the original creation request's retry identity.
ALTER TABLE public.corpora_corpus_provision_ops ADD COLUMN creation_title text;
UPDATE public.corpora_corpus_provision_ops o SET creation_title=m.title
    FROM public.corpora_corpus_metadata m WHERE m.id=o.corpus_id;
ALTER TABLE public.corpora_corpus_provision_ops ALTER COLUMN creation_title SET NOT NULL;
ALTER TABLE public.corpora_corpus_provision_ops ADD CONSTRAINT corpora_creation_title_length
    CHECK (length(btrim(creation_title)) BETWEEN 1 AND 512);
CREATE FUNCTION public.corpora_corpus_creation_input()
RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER SET search_path = '' AS $$
BEGIN
    IF TG_OP='INSERT' THEN
        SELECT title INTO NEW.creation_title FROM public.corpora_corpus_metadata WHERE id=NEW.corpus_id;
    ELSIF NEW.creation_title IS DISTINCT FROM OLD.creation_title THEN
        RAISE EXCEPTION 'CORPUS_CREATION_INPUT_IMMUTABLE';
    END IF;
    RETURN NEW;
END;
$$;
REVOKE ALL ON FUNCTION public.corpora_corpus_creation_input() FROM PUBLIC,anon,authenticated;
CREATE TRIGGER corpora_corpus_creation_input BEFORE INSERT OR UPDATE
    ON public.corpora_corpus_provision_ops FOR EACH ROW
    EXECUTE FUNCTION public.corpora_corpus_creation_input();

-- Only the trusted API may supply verified JWT claims. Browser roles cannot
-- call these RPCs or obtain worker claims. All privileged SQL stays private.
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
    SELECT * INTO m FROM public.corpora_corpus_metadata WHERE id = p_corpus AND (
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
    SELECT * INTO m FROM public.corpora_corpus_metadata WHERE id = p_corpus FOR UPDATE;
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


NOTIFY pgrst, 'reload schema';
