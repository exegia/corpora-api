-- Fresh-project baseline captured from the reviewed hosted schema on 2026-11-18.
-- Contains schema only; it must never be applied to an existing project.
-- Later API migrations are applied separately in timestamp order.
BEGIN;
DO $$
BEGIN
    IF to_regnamespace('corpora_session_guard') IS NOT NULL
       OR EXISTS (SELECT 1 FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n
           ON n.oid=c.relnamespace WHERE n.nspname='public' AND c.relkind IN ('r','p','v','m','f','S'))
       OR EXISTS (SELECT 1 FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace n
           ON n.oid=p.pronamespace WHERE n.nspname='public')
       OR EXISTS (SELECT 1 FROM auth.users)
       OR EXISTS (SELECT 1 FROM storage.buckets) THEN
        RAISE EXCEPTION 'FRESH_BOOTSTRAP_REQUIRES_EMPTY_PROJECT';
    END IF;
END;
$$;
-- A fresh Supabase instance grants extra defaults during CREATE. Clear those
-- before restoring explicit object grants; the dump restores final defaults.
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public REVOKE ALL ON TABLES FROM anon,authenticated,service_role;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public REVOKE ALL ON SEQUENCES FROM anon,authenticated,service_role;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public REVOKE ALL ON FUNCTIONS FROM anon,authenticated,service_role;




SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;


CREATE SCHEMA IF NOT EXISTS "corpora_session_guard";


ALTER SCHEMA "corpora_session_guard" OWNER TO "postgres";


CREATE SCHEMA IF NOT EXISTS "public";


ALTER SCHEMA "public" OWNER TO "pg_database_owner";


COMMENT ON SCHEMA "public" IS 'standard public schema';



CREATE TYPE "public"."book_type" AS ENUM (
    'bible',
    'commentary',
    'lexicon',
    'biography',
    'review',
    'manuscript',
    'tanakh',
    'quran',
    'apocrypha'
);


ALTER TYPE "public"."book_type" OWNER TO "postgres";


CREATE TYPE "public"."category_type" AS ENUM (
    'biblical',
    'religious',
    'literary',
    'historical',
    'paratext'
);


ALTER TYPE "public"."category_type" OWNER TO "postgres";


CREATE TYPE "public"."format_type" AS ENUM (
    'application/xml',
    'application/json',
    'text/html',
    'text/plain',
    'application/pdf',
    'application/tei+xml',
    'application/tf+xml',
    'application/cfm+xml',
    'application/epub+xml'
);


ALTER TYPE "public"."format_type" OWNER TO "postgres";


CREATE TYPE "public"."language_type" AS ENUM (
    'hebrew',
    'greek',
    'syriac',
    'arabic',
    'aramaic',
    'proto-cuneiform',
    'akkadian',
    'ugaritic',
    'pali',
    'latin',
    'dutch',
    'french',
    'italian',
    'english'
);


ALTER TYPE "public"."language_type" OWNER TO "postgres";


CREATE TYPE "public"."licence_conformance" AS ENUM (
    'not reviewed',
    'approved',
    'rejected'
);


ALTER TYPE "public"."licence_conformance" OWNER TO "postgres";


CREATE TYPE "public"."licence_status" AS ENUM (
    'active',
    'retired',
    'superseded'
);


ALTER TYPE "public"."licence_status" OWNER TO "postgres";


CREATE TYPE "public"."logs_category" AS ENUM (
    'auth',
    'database',
    'storage',
    'docker',
    'mcp',
    'other'
);


ALTER TYPE "public"."logs_category" OWNER TO "postgres";


CREATE TYPE "public"."logs_level" AS ENUM (
    'debug',
    'info',
    'warning',
    'error'
);


ALTER TYPE "public"."logs_level" OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "corpora_session_guard"."admit_provision"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid") RETURNS "jsonb"
    LANGUAGE "plpgsql" SECURITY DEFINER
    SET "search_path" TO ''
    AS $$
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


ALTER FUNCTION "corpora_session_guard"."admit_provision"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid") OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "corpora_session_guard"."begin_corpus_edit"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_operation" "uuid", "p_sequence" bigint, "p_parent" "text", "p_digest" "text", "p_size" bigint, "p_intent" "text", "p_kind" "text", "p_restore" bigint) RETURNS "jsonb"
    LANGUAGE "plpgsql" SECURITY DEFINER
    SET "search_path" TO ''
    SET "lock_timeout" TO '3s'
    AS $$
DECLARE status jsonb; result jsonb;
BEGIN
    IF p_kind IS NULL OR p_kind NOT IN ('manifest','node','restore')
       OR ((p_kind='restore') IS DISTINCT FROM (p_restore IS NOT NULL))
       OR (p_restore IS NOT NULL AND p_restore<1) THEN
        RAISE EXCEPTION 'CORPUS_EDIT_INPUT_INVALID';
    END IF;
    -- Locks the authorized metadata row before inspecting or claiming an intent.
    status := corpora_session_guard.edit_intent_status(
        p_actor,p_session,p_token_exp,p_corpus,p_operation,p_intent);
    IF status->>'status'<>'absent' THEN RETURN status; END IF;
    IF p_kind='restore' AND NOT EXISTS(SELECT 1 FROM public.corpora_corpus_versions
        WHERE corpus_id=p_corpus AND sequence=p_restore) THEN
        RETURN pg_catalog.jsonb_build_object('status','not_found');
    END IF;
    result := corpora_session_guard.begin_version_save(
        p_actor,p_session,p_token_exp,p_corpus,p_operation,p_sequence,p_parent,p_digest,p_size);
    IF result->>'status'='claimed' THEN
        INSERT INTO public.corpora_corpus_version_intents
            (operation_id,corpus_id,intent_sha256,kind,restore_sequence)
            VALUES(p_operation,p_corpus,p_intent,p_kind,p_restore);
    END IF;
    RETURN result;
END;
$$;


ALTER FUNCTION "corpora_session_guard"."begin_corpus_edit"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_operation" "uuid", "p_sequence" bigint, "p_parent" "text", "p_digest" "text", "p_size" bigint, "p_intent" "text", "p_kind" "text", "p_restore" bigint) OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "corpora_session_guard"."begin_creation"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_title" "text", "p_organization" "text") RETURNS "jsonb"
    LANGUAGE "plpgsql" SECURITY DEFINER
    SET "search_path" TO ''
    SET "lock_timeout" TO '3s'
    AS $_$
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
$_$;


ALTER FUNCTION "corpora_session_guard"."begin_creation"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_title" "text", "p_organization" "text") OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "corpora_session_guard"."begin_deletion"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_operation" "uuid", "p_revision" bigint) RETURNS "jsonb"
    LANGUAGE "plpgsql" SECURITY DEFINER
    SET "search_path" TO ''
    SET "lock_timeout" TO '3s'
    AS $$
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


ALTER FUNCTION "corpora_session_guard"."begin_deletion"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_operation" "uuid", "p_revision" bigint) OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "corpora_session_guard"."begin_import"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_operation" "uuid", "p_revision" bigint, "p_digest" "text", "p_size" bigint) RETURNS "jsonb"
    LANGUAGE "plpgsql" SECURITY DEFINER
    SET "search_path" TO ''
    SET "lock_timeout" TO '3s'
    AS $_$
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
$_$;


ALTER FUNCTION "corpora_session_guard"."begin_import"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_operation" "uuid", "p_revision" bigint, "p_digest" "text", "p_size" bigint) OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "corpora_session_guard"."begin_uncertain_discard"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_operation" "uuid", "p_revision" bigint, "p_observed_sha" "text") RETURNS "jsonb"
    LANGUAGE "plpgsql" SECURITY DEFINER
    SET "search_path" TO ''
    SET "lock_timeout" TO '3s'
    AS $_$
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
$_$;


ALTER FUNCTION "corpora_session_guard"."begin_uncertain_discard"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_operation" "uuid", "p_revision" bigint, "p_observed_sha" "text") OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "corpora_session_guard"."begin_version_save"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_operation" "uuid", "p_sequence" bigint, "p_parent" "text", "p_digest" "text", "p_size" bigint) RETURNS "jsonb"
    LANGUAGE "plpgsql" SECURITY DEFINER
    SET "search_path" TO ''
    SET "lock_timeout" TO '3s'
    AS $_$
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
$_$;


ALTER FUNCTION "corpora_session_guard"."begin_version_save"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_operation" "uuid", "p_sequence" bigint, "p_parent" "text", "p_digest" "text", "p_size" bigint) OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "corpora_session_guard"."complete_import"("p_operation" "uuid", "p_claim" "uuid", "p_publication" "uuid", "p_sha" "text") RETURNS boolean
    LANGUAGE "plpgsql" SECURITY DEFINER
    SET "search_path" TO ''
    SET "lock_timeout" TO '3s'
    AS $_$
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
$_$;


ALTER FUNCTION "corpora_session_guard"."complete_import"("p_operation" "uuid", "p_claim" "uuid", "p_publication" "uuid", "p_sha" "text") OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "corpora_session_guard"."complete_version_save"("p_operation" "uuid", "p_claim" "uuid", "p_publication" "uuid", "p_sha" "text") RETURNS boolean
    LANGUAGE "plpgsql" SECURITY DEFINER
    SET "search_path" TO ''
    SET "lock_timeout" TO '3s'
    AS $_$
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
$_$;


ALTER FUNCTION "corpora_session_guard"."complete_version_save"("p_operation" "uuid", "p_claim" "uuid", "p_publication" "uuid", "p_sha" "text") OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "corpora_session_guard"."corpus_reader_add_email"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_email" "text") RETURNS "jsonb"
    LANGUAGE "plpgsql" SECURITY DEFINER
    SET "search_path" TO ''
    SET "lock_timeout" TO '3s'
    AS $$
DECLARE m public.corpora_corpus_metadata%ROWTYPE; reader_id uuid; matches bigint;
BEGIN
    PERFORM corpora_session_guard.lock_verified_session(p_actor,p_session,p_token_exp);
    IF p_corpus IS NULL OR p_email IS NULL OR length(p_email)>254 OR pg_catalog.btrim(p_email)='' THEN
        RAISE EXCEPTION 'CORPUS_READER_INPUT_INVALID';
    END IF;
    SELECT * INTO m FROM public.corpora_corpus_metadata WHERE id=p_corpus FOR UPDATE;
    IF NOT FOUND OR (m.owner_id IS DISTINCT FROM p_actor AND NOT EXISTS(
        SELECT 1 FROM corpora_session_guard.superadmins WHERE user_id=p_actor))
       OR NOT EXISTS(SELECT 1 FROM public.corpora_corpus_access WHERE corpus_id=m.id AND deleted_at IS NULL)
    THEN RETURN pg_catalog.jsonb_build_object('status','not_found'); END IF;
    SELECT count(*),(pg_catalog.array_agg(id))[1] INTO matches,reader_id FROM auth.users
        WHERE pg_catalog.lower(email)=pg_catalog.lower(pg_catalog.btrim(p_email))
          AND email_confirmed_at IS NOT NULL AND deleted_at IS NULL;
    IF matches<>1 THEN RETURN pg_catalog.jsonb_build_object('status','reader_not_found'); END IF;
    RETURN corpora_session_guard.corpus_reader_set(p_actor,p_session,p_token_exp,p_corpus,reader_id,true);
END;
$$;


ALTER FUNCTION "corpora_session_guard"."corpus_reader_add_email"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_email" "text") OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "corpora_session_guard"."corpus_reader_set"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_reader" "uuid", "p_present" boolean) RETURNS "jsonb"
    LANGUAGE "plpgsql" SECURITY DEFINER
    SET "search_path" TO ''
    SET "lock_timeout" TO '3s'
    AS $$
DECLARE m public.corpora_corpus_metadata%ROWTYPE;
BEGIN
    PERFORM corpora_session_guard.lock_verified_session(p_actor,p_session,p_token_exp);
    IF p_corpus IS NULL OR p_reader IS NULL OR p_present IS NULL THEN
        RAISE EXCEPTION 'CORPUS_READER_INPUT_INVALID';
    END IF;
    SELECT * INTO m FROM public.corpora_corpus_metadata WHERE id=p_corpus FOR UPDATE;
    IF NOT FOUND OR (m.owner_id IS DISTINCT FROM p_actor AND NOT EXISTS(
        SELECT 1 FROM corpora_session_guard.superadmins WHERE user_id=p_actor))
       OR NOT EXISTS(SELECT 1 FROM public.corpora_corpus_access WHERE corpus_id=m.id AND deleted_at IS NULL)
    THEN RETURN pg_catalog.jsonb_build_object('status','not_found'); END IF;
    IF p_present AND NOT EXISTS(SELECT 1 FROM auth.users WHERE id=p_reader AND deleted_at IS NULL
        AND (email_confirmed_at IS NOT NULL OR phone_confirmed_at IS NOT NULL)) THEN
        RETURN pg_catalog.jsonb_build_object('status','reader_not_found');
    END IF;
    IF p_present THEN
        INSERT INTO public.corpora_corpus_readers(corpus_id,user_id,added_by)
            VALUES(p_corpus,p_reader,p_actor) ON CONFLICT DO NOTHING;
    ELSE
        DELETE FROM public.corpora_corpus_readers WHERE corpus_id=p_corpus AND user_id=p_reader;
    END IF;
    PERFORM corpora_session_guard.lock_verified_session(p_actor,p_session,p_token_exp);
    RETURN pg_catalog.jsonb_build_object('status','ok','corpus_id',p_corpus,
        'user_id',p_reader,'present',p_present);
END;
$$;


ALTER FUNCTION "corpora_session_guard"."corpus_reader_set"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_reader" "uuid", "p_present" boolean) OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "corpora_session_guard"."corpus_readers_list"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_after" "uuid", "p_limit" integer) RETURNS "jsonb"
    LANGUAGE "plpgsql" SECURITY DEFINER
    SET "search_path" TO ''
    SET "lock_timeout" TO '3s'
    AS $$
DECLARE m public.corpora_corpus_metadata%ROWTYPE; readers jsonb;
BEGIN
    PERFORM corpora_session_guard.lock_verified_session(p_actor,p_session,p_token_exp);
    IF p_corpus IS NULL OR p_limit IS NULL OR p_limit NOT BETWEEN 1 AND 100 THEN
        RAISE EXCEPTION 'CORPUS_READERS_INPUT_INVALID';
    END IF;
    SELECT * INTO m FROM public.corpora_corpus_metadata WHERE id=p_corpus FOR SHARE;
    IF NOT FOUND OR (m.owner_id IS DISTINCT FROM p_actor AND NOT EXISTS(
        SELECT 1 FROM corpora_session_guard.superadmins WHERE user_id=p_actor))
       OR NOT EXISTS(SELECT 1 FROM public.corpora_corpus_access WHERE corpus_id=m.id AND deleted_at IS NULL)
    THEN RETURN pg_catalog.jsonb_build_object('status','not_found'); END IF;
    SELECT coalesce(pg_catalog.jsonb_agg(pg_catalog.to_jsonb(item) ORDER BY item.user_id),'[]'::jsonb)
        INTO readers FROM (SELECT user_id,created_at FROM public.corpora_corpus_readers
            WHERE corpus_id=p_corpus AND (p_after IS NULL OR user_id>p_after)
            ORDER BY user_id LIMIT p_limit+1) item;
    PERFORM corpora_session_guard.lock_verified_session(p_actor,p_session,p_token_exp);
    RETURN pg_catalog.jsonb_build_object('status','ok','corpus_id',p_corpus,'readers',readers);
END;
$$;


ALTER FUNCTION "corpora_session_guard"."corpus_readers_list"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_after" "uuid", "p_limit" integer) OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "corpora_session_guard"."creation_status"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid") RETURNS "jsonb"
    LANGUAGE "plpgsql" SECURITY DEFINER
    SET "search_path" TO ''
    SET "lock_timeout" TO '3s'
    AS $$
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


ALTER FUNCTION "corpora_session_guard"."creation_status"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid") OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "corpora_session_guard"."deletion_status"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_operation" "uuid") RETURNS "jsonb"
    LANGUAGE "plpgsql" SECURITY DEFINER
    SET "search_path" TO ''
    SET "lock_timeout" TO '3s'
    AS $$
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


ALTER FUNCTION "corpora_session_guard"."deletion_status"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_operation" "uuid") OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "corpora_session_guard"."edit_intent_status"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_operation" "uuid", "p_intent" "text") RETURNS "jsonb"
    LANGUAGE "plpgsql" SECURITY DEFINER
    SET "search_path" TO ''
    SET "lock_timeout" TO '3s'
    AS $_$
DECLARE m public.corpora_corpus_metadata%ROWTYPE; o public.corpora_corpus_write_ops%ROWTYPE;
        i public.corpora_corpus_version_intents%ROWTYPE;
BEGIN
    PERFORM corpora_session_guard.lock_verified_session(p_actor,p_session,p_token_exp);
    IF p_corpus IS NULL OR p_operation IS NULL OR p_intent IS NULL
        OR p_intent !~ '^[0-9a-f]{64}$' THEN RAISE EXCEPTION 'CORPUS_EDIT_INPUT_INVALID'; END IF;
    SELECT * INTO m FROM public.corpora_corpus_metadata WHERE id=p_corpus AND (
        owner_id=p_actor OR EXISTS(SELECT 1 FROM corpora_session_guard.superadmins WHERE user_id=p_actor)
    ) FOR UPDATE;
    IF NOT FOUND OR NOT EXISTS(SELECT 1 FROM public.corpora_corpus_access
        WHERE corpus_id=p_corpus AND deleted_at IS NULL) THEN
        RETURN pg_catalog.jsonb_build_object('status','not_found');
    END IF;
    SELECT * INTO o FROM public.corpora_corpus_write_ops WHERE id=p_operation;
    IF NOT FOUND THEN RETURN pg_catalog.jsonb_build_object('status','absent'); END IF;
    SELECT * INTO i FROM public.corpora_corpus_version_intents WHERE operation_id=p_operation;
    IF NOT FOUND OR o.corpus_id<>m.id OR o.actor_id<>p_actor OR i.corpus_id<>m.id
       OR i.intent_sha256<>p_intent THEN
        RETURN pg_catalog.jsonb_build_object('status','conflict');
    END IF;
    RETURN corpora_session_guard.version_save_status(p_actor,p_session,p_token_exp,p_corpus,p_operation);
END;
$_$;


ALTER FUNCTION "corpora_session_guard"."edit_intent_status"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_operation" "uuid", "p_intent" "text") OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "corpora_session_guard"."finalize_provision"("p_corpus" "uuid", "p_operation" "uuid", "p_claim" "uuid", "p_repo" "text", "p_type" "text", "p_sha" "text") RETURNS boolean
    LANGUAGE "plpgsql" SECURITY DEFINER
    SET "search_path" TO ''
    AS $_$
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
$_$;


ALTER FUNCTION "corpora_session_guard"."finalize_provision"("p_corpus" "uuid", "p_operation" "uuid", "p_claim" "uuid", "p_repo" "text", "p_type" "text", "p_sha" "text") OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "corpora_session_guard"."finish_deletion"("p_operation" "uuid", "p_claim" "uuid", "p_completed" boolean) RETURNS boolean
    LANGUAGE "plpgsql" SECURITY DEFINER
    SET "search_path" TO ''
    SET "lock_timeout" TO '3s'
    AS $$
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


ALTER FUNCTION "corpora_session_guard"."finish_deletion"("p_operation" "uuid", "p_claim" "uuid", "p_completed" boolean) OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "corpora_session_guard"."get_metadata"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid") RETURNS "jsonb"
    LANGUAGE "plpgsql" SECURITY DEFINER
    SET "search_path" TO ''
    SET "lock_timeout" TO '3s'
    AS $$
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


ALTER FUNCTION "corpora_session_guard"."get_metadata"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid") OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "corpora_session_guard"."guard_access"() RETURNS "trigger"
    LANGUAGE "plpgsql" SECURITY DEFINER
    SET "search_path" TO ''
    AS $$
BEGIN
    IF TG_OP='DELETE' OR NEW.corpus_id IS DISTINCT FROM OLD.corpus_id
       OR OLD.deleted_at IS NOT NULL THEN RAISE EXCEPTION 'CORPUS_ACCESS_IMMUTABLE'; END IF;
    RETURN NEW;
END;
$$;


ALTER FUNCTION "corpora_session_guard"."guard_access"() OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "corpora_session_guard"."guard_deletion"() RETURNS "trigger"
    LANGUAGE "plpgsql" SECURITY DEFINER
    SET "search_path" TO ''
    AS $$
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


ALTER FUNCTION "corpora_session_guard"."guard_deletion"() OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "corpora_session_guard"."import_reconciliation_target"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_operation" "uuid") RETURNS "jsonb"
    LANGUAGE "plpgsql" SECURITY DEFINER
    SET "search_path" TO ''
    SET "lock_timeout" TO '3s'
    AS $$
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


ALTER FUNCTION "corpora_session_guard"."import_reconciliation_target"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_operation" "uuid") OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "corpora_session_guard"."import_status"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_operation" "uuid") RETURNS "jsonb"
    LANGUAGE "plpgsql" SECURITY DEFINER
    SET "search_path" TO ''
    SET "lock_timeout" TO '3s'
    AS $$
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


ALTER FUNCTION "corpora_session_guard"."import_status"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_operation" "uuid") OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "corpora_session_guard"."initialize_access"() RETURNS "trigger"
    LANGUAGE "plpgsql" SECURITY DEFINER
    SET "search_path" TO ''
    AS $$
BEGIN
    INSERT INTO public.corpora_corpus_access(corpus_id) VALUES(NEW.id);
    RETURN NEW;
END;
$$;


ALTER FUNCTION "corpora_session_guard"."initialize_access"() OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "corpora_session_guard"."list_corpus_versions"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_before" bigint, "p_limit" integer) RETURNS "jsonb"
    LANGUAGE "plpgsql" SECURITY DEFINER
    SET "search_path" TO ''
    SET "lock_timeout" TO '3s'
    AS $$
DECLARE
    m public.corpora_corpus_metadata%ROWTYPE;
    head_sequence bigint;
    page jsonb;
    last_sequence bigint;
    has_more boolean;
BEGIN
    PERFORM corpora_session_guard.lock_verified_session(p_actor,p_session,p_token_exp);
    IF p_corpus IS NULL OR p_limit IS NULL OR p_limit NOT BETWEEN 1 AND 100
       OR (p_before IS NOT NULL AND p_before < 1) THEN
        RAISE EXCEPTION 'CORPUS_VERSION_HISTORY_INPUT_INVALID';
    END IF;
    SELECT * INTO m FROM public.corpora_corpus_metadata WHERE id=p_corpus AND (
        owner_id=p_actor OR EXISTS (
            SELECT 1 FROM corpora_session_guard.superadmins WHERE user_id=p_actor
        )
    ) FOR SHARE;
    IF NOT FOUND OR NOT EXISTS (
        SELECT 1 FROM public.corpora_corpus_access WHERE corpus_id=p_corpus AND deleted_at IS NULL
    ) THEN
        PERFORM corpora_session_guard.lock_verified_session(p_actor,p_session,p_token_exp);
        RETURN pg_catalog.jsonb_build_object('status','not_found');
    END IF;
    SELECT sequence INTO head_sequence FROM public.corpora_corpus_heads WHERE corpus_id=m.id;
    SELECT coalesce(pg_catalog.jsonb_agg(pg_catalog.jsonb_build_object(
        'sequence', item.sequence, 'commit_sha', item.commit_sha, 'parent_sha', item.parent_sha,
        'operation_id', item.operation_id, 'actor_id', item.actor_id, 'created_at', item.created_at
    ) ORDER BY item.sequence DESC), '[]'::jsonb), min(item.sequence)
    INTO page, last_sequence FROM (
        SELECT v.sequence,v.commit_sha,v.parent_sha,v.operation_id,o.actor_id,v.created_at
        FROM public.corpora_corpus_versions v
        JOIN public.corpora_corpus_write_ops o ON o.id=v.operation_id
            AND o.corpus_id=v.corpus_id AND o.state='completed'
        WHERE v.corpus_id=m.id AND (p_before IS NULL OR v.sequence<p_before)
        ORDER BY v.sequence DESC LIMIT p_limit
    ) item;
    SELECT EXISTS (
        SELECT 1 FROM public.corpora_corpus_versions v
        JOIN public.corpora_corpus_write_ops o ON o.id=v.operation_id
            AND o.corpus_id=v.corpus_id AND o.state='completed'
        WHERE v.corpus_id=m.id AND last_sequence IS NOT NULL AND v.sequence<last_sequence
    ) INTO has_more;
    PERFORM corpora_session_guard.lock_verified_session(p_actor,p_session,p_token_exp);
    RETURN pg_catalog.jsonb_build_object(
        'status','ok','corpus_id',m.id,'current_sequence',head_sequence,
        'versions',page,'next_before',CASE WHEN has_more THEN last_sequence ELSE NULL END
    );
END;
$$;


ALTER FUNCTION "corpora_session_guard"."list_corpus_versions"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_before" bigint, "p_limit" integer) OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "corpora_session_guard"."list_metadata"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_owner" "uuid", "p_after" "uuid", "p_limit" integer) RETURNS "jsonb"
    LANGUAGE "plpgsql" SECURITY DEFINER
    SET "search_path" TO ''
    SET "lock_timeout" TO '3s'
    AS $$
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


ALTER FUNCTION "corpora_session_guard"."list_metadata"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_owner" "uuid", "p_after" "uuid", "p_limit" integer) OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "corpora_session_guard"."lock_verified_session"("p_owner" "uuid", "p_session" "uuid", "p_token_exp" bigint) RETURNS "void"
    LANGUAGE "plpgsql" SECURITY DEFINER
    SET "search_path" TO ''
    AS $$
DECLARE
    session_expiry timestamptz;
BEGIN
    IF p_owner IS NULL OR p_session IS NULL OR p_token_exp IS NULL
       OR p_token_exp <= extract(epoch FROM pg_catalog.clock_timestamp()) THEN
        RAISE EXCEPTION 'AUTH_SESSION_INVALID';
    END IF;
    SELECT s.not_after INTO session_expiry
        FROM auth.sessions AS s
        WHERE s.id = p_session AND s.user_id = p_owner FOR SHARE;
    IF NOT FOUND OR (session_expiry IS NOT NULL
        AND session_expiry <= pg_catalog.clock_timestamp()) THEN
        RAISE EXCEPTION 'AUTH_SESSION_INVALID';
    END IF;
END;
$$;


ALTER FUNCTION "corpora_session_guard"."lock_verified_session"("p_owner" "uuid", "p_session" "uuid", "p_token_exp" bigint) OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "corpora_session_guard"."mark_import_uncertain"("p_operation" "uuid", "p_claim" "uuid") RETURNS boolean
    LANGUAGE "plpgsql" SECURITY DEFINER
    SET "search_path" TO ''
    SET "lock_timeout" TO '3s'
    AS $$
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


ALTER FUNCTION "corpora_session_guard"."mark_import_uncertain"("p_operation" "uuid", "p_claim" "uuid") OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "corpora_session_guard"."mark_provision_uncertain"("p_operation" "uuid", "p_claim" "uuid") RETURNS boolean
    LANGUAGE "plpgsql" SECURITY DEFINER
    SET "search_path" TO ''
    AS $$
DECLARE changed uuid;
BEGIN
    UPDATE public.corpora_corpus_provision_ops SET state = 'uncertain'
        WHERE id = p_operation AND worker_claim = p_claim AND state IN ('executing', 'uncertain')
        RETURNING id INTO changed;
    RETURN changed IS NOT NULL;
END;
$$;


ALTER FUNCTION "corpora_session_guard"."mark_provision_uncertain"("p_operation" "uuid", "p_claim" "uuid") OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "corpora_session_guard"."mark_version_save_uncertain"("p_operation" "uuid", "p_claim" "uuid") RETURNS boolean
    LANGUAGE "plpgsql" SECURITY DEFINER
    SET "search_path" TO ''
    SET "lock_timeout" TO '3s'
    AS $$
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


ALTER FUNCTION "corpora_session_guard"."mark_version_save_uncertain"("p_operation" "uuid", "p_claim" "uuid") OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "corpora_session_guard"."patch_metadata"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_revision" bigint, "p_fields" "jsonb") RETURNS "jsonb"
    LANGUAGE "plpgsql" SECURITY DEFINER
    SET "search_path" TO ''
    SET "lock_timeout" TO '3s'
    AS $$
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


ALTER FUNCTION "corpora_session_guard"."patch_metadata"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_revision" bigint, "p_fields" "jsonb") OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "corpora_session_guard"."project_corpora_list"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_project" "uuid", "p_linked_only" boolean, "p_after" "uuid", "p_limit" integer) RETURNS "jsonb"
    LANGUAGE "plpgsql" SECURITY DEFINER
    SET "search_path" TO ''
    SET "lock_timeout" TO '3s'
    AS $$
DECLARE rows jsonb;
BEGIN
    IF p_linked_only IS NULL OR p_limit IS NULL OR p_limit NOT BETWEEN 1 AND 100 THEN
        RAISE EXCEPTION 'PROJECT_CORPUS_INPUT_INVALID';
    END IF;
    IF NOT corpora_session_guard.project_link_access(p_actor,p_session,p_token_exp,p_project,NOT p_linked_only) THEN
        RETURN jsonb_build_object('status','not_found');
    END IF;
    SELECT coalesce(jsonb_agg(to_jsonb(item) ORDER BY item.id),'[]'::jsonb) INTO rows
    FROM (
        SELECT m.id,m.owner_id,m.title,m.language,m.state,m.revision,m.created_at,m.updated_at,
            (pc.corpus_id IS NOT NULL) AS linked
        FROM public.corpora_corpus_metadata m
        JOIN public.corpora_corpus_access a ON a.corpus_id=m.id AND a.deleted_at IS NULL
        LEFT JOIN public.corpora_project_managed_corpora pc ON pc.corpus_id=m.id AND pc.project_id=p_project
        WHERE m.state='ready' AND EXISTS(SELECT 1 FROM public.corpora_corpus_heads h WHERE h.corpus_id=m.id)
          AND (p_after IS NULL OR m.id>p_after)
          AND (NOT p_linked_only OR pc.corpus_id IS NOT NULL)
          AND (m.owner_id=p_actor OR a.visibility='public' OR EXISTS(
              SELECT 1 FROM corpora_session_guard.superadmins WHERE user_id=p_actor
          ) OR EXISTS(SELECT 1 FROM public.corpora_corpus_readers r WHERE r.corpus_id=m.id AND r.user_id=p_actor))
        ORDER BY m.id LIMIT p_limit+1 FOR SHARE OF m
    ) item;
    PERFORM corpora_session_guard.lock_verified_session(p_actor,p_session,p_token_exp);
    RETURN jsonb_build_object('status','list','rows',rows);
END;
$$;


ALTER FUNCTION "corpora_session_guard"."project_corpora_list"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_project" "uuid", "p_linked_only" boolean, "p_after" "uuid", "p_limit" integer) OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "corpora_session_guard"."project_corpus_set"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_project" "uuid", "p_corpus" "uuid", "p_present" boolean) RETURNS "jsonb"
    LANGUAGE "plpgsql" SECURITY DEFINER
    SET "search_path" TO ''
    SET "lock_timeout" TO '3s'
    AS $$
DECLARE access jsonb;
BEGIN
    IF p_corpus IS NULL OR p_present IS NULL THEN RAISE EXCEPTION 'PROJECT_CORPUS_INPUT_INVALID'; END IF;
    IF NOT corpora_session_guard.project_link_access(p_actor,p_session,p_token_exp,p_project,true) THEN
        RETURN jsonb_build_object('status','not_found');
    END IF;
    IF p_present THEN
        access := corpora_session_guard.read_access(p_actor,p_session,p_token_exp,p_corpus);
        IF access->>'status' IS DISTINCT FROM 'ok' OR access->'head' IS NULL
           OR access->'head' = 'null'::jsonb OR access#>>'{row,state}' IS DISTINCT FROM 'ready' THEN
            RETURN jsonb_build_object('status','not_found');
        END IF;
        INSERT INTO public.corpora_project_managed_corpora(project_id,corpus_id,linked_by)
            VALUES(p_project,p_corpus,p_actor) ON CONFLICT DO NOTHING;
    ELSE
        -- Return the same receipt whether or not the link exists. A manager can
        -- remove an inaccessible or deleted corpus without learning if it exists.
        DELETE FROM public.corpora_project_managed_corpora
            WHERE project_id=p_project AND corpus_id=p_corpus;
    END IF;
    PERFORM corpora_session_guard.lock_verified_session(p_actor,p_session,p_token_exp);
    RETURN jsonb_build_object('status','ok','project_id',p_project,'corpus_id',p_corpus,'linked',p_present);
END;
$$;


ALTER FUNCTION "corpora_session_guard"."project_corpus_set"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_project" "uuid", "p_corpus" "uuid", "p_present" boolean) OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "corpora_session_guard"."project_discovery_admit"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_project" "uuid", "p_discovery" "uuid", "p_reason" "text", "p_evidence" "jsonb") RETURNS "jsonb"
    LANGUAGE "plpgsql" SECURITY DEFINER
    SET "search_path" TO ''
    SET "lock_timeout" TO '3s'
    AS $$
DECLARE prior record; access jsonb;
BEGIN
    IF NOT corpora_session_guard.project_link_access(p_actor,p_session,p_token_exp,p_project,true) THEN
        RETURN pg_catalog.jsonb_build_object('status','not_found');
    END IF;
    IF p_discovery IS NULL OR p_reason IS NULL OR length(p_reason) NOT BETWEEN 1 AND 2000
       OR btrim(p_reason)='' OR p_evidence IS NULL OR pg_column_size(p_evidence)>65536
       OR p_evidence->>'id' IS DISTINCT FROM p_discovery::text
       OR p_evidence->>'status' IS DISTINCT FROM 'pending' THEN
        RAISE EXCEPTION 'PROJECT_LINK_INPUT_INVALID';
    END IF;
    SELECT d.source_corpus,r.actor_id,r.reason,r.evidence INTO prior
        FROM public.corpora_project_discoveries d JOIN public.corpora_project_discovery_revisions r
        USING(project_id,discovery_id)
        WHERE d.project_id=p_project AND d.discovery_id=p_discovery AND r.version=1 FOR SHARE OF d;
    IF NOT FOUND THEN
        PERFORM corpora_session_guard.lock_verified_session(p_actor,p_session,p_token_exp);
        RETURN pg_catalog.jsonb_build_object('status','ok','exists',false);
    END IF;
    access := corpora_session_guard.read_access(p_actor,p_session,p_token_exp,prior.source_corpus);
    IF access->>'status' IS DISTINCT FROM 'ok' OR access->'head' IS NULL
       OR access->'head' = 'null'::jsonb THEN
        RETURN pg_catalog.jsonb_build_object('status','not_found');
    END IF;
    IF prior.actor_id IS DISTINCT FROM p_actor OR prior.reason IS DISTINCT FROM p_reason
       OR prior.evidence IS DISTINCT FROM p_evidence THEN
        RETURN pg_catalog.jsonb_build_object('status','conflict');
    END IF;
    PERFORM corpora_session_guard.lock_verified_session(p_actor,p_session,p_token_exp);
    RETURN pg_catalog.jsonb_build_object('status','ok','exists',true);
END;
$$;


ALTER FUNCTION "corpora_session_guard"."project_discovery_admit"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_project" "uuid", "p_discovery" "uuid", "p_reason" "text", "p_evidence" "jsonb") OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "corpora_session_guard"."project_discovery_decide"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_project" "uuid", "p_discovery" "uuid", "p_expected" bigint, "p_reason" "text", "p_action" "text") RETURNS "jsonb"
    LANGUAGE "plpgsql" SECURITY DEFINER
    SET "search_path" TO ''
    SET "lock_timeout" TO '3s'
    AS $$
DECLARE current_row record; access jsonb; evidence jsonb; next_version bigint;
BEGIN
    IF NOT corpora_session_guard.project_link_access(p_actor,p_session,p_token_exp,p_project,true) THEN
        RETURN pg_catalog.jsonb_build_object('status','not_found');
    END IF;
    IF p_discovery IS NULL OR p_expected IS NULL OR p_expected<1 OR p_reason IS NULL
       OR length(p_reason) NOT BETWEEN 1 AND 2000 OR btrim(p_reason)=''
       OR p_action IS NULL OR p_action NOT IN ('accept','reject') THEN
        RAISE EXCEPTION 'PROJECT_LINK_INPUT_INVALID';
    END IF;
    SELECT h.version,h.source_corpus,r.evidence INTO current_row
        FROM public.corpora_project_discoveries h JOIN public.corpora_project_discovery_revisions r
        USING(project_id,discovery_id,version)
        WHERE h.project_id=p_project AND h.discovery_id=p_discovery FOR UPDATE OF h;
    IF NOT FOUND THEN RETURN pg_catalog.jsonb_build_object('status','not_found'); END IF;
    access := corpora_session_guard.read_access(p_actor,p_session,p_token_exp,current_row.source_corpus);
    IF access->>'status' IS DISTINCT FROM 'ok' OR access->'head' IS NULL
       OR access->'head' = 'null'::jsonb THEN
        RETURN pg_catalog.jsonb_build_object('status','not_found');
    END IF;
    IF p_expected<>current_row.version THEN RETURN pg_catalog.jsonb_build_object('status','conflict'); END IF;
    IF p_action='accept' AND NOT EXISTS (SELECT 1 FROM public.corpora_corpus_heads
        WHERE corpus_id=current_row.source_corpus AND commit_sha=current_row.evidence#>>'{source,revision}') THEN
        RETURN pg_catalog.jsonb_build_object('status','stale_corpus');
    END IF;
    IF current_row.evidence->>'status' IS DISTINCT FROM 'pending' THEN
        RETURN pg_catalog.jsonb_build_object('status','invalid_transition');
    END IF;
    evidence := pg_catalog.jsonb_set(current_row.evidence,'{status}',pg_catalog.to_jsonb(
        CASE p_action WHEN 'accept' THEN 'accepted'::text ELSE 'rejected'::text END));
    next_version := current_row.version+1;
    UPDATE public.corpora_project_discoveries SET version=next_version
        WHERE project_id=p_project AND discovery_id=p_discovery;
    INSERT INTO public.corpora_project_discovery_revisions
        (project_id,discovery_id,version,actor_id,reason,action,evidence)
        VALUES(p_project,p_discovery,next_version,p_actor,p_reason,p_action,evidence);
    PERFORM corpora_session_guard.lock_verified_session(p_actor,p_session,p_token_exp);
    RETURN pg_catalog.jsonb_build_object('status','ok','version',next_version);
END;
$$;


ALTER FUNCTION "corpora_session_guard"."project_discovery_decide"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_project" "uuid", "p_discovery" "uuid", "p_expected" bigint, "p_reason" "text", "p_action" "text") OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "corpora_session_guard"."project_discovery_history"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_project" "uuid", "p_discovery" "uuid", "p_before" bigint, "p_limit" integer) RETURNS "jsonb"
    LANGUAGE "plpgsql" SECURITY DEFINER
    SET "search_path" TO ''
    SET "lock_timeout" TO '3s'
    AS $$
DECLARE corpus uuid; access jsonb; items jsonb;
BEGIN
    IF NOT corpora_session_guard.project_link_access(p_actor,p_session,p_token_exp,p_project,false) THEN
        RETURN pg_catalog.jsonb_build_object('status','not_found');
    END IF;
    IF p_discovery IS NULL OR p_limit IS NULL OR p_limit NOT BETWEEN 1 AND 10
       OR (p_before IS NOT NULL AND p_before<1) THEN RAISE EXCEPTION 'PROJECT_LINK_INPUT_INVALID'; END IF;
    SELECT source_corpus INTO corpus FROM public.corpora_project_discoveries
        WHERE project_id=p_project AND discovery_id=p_discovery FOR SHARE;
    IF NOT FOUND THEN RETURN pg_catalog.jsonb_build_object('status','not_found'); END IF;
    access := corpora_session_guard.read_access(p_actor,p_session,p_token_exp,corpus);
    IF access->>'status' IS DISTINCT FROM 'ok' OR access->'head' IS NULL
       OR access->'head' = 'null'::jsonb THEN
        RETURN pg_catalog.jsonb_build_object('status','not_found');
    END IF;
    SELECT coalesce(pg_catalog.jsonb_agg(pg_catalog.to_jsonb(r) ORDER BY r.version DESC),'[]'::jsonb)
    INTO items FROM (SELECT version,actor_id,recorded_at,reason,action,evidence
        FROM public.corpora_project_discovery_revisions
        WHERE project_id=p_project AND discovery_id=p_discovery AND (p_before IS NULL OR version<p_before)
        ORDER BY version DESC LIMIT p_limit+1) r;
    PERFORM corpora_session_guard.lock_verified_session(p_actor,p_session,p_token_exp);
    RETURN pg_catalog.jsonb_build_object('status','ok','revisions',items);
END;
$$;


ALTER FUNCTION "corpora_session_guard"."project_discovery_history"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_project" "uuid", "p_discovery" "uuid", "p_before" bigint, "p_limit" integer) OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "corpora_session_guard"."project_discovery_register"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_project" "uuid", "p_discovery" "uuid", "p_reason" "text", "p_evidence" "jsonb") RETURNS "jsonb"
    LANGUAGE "plpgsql" SECURITY DEFINER
    SET "search_path" TO ''
    SET "lock_timeout" TO '3s'
    AS $$
DECLARE corpus uuid; access jsonb; prior record;
BEGIN
    IF NOT corpora_session_guard.project_link_access(p_actor,p_session,p_token_exp,p_project,true) THEN
        RETURN pg_catalog.jsonb_build_object('status','not_found');
    END IF;
    IF p_discovery IS NULL OR p_reason IS NULL OR length(p_reason) NOT BETWEEN 1 AND 2000
       OR btrim(p_reason)='' OR p_evidence IS NULL OR pg_column_size(p_evidence)>65536
       OR p_evidence->>'id' IS DISTINCT FROM p_discovery::text
       OR p_evidence->>'status' IS DISTINCT FROM 'pending' THEN
        RAISE EXCEPTION 'PROJECT_LINK_INPUT_INVALID';
    END IF;
    corpus := (p_evidence#>>'{source,package_id}')::uuid;
    IF corpus IS NULL THEN RAISE EXCEPTION 'PROJECT_LINK_INPUT_INVALID'; END IF;
    access := corpora_session_guard.read_access(p_actor,p_session,p_token_exp,corpus);
    IF access->>'status' IS DISTINCT FROM 'ok' OR access->'head' IS NULL
       OR access->'head' = 'null'::jsonb THEN
        RETURN pg_catalog.jsonb_build_object('status','not_found');
    END IF;
    IF NOT EXISTS (SELECT 1 FROM public.corpora_corpus_heads
        WHERE corpus_id=corpus AND commit_sha=p_evidence#>>'{source,revision}') THEN
        RETURN pg_catalog.jsonb_build_object('status','stale_corpus');
    END IF;
    INSERT INTO public.corpora_project_discoveries(project_id,discovery_id,source_corpus,version)
        VALUES(p_project,p_discovery,corpus,1) ON CONFLICT DO NOTHING;
    IF NOT FOUND THEN
        SELECT r.actor_id,r.reason,r.evidence,d.source_corpus INTO prior
        FROM public.corpora_project_discovery_revisions r
        JOIN public.corpora_project_discoveries d USING(project_id,discovery_id)
        WHERE r.project_id=p_project AND r.discovery_id=p_discovery AND r.version=1;
        access := corpora_session_guard.read_access(p_actor,p_session,p_token_exp,prior.source_corpus);
        IF access->>'status' IS DISTINCT FROM 'ok' OR access->'head' IS NULL
           OR access->'head' = 'null'::jsonb THEN
            RETURN pg_catalog.jsonb_build_object('status','not_found');
        END IF;
        IF prior.actor_id IS DISTINCT FROM p_actor OR prior.reason IS DISTINCT FROM p_reason
           OR prior.evidence IS DISTINCT FROM p_evidence THEN
            RETURN pg_catalog.jsonb_build_object('status','conflict');
        END IF;
        PERFORM corpora_session_guard.lock_verified_session(p_actor,p_session,p_token_exp);
        RETURN pg_catalog.jsonb_build_object('status','ok','version',1);
    END IF;
    INSERT INTO public.corpora_project_discovery_revisions
        (project_id,discovery_id,version,actor_id,reason,action,evidence)
        VALUES(p_project,p_discovery,1,p_actor,p_reason,'register',p_evidence);
    PERFORM corpora_session_guard.lock_verified_session(p_actor,p_session,p_token_exp);
    RETURN pg_catalog.jsonb_build_object('status','ok','version',1);
END;
$$;


ALTER FUNCTION "corpora_session_guard"."project_discovery_register"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_project" "uuid", "p_discovery" "uuid", "p_reason" "text", "p_evidence" "jsonb") OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "corpora_session_guard"."project_link_access"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_project" "uuid", "p_edit" boolean) RETURNS boolean
    LANGUAGE "plpgsql" SECURITY DEFINER
    SET "search_path" TO ''
    SET "lock_timeout" TO '3s'
    AS $$
DECLARE project public.projects%ROWTYPE; manager boolean;
BEGIN
    PERFORM corpora_session_guard.lock_verified_session(p_actor,p_session,p_token_exp);
    IF p_project IS NULL OR p_edit IS NULL THEN RAISE EXCEPTION 'PROJECT_LINK_INPUT_INVALID'; END IF;
    SELECT * INTO project FROM public.projects WHERE id=p_project FOR SHARE;
    IF NOT FOUND THEN RETURN false; END IF;
    manager := project.owner_id IS NOT DISTINCT FROM p_actor OR EXISTS (
        SELECT 1 FROM corpora_session_guard.superadmins WHERE user_id=p_actor);
    RETURN manager OR (NOT p_edit AND EXISTS (
        SELECT 1 FROM public.corpora_project_members WHERE project_id=p_project AND user_id=p_actor));
END;
$$;


ALTER FUNCTION "corpora_session_guard"."project_link_access"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_project" "uuid", "p_edit" boolean) OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "corpora_session_guard"."project_link_current"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_project" "uuid", "p_reference" "uuid") RETURNS "jsonb"
    LANGUAGE "plpgsql" SECURITY DEFINER
    SET "search_path" TO ''
    SET "lock_timeout" TO '3s'
    AS $$
DECLARE current_row record; item uuid; access jsonb;
BEGIN
    IF NOT corpora_session_guard.project_link_access(p_actor,p_session,p_token_exp,p_project,true) THEN
        RETURN pg_catalog.jsonb_build_object('status','not_found');
    END IF;
    SELECT r.* INTO current_row FROM public.corpora_project_link_heads h
        JOIN public.corpora_project_link_revisions r USING(project_id,reference_id,version)
        WHERE h.project_id=p_project AND h.reference_id=p_reference FOR SHARE OF h;
    IF NOT FOUND THEN RETURN pg_catalog.jsonb_build_object('status','not_found'); END IF;
    FOR item IN SELECT DISTINCT id FROM (VALUES(current_row.source_corpus),(current_row.target_corpus)) AS ids(id)
        ORDER BY id LOOP
        access := corpora_session_guard.read_access(p_actor,p_session,p_token_exp,item);
        IF access->>'status' IS DISTINCT FROM 'ok' OR access->'head' IS NULL
           OR access->'head' = 'null'::jsonb THEN
            RETURN pg_catalog.jsonb_build_object('status','not_found');
        END IF;
    END LOOP;
    PERFORM corpora_session_guard.lock_verified_session(p_actor,p_session,p_token_exp);
    RETURN pg_catalog.jsonb_build_object('status','ok','revision',pg_catalog.jsonb_build_object(
        'version',current_row.version,'actor_id',current_row.actor_id,'recorded_at',current_row.recorded_at,
        'reason',current_row.reason,'action',current_row.action,'reference',current_row.reference));
END;
$$;


ALTER FUNCTION "corpora_session_guard"."project_link_current"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_project" "uuid", "p_reference" "uuid") OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "corpora_session_guard"."project_link_decide"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_project" "uuid", "p_reference" "uuid", "p_expected" bigint, "p_reason" "text", "p_action" "text") RETURNS "jsonb"
    LANGUAGE "plpgsql" SECURITY DEFINER
    SET "search_path" TO ''
    SET "lock_timeout" TO '3s'
    AS $$
DECLARE previous record; item uuid; access jsonb; value jsonb; next_version bigint;
BEGIN
    IF NOT corpora_session_guard.project_link_access(p_actor,p_session,p_token_exp,p_project,true) THEN
        RETURN pg_catalog.jsonb_build_object('status','not_found');
    END IF;
    IF p_reference IS NULL OR p_expected IS NULL OR p_expected < 1 OR p_reason IS NULL
       OR length(p_reason) NOT BETWEEN 1 AND 2000 OR btrim(p_reason)=''
       OR p_action NOT IN ('reject','reopen','resolve','approve','publish','withdraw') OR p_action IS NULL THEN
        RAISE EXCEPTION 'PROJECT_LINK_INPUT_INVALID';
    END IF;
    SELECT h.version,r.reference,r.source_corpus,r.target_corpus INTO previous
        FROM public.corpora_project_link_heads h
        JOIN public.corpora_project_link_revisions r USING(project_id,reference_id,version)
        WHERE h.project_id=p_project AND h.reference_id=p_reference FOR UPDATE OF h;
    IF NOT FOUND THEN RETURN pg_catalog.jsonb_build_object('status','not_found'); END IF;
    IF p_expected <> previous.version THEN RETURN pg_catalog.jsonb_build_object('status','conflict'); END IF;
    FOR item IN SELECT DISTINCT id FROM (VALUES(previous.source_corpus),(previous.target_corpus)) AS ids(id)
        ORDER BY id LOOP
        access := corpora_session_guard.read_access(p_actor,p_session,p_token_exp,item);
        IF access->>'status' IS DISTINCT FROM 'ok' OR access->'head' IS NULL
           OR access->'head' = 'null'::jsonb THEN
            RETURN pg_catalog.jsonb_build_object('status','not_found');
        END IF;
    END LOOP;
    IF p_action IN ('resolve','approve','publish') AND (
        access IS NULL OR NOT EXISTS (
            SELECT 1 FROM public.corpora_corpus_heads
            WHERE corpus_id=previous.source_corpus AND commit_sha=previous.reference#>>'{source,revision}') OR
        NOT EXISTS (
            SELECT 1 FROM public.corpora_corpus_heads
            WHERE corpus_id=previous.target_corpus AND commit_sha=previous.reference#>>'{target,revision}')) THEN
        RETURN pg_catalog.jsonb_build_object('status','stale_corpus');
    END IF;
    IF (p_action='reject' AND previous.reference->>'review' IS DISTINCT FROM 'pending') OR
       (p_action='reopen' AND previous.reference->>'review' IS DISTINCT FROM 'rejected') OR
       (p_action='resolve' AND (previous.reference->>'review' IS DISTINCT FROM 'pending' OR
           previous.reference->>'resolution' IS DISTINCT FROM 'unresolved')) OR
       (p_action='approve' AND (previous.reference->>'review' IS DISTINCT FROM 'pending' OR
           previous.reference->>'resolution' IS DISTINCT FROM 'resolved')) OR
       (p_action='publish' AND (previous.reference->>'review' IS DISTINCT FROM 'approved' OR
           previous.reference->>'resolution' IS DISTINCT FROM 'resolved' OR
           (previous.reference->>'publication' IS DISTINCT FROM 'draft' AND
            previous.reference->>'publication' IS DISTINCT FROM 'withdrawn'))) OR
       (p_action='withdraw' AND previous.reference->>'publication' IS DISTINCT FROM 'published') THEN
        RETURN pg_catalog.jsonb_build_object('status','invalid_transition');
    END IF;
    value := CASE p_action
        WHEN 'resolve' THEN pg_catalog.jsonb_set(previous.reference,'{resolution}', '"resolved"'::jsonb)
        WHEN 'approve' THEN pg_catalog.jsonb_set(previous.reference,'{review}', '"approved"'::jsonb)
        WHEN 'reject' THEN pg_catalog.jsonb_set(previous.reference,'{review}', '"rejected"'::jsonb)
        WHEN 'publish' THEN pg_catalog.jsonb_set(previous.reference,'{publication}', '"published"'::jsonb)
        WHEN 'withdraw' THEN pg_catalog.jsonb_set(previous.reference,'{publication}', '"withdrawn"'::jsonb)
        ELSE pg_catalog.jsonb_set(previous.reference,'{review}', '"pending"'::jsonb)
    END;
    next_version := previous.version+1;
    UPDATE public.corpora_project_link_heads SET version=next_version
        WHERE project_id=p_project AND reference_id=p_reference;
    INSERT INTO public.corpora_project_link_revisions(project_id,reference_id,version,actor_id,reason,
        action,source_corpus,target_corpus,reference)
        VALUES(p_project,p_reference,next_version,p_actor,p_reason,p_action,
            previous.source_corpus,previous.target_corpus,value);
    PERFORM corpora_session_guard.lock_verified_session(p_actor,p_session,p_token_exp);
    RETURN pg_catalog.jsonb_build_object('status','ok','version',next_version);
END;
$$;


ALTER FUNCTION "corpora_session_guard"."project_link_decide"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_project" "uuid", "p_reference" "uuid", "p_expected" bigint, "p_reason" "text", "p_action" "text") OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "corpora_session_guard"."project_link_export"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_project" "uuid", "p_references" "uuid"[]) RETURNS "jsonb"
    LANGUAGE "plpgsql" SECURITY DEFINER
    SET "search_path" TO ''
    SET "lock_timeout" TO '3s'
    AS $$
DECLARE wanted uuid; item uuid; access jsonb; unique_count integer; revisions jsonb;
BEGIN
    IF NOT corpora_session_guard.project_link_access(p_actor,p_session,p_token_exp,p_project,false) THEN
        RETURN pg_catalog.jsonb_build_object('status','not_found');
    END IF;
    IF p_references IS NULL OR cardinality(p_references) NOT BETWEEN 1 AND 10
       OR pg_catalog.array_position(p_references,NULL) IS NOT NULL THEN
        RAISE EXCEPTION 'PROJECT_LINK_EXPORT_INPUT_INVALID';
    END IF;
    SELECT count(DISTINCT id) INTO unique_count FROM pg_catalog.unnest(p_references) AS ids(id);
    IF unique_count<>cardinality(p_references) THEN RAISE EXCEPTION 'PROJECT_LINK_EXPORT_INPUT_INVALID'; END IF;
    FOR wanted IN SELECT id FROM pg_catalog.unnest(p_references) AS ids(id) ORDER BY id LOOP
        PERFORM 1 FROM public.corpora_project_link_heads
            WHERE project_id=p_project AND reference_id=wanted FOR SHARE;
        IF NOT FOUND THEN RETURN pg_catalog.jsonb_build_object('status','not_found'); END IF;
    END LOOP;
    FOR item IN
        SELECT DISTINCT id FROM (
            SELECT r.source_corpus AS id FROM public.corpora_project_link_heads h
                JOIN public.corpora_project_link_revisions r USING(project_id,reference_id,version)
                WHERE h.project_id=p_project AND h.reference_id=ANY(p_references)
            UNION
            SELECT r.target_corpus AS id FROM public.corpora_project_link_heads h
                JOIN public.corpora_project_link_revisions r USING(project_id,reference_id,version)
                WHERE h.project_id=p_project AND h.reference_id=ANY(p_references)
        ) corpora ORDER BY id
    LOOP
        access := corpora_session_guard.read_access(p_actor,p_session,p_token_exp,item);
        IF access->>'status' IS DISTINCT FROM 'ok' OR access->'head' IS NULL
           OR access->'head' = 'null'::jsonb THEN
            RETURN pg_catalog.jsonb_build_object('status','not_found');
        END IF;
    END LOOP;
    SELECT pg_catalog.jsonb_agg(pg_catalog.jsonb_build_object(
        'version',r.version,'actor_id',r.actor_id,'recorded_at',r.recorded_at,
        'reason',r.reason,'action',r.action,'reference',r.reference
    ) ORDER BY h.reference_id) INTO revisions FROM public.corpora_project_link_heads h
        JOIN public.corpora_project_link_revisions r USING(project_id,reference_id,version)
        WHERE h.project_id=p_project AND h.reference_id=ANY(p_references);
    IF revisions IS NULL OR pg_catalog.jsonb_array_length(revisions)<>cardinality(p_references) THEN
        RETURN pg_catalog.jsonb_build_object('status','not_found');
    END IF;
    PERFORM corpora_session_guard.lock_verified_session(p_actor,p_session,p_token_exp);
    RETURN pg_catalog.jsonb_build_object('status','ok','revisions',revisions);
END;
$$;


ALTER FUNCTION "corpora_session_guard"."project_link_export"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_project" "uuid", "p_references" "uuid"[]) OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "corpora_session_guard"."project_link_history"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_project" "uuid", "p_reference" "uuid", "p_before" bigint, "p_limit" integer) RETURNS "jsonb"
    LANGUAGE "plpgsql" SECURITY DEFINER
    SET "search_path" TO ''
    SET "lock_timeout" TO '3s'
    AS $$
DECLARE items jsonb; item uuid; access jsonb; ids uuid[];
BEGIN
    IF NOT corpora_session_guard.project_link_access(p_actor,p_session,p_token_exp,p_project,false) THEN
        RETURN pg_catalog.jsonb_build_object('status','not_found');
    END IF;
    IF p_reference IS NULL OR p_limit IS NULL OR p_limit NOT BETWEEN 1 AND 10
       OR (p_before IS NOT NULL AND p_before < 1) THEN
        RAISE EXCEPTION 'PROJECT_LINK_INPUT_INVALID';
    END IF;
    PERFORM 1 FROM public.corpora_project_link_heads
        WHERE project_id=p_project AND reference_id=p_reference FOR SHARE;
    IF NOT FOUND THEN
        RETURN pg_catalog.jsonb_build_object('status','not_found');
    END IF;
    SELECT pg_catalog.array_agg(DISTINCT x.corpus ORDER BY x.corpus) INTO ids
    FROM (SELECT source_corpus,target_corpus FROM public.corpora_project_link_revisions
        WHERE project_id=p_project AND reference_id=p_reference AND version IN (
            SELECT version FROM public.corpora_project_link_heads
                WHERE project_id=p_project AND reference_id=p_reference
            UNION
            (SELECT version FROM public.corpora_project_link_revisions
                WHERE project_id=p_project AND reference_id=p_reference
                    AND (p_before IS NULL OR version<p_before)
                ORDER BY version DESC LIMIT p_limit+1))) r
    CROSS JOIN LATERAL (VALUES(r.source_corpus),(r.target_corpus)) AS x(corpus);
    FOREACH item IN ARRAY coalesce(ids,ARRAY[]::uuid[]) LOOP
        access := corpora_session_guard.read_access(p_actor,p_session,p_token_exp,item);
        IF access->>'status' IS DISTINCT FROM 'ok' OR access->'head' IS NULL
           OR access->'head' = 'null'::jsonb THEN
            RETURN pg_catalog.jsonb_build_object('status','not_found');
        END IF;
    END LOOP;
    SELECT coalesce(pg_catalog.jsonb_agg(pg_catalog.to_jsonb(r) ORDER BY r.version DESC),'[]'::jsonb)
    INTO items FROM (SELECT version,actor_id,recorded_at,reason,action,reference
        FROM public.corpora_project_link_revisions
        WHERE project_id=p_project AND reference_id=p_reference
          AND (p_before IS NULL OR version<p_before)
        ORDER BY version DESC LIMIT p_limit+1) r;
    PERFORM corpora_session_guard.lock_verified_session(p_actor,p_session,p_token_exp);
    RETURN pg_catalog.jsonb_build_object('status','ok','revisions',items);
END;
$$;


ALTER FUNCTION "corpora_session_guard"."project_link_history"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_project" "uuid", "p_reference" "uuid", "p_before" bigint, "p_limit" integer) OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "corpora_session_guard"."project_link_read_current"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_project" "uuid", "p_reference" "uuid") RETURNS "jsonb"
    LANGUAGE "plpgsql" SECURITY DEFINER
    SET "search_path" TO ''
    SET "lock_timeout" TO '3s'
    AS $$
DECLARE current_row record; item uuid; access jsonb;
BEGIN
    IF NOT corpora_session_guard.project_link_access(p_actor,p_session,p_token_exp,p_project,false) THEN
        RETURN pg_catalog.jsonb_build_object('status','not_found');
    END IF;
    SELECT r.* INTO current_row FROM public.corpora_project_link_heads h
        JOIN public.corpora_project_link_revisions r USING(project_id,reference_id,version)
        WHERE h.project_id=p_project AND h.reference_id=p_reference FOR SHARE OF h;
    IF NOT FOUND THEN RETURN pg_catalog.jsonb_build_object('status','not_found'); END IF;
    FOR item IN SELECT DISTINCT id FROM (VALUES(current_row.source_corpus),(current_row.target_corpus)) AS ids(id)
        ORDER BY id LOOP
        access := corpora_session_guard.read_access(p_actor,p_session,p_token_exp,item);
        IF access->>'status' IS DISTINCT FROM 'ok' OR access->'head' IS NULL
           OR access->'head' = 'null'::jsonb THEN
            RETURN pg_catalog.jsonb_build_object('status','not_found');
        END IF;
    END LOOP;
    PERFORM corpora_session_guard.lock_verified_session(p_actor,p_session,p_token_exp);
    RETURN pg_catalog.jsonb_build_object('status','ok','revision',pg_catalog.jsonb_build_object(
        'version',current_row.version,'actor_id',current_row.actor_id,'recorded_at',current_row.recorded_at,
        'reason',current_row.reason,'action',current_row.action,'reference',current_row.reference));
END;
$$;


ALTER FUNCTION "corpora_session_guard"."project_link_read_current"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_project" "uuid", "p_reference" "uuid") OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "corpora_session_guard"."project_link_save"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_project" "uuid", "p_reference" "uuid", "p_expected" bigint, "p_reason" "text", "p_value" "jsonb") RETURNS "jsonb"
    LANGUAGE "plpgsql" SECURITY DEFINER
    SET "search_path" TO ''
    SET "lock_timeout" TO '3s'
    AS $$
DECLARE source_id uuid; target_id uuid; item uuid; access jsonb; previous record; next_version bigint;
BEGIN
    IF NOT corpora_session_guard.project_link_access(p_actor,p_session,p_token_exp,p_project,true) THEN
        RETURN pg_catalog.jsonb_build_object('status','not_found');
    END IF;
    IF p_reference IS NULL OR p_reason IS NULL OR length(p_reason) NOT BETWEEN 1 AND 2000
       OR btrim(p_reason)='' OR p_value IS NULL OR pg_column_size(p_value)>65536
       OR p_value->>'id' IS DISTINCT FROM p_reference::text
       OR p_value->>'resolution' IS DISTINCT FROM 'unresolved'
       OR p_value->>'review' IS DISTINCT FROM 'pending'
       OR p_value->>'publication' IS DISTINCT FROM 'draft'
       OR (p_expected IS NOT NULL AND p_expected < 1) THEN
        RAISE EXCEPTION 'PROJECT_LINK_INPUT_INVALID';
    END IF;
    source_id := (p_value#>>'{source,package_id}')::uuid;
    target_id := (p_value#>>'{target,package_id}')::uuid;
    IF source_id IS NULL OR target_id IS NULL THEN RAISE EXCEPTION 'PROJECT_LINK_INPUT_INVALID'; END IF;
    SELECT h.version,r.reference,r.source_corpus,r.target_corpus INTO previous
        FROM public.corpora_project_link_heads h
        JOIN public.corpora_project_link_revisions r USING(project_id,reference_id,version)
        WHERE h.project_id=p_project AND h.reference_id=p_reference FOR UPDATE OF h;
    IF FOUND AND p_expected IS DISTINCT FROM previous.version THEN
        RETURN pg_catalog.jsonb_build_object('status','conflict');
    ELSIF NOT FOUND AND p_expected IS NOT NULL THEN
        RETURN pg_catalog.jsonb_build_object('status','conflict');
    END IF;
    FOR item IN SELECT DISTINCT id FROM (VALUES(source_id),(target_id),
        (previous.source_corpus),(previous.target_corpus)) AS ids(id)
        WHERE id IS NOT NULL ORDER BY id LOOP
        access := corpora_session_guard.read_access(p_actor,p_session,p_token_exp,item);
        IF access->>'status' IS DISTINCT FROM 'ok' OR access->'head' IS NULL
           OR access->'head' = 'null'::jsonb THEN
            RETURN pg_catalog.jsonb_build_object('status','not_found');
        END IF;
    END LOOP;
    IF NOT EXISTS (SELECT 1 FROM public.corpora_corpus_versions
        WHERE corpus_id=source_id AND commit_sha=p_value#>>'{source,revision}') OR NOT EXISTS (
        SELECT 1 FROM public.corpora_corpus_versions
        WHERE corpus_id=target_id AND commit_sha=p_value#>>'{target,revision}') THEN
        RETURN pg_catalog.jsonb_build_object('status','not_found');
    END IF;
    IF previous.version IS NOT NULL THEN
        IF previous.reference->>'review' IS DISTINCT FROM 'pending' THEN
            RETURN pg_catalog.jsonb_build_object('status','invalid_transition');
        END IF;
        -- A later edit retains the first author's provenance.
        p_value := pg_catalog.jsonb_set(p_value,'{provenance}',previous.reference->'provenance');
        next_version := previous.version+1;
        UPDATE public.corpora_project_link_heads SET version=next_version
            WHERE project_id=p_project AND reference_id=p_reference;
    ELSE
        IF p_value#>>'{provenance,agent_id}' IS DISTINCT FROM p_actor::text OR
           p_value#>>'{provenance,origin}' IS DISTINCT FROM 'manual' THEN
            RAISE EXCEPTION 'PROJECT_LINK_INPUT_INVALID';
        END IF;
        next_version := 1;
        INSERT INTO public.corpora_project_link_heads(project_id,reference_id,version)
            VALUES(p_project,p_reference,next_version) ON CONFLICT DO NOTHING;
        IF NOT FOUND THEN RETURN pg_catalog.jsonb_build_object('status','conflict'); END IF;
    END IF;
    INSERT INTO public.corpora_project_link_revisions(project_id,reference_id,version,actor_id,reason,
        action,source_corpus,target_corpus,reference)
        VALUES(p_project,p_reference,next_version,p_actor,p_reason,'edit',source_id,target_id,p_value);
    PERFORM corpora_session_guard.lock_verified_session(p_actor,p_session,p_token_exp);
    RETURN pg_catalog.jsonb_build_object('status','ok','version',next_version);
END;
$$;


ALTER FUNCTION "corpora_session_guard"."project_link_save"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_project" "uuid", "p_reference" "uuid", "p_expected" bigint, "p_reason" "text", "p_value" "jsonb") OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "corpora_session_guard"."project_member_add_email"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_project" "uuid", "p_email" "text") RETURNS "jsonb"
    LANGUAGE "plpgsql" SECURITY DEFINER
    SET "search_path" TO ''
    SET "lock_timeout" TO '3s'
    AS $$
DECLARE project public.projects%ROWTYPE; member_id uuid; matches bigint;
BEGIN
    PERFORM corpora_session_guard.lock_verified_session(p_actor,p_session,p_token_exp);
    IF p_project IS NULL OR p_email IS NULL OR length(p_email)>254
       OR pg_catalog.btrim(p_email)='' THEN
        RAISE EXCEPTION 'PROJECT_MEMBER_INPUT_INVALID';
    END IF;
    SELECT * INTO project FROM public.projects WHERE id=p_project FOR UPDATE;
    IF NOT FOUND OR (project.owner_id IS DISTINCT FROM p_actor AND NOT EXISTS(
        SELECT 1 FROM corpora_session_guard.superadmins WHERE user_id=p_actor)) THEN
        RETURN pg_catalog.jsonb_build_object('status','not_found');
    END IF;
    SELECT count(*),(pg_catalog.array_agg(id))[1] INTO matches,member_id FROM auth.users
        WHERE pg_catalog.lower(email)=pg_catalog.lower(pg_catalog.btrim(p_email))
          AND email_confirmed_at IS NOT NULL AND deleted_at IS NULL;
    IF matches<>1 THEN RETURN pg_catalog.jsonb_build_object('status','member_not_found'); END IF;
    RETURN corpora_session_guard.project_member_set(
        p_actor,p_session,p_token_exp,p_project,member_id,true);
END;
$$;


ALTER FUNCTION "corpora_session_guard"."project_member_add_email"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_project" "uuid", "p_email" "text") OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "corpora_session_guard"."project_member_set"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_project" "uuid", "p_member" "uuid", "p_present" boolean) RETURNS "jsonb"
    LANGUAGE "plpgsql" SECURITY DEFINER
    SET "search_path" TO ''
    SET "lock_timeout" TO '3s'
    AS $$
DECLARE project public.projects%ROWTYPE;
BEGIN
    PERFORM corpora_session_guard.lock_verified_session(p_actor,p_session,p_token_exp);
    IF p_project IS NULL OR p_member IS NULL OR p_present IS NULL THEN
        RAISE EXCEPTION 'PROJECT_MEMBER_INPUT_INVALID';
    END IF;
    SELECT * INTO project FROM public.projects WHERE id=p_project FOR UPDATE;
    IF NOT FOUND OR (project.owner_id IS DISTINCT FROM p_actor AND NOT EXISTS(
        SELECT 1 FROM corpora_session_guard.superadmins WHERE user_id=p_actor)) THEN
        RETURN pg_catalog.jsonb_build_object('status','not_found');
    END IF;
    IF p_present AND NOT EXISTS(SELECT 1 FROM auth.users WHERE id=p_member AND deleted_at IS NULL
        AND (email_confirmed_at IS NOT NULL OR phone_confirmed_at IS NOT NULL)) THEN
        RETURN pg_catalog.jsonb_build_object('status','member_not_found');
    END IF;
    IF p_present THEN
        INSERT INTO public.corpora_project_members(project_id,user_id,added_by)
            VALUES(p_project,p_member,p_actor) ON CONFLICT DO NOTHING;
    ELSE
        DELETE FROM public.corpora_project_members WHERE project_id=p_project AND user_id=p_member;
    END IF;
    PERFORM corpora_session_guard.lock_verified_session(p_actor,p_session,p_token_exp);
    RETURN pg_catalog.jsonb_build_object('status','ok','project_id',p_project,
        'user_id',p_member,'present',p_present);
END;
$$;


ALTER FUNCTION "corpora_session_guard"."project_member_set"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_project" "uuid", "p_member" "uuid", "p_present" boolean) OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "corpora_session_guard"."project_members_list"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_project" "uuid", "p_after" "uuid", "p_limit" integer) RETURNS "jsonb"
    LANGUAGE "plpgsql" SECURITY DEFINER
    SET "search_path" TO ''
    SET "lock_timeout" TO '3s'
    AS $$
DECLARE project public.projects%ROWTYPE; members jsonb;
BEGIN
    PERFORM corpora_session_guard.lock_verified_session(p_actor,p_session,p_token_exp);
    IF p_project IS NULL OR p_limit IS NULL OR p_limit NOT BETWEEN 1 AND 100 THEN
        RAISE EXCEPTION 'PROJECT_MEMBERS_INPUT_INVALID';
    END IF;
    SELECT * INTO project FROM public.projects WHERE id=p_project FOR SHARE;
    IF NOT FOUND OR (project.owner_id IS DISTINCT FROM p_actor AND NOT EXISTS(
        SELECT 1 FROM corpora_session_guard.superadmins WHERE user_id=p_actor)) THEN
        RETURN pg_catalog.jsonb_build_object('status','not_found');
    END IF;
    SELECT coalesce(pg_catalog.jsonb_agg(pg_catalog.to_jsonb(item) ORDER BY item.user_id),'[]'::jsonb)
        INTO members FROM (SELECT user_id,created_at FROM public.corpora_project_members
            WHERE project_id=p_project AND (p_after IS NULL OR user_id>p_after)
            ORDER BY user_id LIMIT p_limit+1) item;
    PERFORM corpora_session_guard.lock_verified_session(p_actor,p_session,p_token_exp);
    RETURN pg_catalog.jsonb_build_object('status','ok','project_id',p_project,'members',members);
END;
$$;


ALTER FUNCTION "corpora_session_guard"."project_members_list"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_project" "uuid", "p_after" "uuid", "p_limit" integer) OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "corpora_session_guard"."read_access"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid") RETURNS "jsonb"
    LANGUAGE "plpgsql" SECURITY DEFINER
    SET "search_path" TO ''
    SET "lock_timeout" TO '3s'
    AS $$
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
        OR (p_actor IS NOT NULL AND m.state='ready' AND EXISTS (
            SELECT 1 FROM public.corpora_corpus_readers r WHERE r.corpus_id=m.id AND r.user_id=p_actor)
            AND EXISTS (SELECT 1 FROM public.corpora_corpus_heads WHERE corpus_id=m.id))
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


ALTER FUNCTION "corpora_session_guard"."read_access"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid") OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "corpora_session_guard"."reconcile_version_save"("p_operation" "uuid", "p_claim" "uuid", "p_publication" "uuid", "p_sha" "text") RETURNS boolean
    LANGUAGE "plpgsql" SECURITY DEFINER
    SET "search_path" TO ''
    SET "lock_timeout" TO '3s'
    AS $_$
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
$_$;


ALTER FUNCTION "corpora_session_guard"."reconcile_version_save"("p_operation" "uuid", "p_claim" "uuid", "p_publication" "uuid", "p_sha" "text") OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "corpora_session_guard"."reconciliation_target"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_operation" "uuid") RETURNS "jsonb"
    LANGUAGE "plpgsql" SECURITY DEFINER
    SET "search_path" TO ''
    SET "lock_timeout" TO '3s'
    AS $$
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


ALTER FUNCTION "corpora_session_guard"."reconciliation_target"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_operation" "uuid") OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "corpora_session_guard"."require_live_corpus"() RETURNS "trigger"
    LANGUAGE "plpgsql" SECURITY DEFINER
    SET "search_path" TO ''
    AS $$
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


ALTER FUNCTION "corpora_session_guard"."require_live_corpus"() OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "corpora_session_guard"."restore_source"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_sequence" bigint) RETURNS "jsonb"
    LANGUAGE "plpgsql" SECURITY DEFINER
    SET "search_path" TO ''
    SET "lock_timeout" TO '3s'
    AS $$
DECLARE m public.corpora_corpus_metadata%ROWTYPE; result jsonb;
BEGIN
    PERFORM corpora_session_guard.lock_verified_session(p_actor,p_session,p_token_exp);
    SELECT * INTO m FROM public.corpora_corpus_metadata WHERE id=p_corpus AND (
        owner_id=p_actor OR EXISTS(SELECT 1 FROM corpora_session_guard.superadmins WHERE user_id=p_actor)
    ) FOR SHARE;
    IF NOT FOUND OR NOT EXISTS(SELECT 1 FROM public.corpora_corpus_access
        WHERE corpus_id=p_corpus AND deleted_at IS NULL) THEN
        RETURN pg_catalog.jsonb_build_object('status','not_found');
    END IF;
    SELECT pg_catalog.jsonb_build_object(
        'status','ok','commit_sha',v.commit_sha,'sequence',v.sequence,'retained_ref',v.retained_ref,
        'archive_size',s.archive_size,'archive_sha256',o.input_sha256
    ) INTO result FROM public.corpora_corpus_versions v
        JOIN public.corpora_corpus_write_ops o ON o.id=v.operation_id AND o.corpus_id=v.corpus_id
            AND o.state='completed' AND o.input_format='corpus'
        JOIN public.corpora_corpus_publication_claims p ON p.operation_id=o.id
            AND p.corpus_id=o.corpus_id AND p.publication_claim=v.publication_claim
            AND p.stage_claim=o.worker_claim
        JOIN public.corpora_corpus_staged_inputs s ON s.operation_id=o.id
            AND s.corpus_id=o.corpus_id AND s.worker_claim=o.worker_claim
            AND s.input_sha256=o.input_sha256 AND s.input_format=o.input_format
            AND s.repo_id=m.hf_repo_id AND s.repo_type=m.hf_repo_type
        WHERE v.corpus_id=m.id AND v.sequence=p_sequence;
    PERFORM corpora_session_guard.lock_verified_session(p_actor,p_session,p_token_exp);
    RETURN coalesce(result,pg_catalog.jsonb_build_object('status','not_found'));
END;
$$;


ALTER FUNCTION "corpora_session_guard"."restore_source"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_sequence" bigint) OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "corpora_session_guard"."set_visibility"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_revision" bigint, "p_visibility" "text") RETURNS "jsonb"
    LANGUAGE "plpgsql" SECURITY DEFINER
    SET "search_path" TO ''
    SET "lock_timeout" TO '3s'
    AS $$
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


ALTER FUNCTION "corpora_session_guard"."set_visibility"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_revision" bigint, "p_visibility" "text") OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "corpora_session_guard"."shared_corpora_list"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_after" "uuid", "p_limit" integer) RETURNS "jsonb"
    LANGUAGE "plpgsql" SECURITY DEFINER
    SET "search_path" TO ''
    SET "lock_timeout" TO '3s'
    AS $$
DECLARE rows jsonb;
BEGIN
    PERFORM corpora_session_guard.lock_verified_session(p_actor,p_session,p_token_exp);
    IF p_limit IS NULL OR p_limit NOT BETWEEN 1 AND 100 THEN RAISE EXCEPTION 'CORPUS_READERS_INPUT_INVALID'; END IF;
    SELECT coalesce(pg_catalog.jsonb_agg(pg_catalog.to_jsonb(item) ORDER BY item.id),'[]'::jsonb)
        INTO rows FROM (SELECT m.id,m.owner_id,m.title,m.language,m.state,m.revision,m.created_at,m.updated_at
            FROM public.corpora_corpus_metadata m JOIN public.corpora_corpus_readers r ON r.corpus_id=m.id
            JOIN public.corpora_corpus_access a ON a.corpus_id=m.id
            WHERE r.user_id=p_actor AND a.deleted_at IS NULL AND m.state='ready'
              AND EXISTS(SELECT 1 FROM public.corpora_corpus_heads h WHERE h.corpus_id=m.id)
              AND (p_after IS NULL OR m.id>p_after)
            ORDER BY m.id LIMIT p_limit+1 FOR SHARE OF m) item;
    PERFORM corpora_session_guard.lock_verified_session(p_actor,p_session,p_token_exp);
    RETURN pg_catalog.jsonb_build_object('status','list','rows',rows);
END;
$$;


ALTER FUNCTION "corpora_session_guard"."shared_corpora_list"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_after" "uuid", "p_limit" integer) OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "corpora_session_guard"."stage_import"("p_operation" "uuid", "p_claim" "uuid", "p_stage_sha" "text", "p_publication" "uuid") RETURNS boolean
    LANGUAGE "plpgsql" SECURITY DEFINER
    SET "search_path" TO ''
    SET "lock_timeout" TO '3s'
    AS $_$
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
$_$;


ALTER FUNCTION "corpora_session_guard"."stage_import"("p_operation" "uuid", "p_claim" "uuid", "p_stage_sha" "text", "p_publication" "uuid") OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "corpora_session_guard"."stage_version_save"("p_operation" "uuid", "p_claim" "uuid", "p_stage_sha" "text", "p_publication" "uuid") RETURNS boolean
    LANGUAGE "plpgsql" SECURITY DEFINER
    SET "search_path" TO ''
    SET "lock_timeout" TO '3s'
    AS $_$
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
$_$;


ALTER FUNCTION "corpora_session_guard"."stage_version_save"("p_operation" "uuid", "p_claim" "uuid", "p_stage_sha" "text", "p_publication" "uuid") OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "corpora_session_guard"."uncertain_discard_target"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_revision" bigint) RETURNS "jsonb"
    LANGUAGE "plpgsql" SECURITY DEFINER
    SET "search_path" TO ''
    SET "lock_timeout" TO '3s'
    AS $$
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


ALTER FUNCTION "corpora_session_guard"."uncertain_discard_target"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_revision" bigint) OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "corpora_session_guard"."version_save_status"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_operation" "uuid") RETURNS "jsonb"
    LANGUAGE "plpgsql" SECURITY DEFINER
    SET "search_path" TO ''
    SET "lock_timeout" TO '3s'
    AS $$
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


ALTER FUNCTION "corpora_session_guard"."version_save_status"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_operation" "uuid") OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."corpora_add_corpus_reader_by_email"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_email" "text") RETURNS "jsonb"
    LANGUAGE "sql"
    SET "search_path" TO ''
    AS $$
    SELECT corpora_session_guard.corpus_reader_add_email(p_actor,p_session,p_token_exp,p_corpus,p_email);
$$;


ALTER FUNCTION "public"."corpora_add_corpus_reader_by_email"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_email" "text") OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."corpora_add_project_member_by_email"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_project" "uuid", "p_email" "text") RETURNS "jsonb"
    LANGUAGE "sql"
    SET "search_path" TO ''
    AS $$
    SELECT corpora_session_guard.project_member_add_email(p_actor,p_session,p_token_exp,p_project,p_email);
$$;


ALTER FUNCTION "public"."corpora_add_project_member_by_email"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_project" "uuid", "p_email" "text") OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."corpora_admit_project_discovery"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_project" "uuid", "p_discovery" "uuid", "p_reason" "text", "p_evidence" "jsonb") RETURNS "jsonb"
    LANGUAGE "sql"
    SET "search_path" TO ''
    AS $$
    SELECT corpora_session_guard.project_discovery_admit(p_actor,p_session,p_token_exp,p_project,p_discovery,p_reason,p_evidence);
$$;


ALTER FUNCTION "public"."corpora_admit_project_discovery"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_project" "uuid", "p_discovery" "uuid", "p_reason" "text", "p_evidence" "jsonb") OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."corpora_begin_corpus_deletion"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_operation" "uuid", "p_revision" bigint) RETURNS "jsonb"
    LANGUAGE "sql"
    SET "search_path" TO ''
    AS $$
 SELECT corpora_session_guard.begin_deletion(p_actor,p_session,p_token_exp,p_corpus,p_operation,p_revision);
$$;


ALTER FUNCTION "public"."corpora_begin_corpus_deletion"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_operation" "uuid", "p_revision" bigint) OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."corpora_begin_corpus_edit"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_operation" "uuid", "p_sequence" bigint, "p_parent" "text", "p_digest" "text", "p_size" bigint, "p_intent" "text", "p_kind" "text", "p_restore" bigint) RETURNS "jsonb"
    LANGUAGE "sql"
    SET "search_path" TO ''
    AS $$
    SELECT corpora_session_guard.begin_corpus_edit(p_actor,p_session,p_token_exp,p_corpus,p_operation,
        p_sequence,p_parent,p_digest,p_size,p_intent,p_kind,p_restore);
$$;


ALTER FUNCTION "public"."corpora_begin_corpus_edit"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_operation" "uuid", "p_sequence" bigint, "p_parent" "text", "p_digest" "text", "p_size" bigint, "p_intent" "text", "p_kind" "text", "p_restore" bigint) OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."corpora_begin_creation"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_title" "text", "p_organization" "text") RETURNS "jsonb"
    LANGUAGE "sql"
    SET "search_path" TO ''
    AS $$
    SELECT corpora_session_guard.begin_creation(
        p_actor,p_session,p_token_exp,p_corpus,p_title,p_organization);
$$;


ALTER FUNCTION "public"."corpora_begin_creation"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_title" "text", "p_organization" "text") OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."corpora_begin_import"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_operation" "uuid", "p_revision" bigint, "p_digest" "text", "p_size" bigint) RETURNS "jsonb"
    LANGUAGE "sql"
    SET "search_path" TO ''
    AS $$
    SELECT corpora_session_guard.begin_import(
        p_actor,p_session,p_token_exp,p_corpus,p_operation,p_revision,p_digest,p_size);
$$;


ALTER FUNCTION "public"."corpora_begin_import"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_operation" "uuid", "p_revision" bigint, "p_digest" "text", "p_size" bigint) OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."corpora_begin_uncertain_discard"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_operation" "uuid", "p_revision" bigint, "p_observed_sha" "text") RETURNS "jsonb"
    LANGUAGE "sql"
    SET "search_path" TO ''
    AS $$
    SELECT corpora_session_guard.begin_uncertain_discard(
        p_actor,p_session,p_token_exp,p_corpus,p_operation,p_revision,p_observed_sha);
$$;


ALTER FUNCTION "public"."corpora_begin_uncertain_discard"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_operation" "uuid", "p_revision" bigint, "p_observed_sha" "text") OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."corpora_begin_version_save"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_operation" "uuid", "p_sequence" bigint, "p_parent" "text", "p_digest" "text", "p_size" bigint) RETURNS "jsonb"
    LANGUAGE "sql"
    SET "search_path" TO ''
    AS $$
    SELECT corpora_session_guard.begin_version_save(
        p_actor,p_session,p_token_exp,p_corpus,p_operation,p_sequence,p_parent,p_digest,p_size);
$$;


ALTER FUNCTION "public"."corpora_begin_version_save"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_operation" "uuid", "p_sequence" bigint, "p_parent" "text", "p_digest" "text", "p_size" bigint) OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."corpora_complete_import"("p_operation" "uuid", "p_claim" "uuid", "p_publication" "uuid", "p_sha" "text") RETURNS boolean
    LANGUAGE "sql"
    SET "search_path" TO ''
    AS $$
    SELECT corpora_session_guard.complete_import(p_operation,p_claim,p_publication,p_sha);
$$;


ALTER FUNCTION "public"."corpora_complete_import"("p_operation" "uuid", "p_claim" "uuid", "p_publication" "uuid", "p_sha" "text") OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."corpora_complete_version_save"("p_operation" "uuid", "p_claim" "uuid", "p_publication" "uuid", "p_sha" "text") RETURNS boolean
    LANGUAGE "sql"
    SET "search_path" TO ''
    AS $$
    SELECT corpora_session_guard.complete_version_save(p_operation,p_claim,p_publication,p_sha);
$$;


ALTER FUNCTION "public"."corpora_complete_version_save"("p_operation" "uuid", "p_claim" "uuid", "p_publication" "uuid", "p_sha" "text") OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."corpora_corpus_bootstrap_finalized"() RETURNS "trigger"
    LANGUAGE "plpgsql"
    SET "search_path" TO ''
    AS $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM public.corpora_corpus_provision_ops p
                   WHERE p.id = NEW.operation_id AND p.corpus_id = NEW.corpus_id
                     AND p.worker_claim = NEW.worker_claim AND p.state = 'completed') THEN
        RAISE EXCEPTION 'CORPUS_BOOTSTRAP_NOT_FINALIZED';
    END IF;
    RETURN NULL;
END;
$$;


ALTER FUNCTION "public"."corpora_corpus_bootstrap_finalized"() OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."corpora_corpus_creation_input"() RETURNS "trigger"
    LANGUAGE "plpgsql"
    SET "search_path" TO ''
    AS $$
BEGIN
    IF TG_OP='INSERT' THEN
        SELECT title INTO NEW.creation_title FROM public.corpora_corpus_metadata WHERE id=NEW.corpus_id;
    ELSIF NEW.creation_title IS DISTINCT FROM OLD.creation_title THEN
        RAISE EXCEPTION 'CORPUS_CREATION_INPUT_IMMUTABLE';
    END IF;
    RETURN NEW;
END;
$$;


ALTER FUNCTION "public"."corpora_corpus_creation_input"() OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."corpora_corpus_metadata_stamp"() RETURNS "trigger"
    LANGUAGE "plpgsql"
    SET "search_path" TO ''
    AS $$
BEGIN
    IF NEW.id IS DISTINCT FROM OLD.id OR NEW.owner_id IS DISTINCT FROM OLD.owner_id
       OR NEW.hf_repo_id IS DISTINCT FROM OLD.hf_repo_id
       OR NEW.hf_repo_type IS DISTINCT FROM OLD.hf_repo_type
       OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
        RAISE EXCEPTION 'CORPUS_IDENTITY_IMMUTABLE';
    END IF;
    NEW.revision := OLD.revision + 1;
    NEW.updated_at := clock_timestamp();
    RETURN NEW;
END;
$$;


ALTER FUNCTION "public"."corpora_corpus_metadata_stamp"() OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."corpora_corpus_preserve_history"() RETURNS "trigger"
    LANGUAGE "plpgsql"
    SET "search_path" TO ''
    AS $$
BEGIN
    RAISE EXCEPTION 'CORPUS_HISTORY_IMMUTABLE';
END;
$$;


ALTER FUNCTION "public"."corpora_corpus_preserve_history"() OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."corpora_corpus_validate_bootstrap"() RETURNS "trigger"
    LANGUAGE "plpgsql"
    SET "search_path" TO ''
    AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM public.corpora_corpus_provision_ops p
        JOIN public.corpora_corpus_metadata m ON m.id = p.corpus_id
        WHERE p.id = NEW.operation_id AND p.corpus_id = NEW.corpus_id
          AND p.worker_claim = NEW.worker_claim AND p.state IN ('executing', 'uncertain')
          AND m.state = 'provisioning' AND m.hf_repo_id = p.repo_id AND m.hf_repo_type = p.repo_type
          AND NOT EXISTS (SELECT 1 FROM public.corpora_corpus_heads h WHERE h.corpus_id = m.id)
    ) THEN
        RAISE EXCEPTION 'CORPUS_BOOTSTRAP_OPERATION_INVALID';
    END IF;
    RETURN NEW;
END;
$$;


ALTER FUNCTION "public"."corpora_corpus_validate_bootstrap"() OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."corpora_corpus_validate_finalized_version"() RETURNS "trigger"
    LANGUAGE "plpgsql"
    SET "search_path" TO ''
    AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM public.corpora_corpus_write_ops o
        JOIN public.corpora_corpus_heads h ON h.corpus_id = o.corpus_id
        WHERE o.id = NEW.operation_id AND o.corpus_id = NEW.corpus_id
          AND o.state = 'completed' AND h.sequence >= NEW.sequence
    ) THEN
        RAISE EXCEPTION 'CORPUS_VERSION_NOT_FINALIZED';
    END IF;
    RETURN NULL;
END;
$$;


ALTER FUNCTION "public"."corpora_corpus_validate_finalized_version"() OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."corpora_corpus_validate_head"() RETURNS "trigger"
    LANGUAGE "plpgsql"
    SET "search_path" TO ''
    AS $$
DECLARE
    actual_parent text;
BEGIN
    SELECT parent_sha INTO actual_parent FROM public.corpora_corpus_versions
        WHERE corpus_id = NEW.corpus_id AND sequence = NEW.sequence AND commit_sha = NEW.commit_sha;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'CORPUS_HEAD_VERSION_INVALID';
    END IF;
    IF TG_OP = 'INSERT' THEN
        IF NEW.sequence <> 1 OR actual_parent IS NOT NULL THEN
            RAISE EXCEPTION 'CORPUS_HEAD_VERSION_INVALID';
        END IF;
    ELSIF NEW.corpus_id IS DISTINCT FROM OLD.corpus_id OR NEW.sequence <> OLD.sequence + 1
          OR actual_parent IS DISTINCT FROM OLD.commit_sha THEN
        RAISE EXCEPTION 'CORPUS_HEAD_VERSION_INVALID';
    END IF;
    RETURN NEW;
END;
$$;


ALTER FUNCTION "public"."corpora_corpus_validate_head"() OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."corpora_corpus_validate_operation"() RETURNS "trigger"
    LANGUAGE "plpgsql"
    SET "search_path" TO ''
    AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.state <> 'admitted' OR NEW.worker_claim IS NOT NULL OR NEW.claimed_at IS NOT NULL THEN
            RAISE EXCEPTION 'CORPUS_OPERATION_STATE_INVALID';
        END IF;
        RETURN NEW;
    END IF;
    IF NEW.id IS DISTINCT FROM OLD.id OR NEW.corpus_id IS DISTINCT FROM OLD.corpus_id
       OR NEW.actor_id IS DISTINCT FROM OLD.actor_id OR NEW.session_id IS DISTINCT FROM OLD.session_id
       OR NEW.expected_sequence IS DISTINCT FROM OLD.expected_sequence
       OR NEW.expected_parent_sha IS DISTINCT FROM OLD.expected_parent_sha
       OR NEW.input_sha256 IS DISTINCT FROM OLD.input_sha256
       OR NEW.input_format IS DISTINCT FROM OLD.input_format
       OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
        RAISE EXCEPTION 'CORPUS_OPERATION_IDENTITY_IMMUTABLE';
    END IF;
    IF NOT (
        (OLD.state = 'admitted' AND NEW.state IN ('admitted', 'executing'))
        OR (OLD.state = 'executing' AND NEW.state IN ('executing', 'uncertain', 'completed', 'failed'))
        OR (OLD.state = 'uncertain' AND NEW.state IN ('uncertain', 'completed', 'failed'))
        OR (OLD.state IN ('completed', 'failed') AND NEW.state = OLD.state)
    ) THEN
        RAISE EXCEPTION 'CORPUS_OPERATION_STATE_INVALID';
    END IF;
    IF OLD.state = 'admitted' AND NEW.state = 'executing' THEN
        IF NEW.worker_claim IS NULL OR NEW.claimed_at IS NOT NULL THEN
            RAISE EXCEPTION 'CORPUS_WORKER_CLAIM_INVALID';
        END IF;
        NEW.claimed_at := clock_timestamp();
    ELSIF NEW.worker_claim IS DISTINCT FROM OLD.worker_claim
          OR NEW.claimed_at IS DISTINCT FROM OLD.claimed_at THEN
        RAISE EXCEPTION 'CORPUS_WORKER_CLAIM_IMMUTABLE';
    END IF;
    IF NEW.state = 'completed' AND OLD.state <> 'completed' AND NOT EXISTS (
        SELECT 1 FROM public.corpora_corpus_versions v
        JOIN public.corpora_corpus_heads h ON h.corpus_id = v.corpus_id
            AND h.sequence = v.sequence AND h.commit_sha = v.commit_sha
        WHERE v.operation_id = NEW.id
    ) THEN
        RAISE EXCEPTION 'CORPUS_OPERATION_RESULT_MISSING';
    END IF;
    IF NEW.state = 'failed' AND EXISTS (
        SELECT 1 FROM public.corpora_corpus_versions v WHERE v.operation_id = NEW.id
    ) THEN
        RAISE EXCEPTION 'CORPUS_OPERATION_RESULT_EXISTS';
    END IF;
    NEW.updated_at := clock_timestamp();
    RETURN NEW;
END;
$$;


ALTER FUNCTION "public"."corpora_corpus_validate_operation"() OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."corpora_corpus_validate_provision"() RETURNS "trigger"
    LANGUAGE "plpgsql"
    SET "search_path" TO ''
    AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.state <> 'admitted' OR NEW.worker_claim IS NOT NULL OR NEW.claimed_at IS NOT NULL
           OR NOT EXISTS (
               SELECT 1 FROM public.corpora_corpus_metadata m
               WHERE m.id = NEW.corpus_id AND m.state = 'provisioning'
                 AND m.hf_repo_id = NEW.repo_id AND m.hf_repo_type = NEW.repo_type
           ) THEN
            RAISE EXCEPTION 'CORPUS_PROVISION_INVALID';
        END IF;
        RETURN NEW;
    END IF;
    IF NEW.id IS DISTINCT FROM OLD.id OR NEW.corpus_id IS DISTINCT FROM OLD.corpus_id
       OR NEW.actor_id IS DISTINCT FROM OLD.actor_id OR NEW.session_id IS DISTINCT FROM OLD.session_id
       OR NEW.repo_id IS DISTINCT FROM OLD.repo_id OR NEW.repo_type IS DISTINCT FROM OLD.repo_type
       OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
        RAISE EXCEPTION 'CORPUS_PROVISION_IDENTITY_IMMUTABLE';
    END IF;
    IF NOT ((OLD.state = 'admitted' AND NEW.state IN ('admitted', 'executing'))
        OR (OLD.state = 'executing' AND NEW.state IN ('executing', 'uncertain', 'completed'))
        OR (OLD.state = 'uncertain' AND NEW.state IN ('uncertain', 'completed'))
        OR (OLD.state = 'completed' AND NEW.state = 'completed')) THEN
        RAISE EXCEPTION 'CORPUS_PROVISION_STATE_INVALID';
    END IF;
    IF OLD.state = 'admitted' AND NEW.state = 'executing' THEN
        IF NEW.worker_claim IS NULL OR NEW.claimed_at IS NOT NULL THEN
            RAISE EXCEPTION 'CORPUS_PROVISION_CLAIM_INVALID';
        END IF;
        NEW.claimed_at := clock_timestamp();
    ELSIF NEW.worker_claim IS DISTINCT FROM OLD.worker_claim
          OR NEW.claimed_at IS DISTINCT FROM OLD.claimed_at THEN
        RAISE EXCEPTION 'CORPUS_PROVISION_CLAIM_IMMUTABLE';
    END IF;
    IF NEW.state = 'completed' AND NOT EXISTS (
        SELECT 1 FROM public.corpora_corpus_bootstrap b
        WHERE b.operation_id = NEW.id AND b.corpus_id = NEW.corpus_id
          AND b.worker_claim = NEW.worker_claim
    ) THEN
        RAISE EXCEPTION 'CORPUS_PROVISION_RESULT_MISSING';
    END IF;
    RETURN NEW;
END;
$$;


ALTER FUNCTION "public"."corpora_corpus_validate_provision"() OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."corpora_corpus_validate_publication_claim"() RETURNS "trigger"
    LANGUAGE "plpgsql"
    SET "search_path" TO ''
    AS $$
BEGIN
    PERFORM 1 FROM public.corpora_corpus_metadata WHERE id = NEW.corpus_id FOR UPDATE;
    PERFORM 1 FROM public.corpora_corpus_write_ops WHERE id = NEW.operation_id FOR UPDATE;
    IF NOT EXISTS (
        SELECT 1 FROM public.corpora_corpus_write_ops o
        JOIN public.corpora_corpus_metadata m ON m.id=o.corpus_id
        JOIN public.corpora_corpus_staged_inputs s ON s.operation_id=o.id
             AND s.corpus_id=o.corpus_id AND s.worker_claim=o.worker_claim
        LEFT JOIN public.corpora_corpus_heads h ON h.corpus_id=o.corpus_id
        WHERE o.id=NEW.operation_id AND o.corpus_id=NEW.corpus_id
          AND o.worker_claim=NEW.stage_claim AND o.state='executing'
          AND ((o.expected_sequence=0 AND h.corpus_id IS NULL AND m.state='provisioning')
            OR (o.expected_sequence>0 AND m.state='ready'
                AND h.sequence=o.expected_sequence AND h.commit_sha=o.expected_parent_sha))
    ) THEN
        RAISE EXCEPTION 'CORPUS_PUBLICATION_CLAIM_INVALID';
    END IF;
    RETURN NEW;
END;
$$;


ALTER FUNCTION "public"."corpora_corpus_validate_publication_claim"() OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."corpora_corpus_validate_staged_input"() RETURNS "trigger"
    LANGUAGE "plpgsql"
    SET "search_path" TO ''
    AS $$
BEGIN
    -- Match the trusted recorder's metadata-before-operation lock order so an
    -- operation cannot leave executing while its receipt is being inserted.
    PERFORM 1 FROM public.corpora_corpus_metadata WHERE id = NEW.corpus_id FOR UPDATE;
    PERFORM 1 FROM public.corpora_corpus_write_ops WHERE id = NEW.operation_id FOR UPDATE;
    IF NOT EXISTS (
        SELECT 1 FROM public.corpora_corpus_write_ops o
        JOIN public.corpora_corpus_metadata m ON m.id = o.corpus_id
        LEFT JOIN public.corpora_corpus_bootstrap b ON b.corpus_id = o.corpus_id
        WHERE o.id = NEW.operation_id AND o.corpus_id = NEW.corpus_id
          AND o.worker_claim = NEW.worker_claim AND o.state = 'executing'
          AND o.input_sha256 = NEW.input_sha256 AND o.input_format = NEW.input_format
          AND m.hf_repo_id = NEW.repo_id AND m.hf_repo_type = NEW.repo_type
          AND NEW.requested_base_sha = CASE WHEN o.expected_sequence = 0
                                            THEN b.commit_sha ELSE o.expected_parent_sha END
    ) THEN
        RAISE EXCEPTION 'CORPUS_STAGE_RECEIPT_INVALID';
    END IF;
    RETURN NEW;
END;
$$;


ALTER FUNCTION "public"."corpora_corpus_validate_staged_input"() OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."corpora_corpus_validate_version"() RETURNS "trigger"
    LANGUAGE "plpgsql"
    SET "search_path" TO ''
    AS $$
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


ALTER FUNCTION "public"."corpora_corpus_validate_version"() OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."corpora_current_project_link"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_project" "uuid", "p_reference" "uuid") RETURNS "jsonb"
    LANGUAGE "sql"
    SET "search_path" TO ''
    AS $$
    SELECT corpora_session_guard.project_link_current(p_actor,p_session,p_token_exp,p_project,p_reference);
$$;


ALTER FUNCTION "public"."corpora_current_project_link"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_project" "uuid", "p_reference" "uuid") OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."corpora_decide_project_discovery"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_project" "uuid", "p_discovery" "uuid", "p_expected" bigint, "p_reason" "text", "p_action" "text") RETURNS "jsonb"
    LANGUAGE "sql"
    SET "search_path" TO ''
    AS $$
    SELECT corpora_session_guard.project_discovery_decide(p_actor,p_session,p_token_exp,p_project,p_discovery,p_expected,p_reason,p_action);
$$;


ALTER FUNCTION "public"."corpora_decide_project_discovery"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_project" "uuid", "p_discovery" "uuid", "p_expected" bigint, "p_reason" "text", "p_action" "text") OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."corpora_decide_project_link"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_project" "uuid", "p_reference" "uuid", "p_expected" bigint, "p_reason" "text", "p_action" "text") RETURNS "jsonb"
    LANGUAGE "sql"
    SET "search_path" TO ''
    AS $$
    SELECT corpora_session_guard.project_link_decide(
        p_actor,p_session,p_token_exp,p_project,p_reference,p_expected,p_reason,p_action);
$$;


ALTER FUNCTION "public"."corpora_decide_project_link"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_project" "uuid", "p_reference" "uuid", "p_expected" bigint, "p_reason" "text", "p_action" "text") OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."corpora_directory_reader"() RETURNS boolean
    LANGUAGE "plpgsql" SECURITY DEFINER
    SET "search_path" TO ''
    AS $$
DECLARE actor uuid:=auth.uid(); claims jsonb:=auth.jwt(); session_id uuid; token_exp bigint;
BEGIN
    BEGIN
        session_id:=(claims->>'session_id')::uuid;
        token_exp:=(claims->>'exp')::bigint;
    EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range THEN
        RAISE EXCEPTION 'AUTH_SESSION_INVALID';
    END;
    IF actor IS NULL OR session_id IS NULL OR token_exp IS NULL
       OR token_exp<=extract(epoch FROM pg_catalog.clock_timestamp())
       OR NOT EXISTS(SELECT 1 FROM auth.sessions s WHERE s.id=session_id AND s.user_id=actor
           AND (s.not_after IS NULL OR s.not_after>pg_catalog.clock_timestamp())) THEN
        RAISE EXCEPTION 'AUTH_SESSION_INVALID';
    END IF;
    RETURN true;
END;
$$;


ALTER FUNCTION "public"."corpora_directory_reader"() OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."corpora_export_project_links"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_project" "uuid", "p_references" "uuid"[]) RETURNS "jsonb"
    LANGUAGE "sql"
    SET "search_path" TO ''
    AS $$
    SELECT corpora_session_guard.project_link_export(p_actor,p_session,p_token_exp,p_project,p_references);
$$;


ALTER FUNCTION "public"."corpora_export_project_links"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_project" "uuid", "p_references" "uuid"[]) OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."corpora_finalize_creation"("p_corpus" "uuid", "p_operation" "uuid", "p_claim" "uuid", "p_repo" "text", "p_type" "text", "p_sha" "text") RETURNS boolean
    LANGUAGE "sql"
    SET "search_path" TO ''
    AS $$
    SELECT corpora_session_guard.finalize_provision(
        p_corpus,p_operation,p_claim,p_repo,p_type,p_sha);
$$;


ALTER FUNCTION "public"."corpora_finalize_creation"("p_corpus" "uuid", "p_operation" "uuid", "p_claim" "uuid", "p_repo" "text", "p_type" "text", "p_sha" "text") OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."corpora_find_creation_result"("p_corpus" "uuid", "p_operation" "uuid", "p_claim" "uuid", "p_repo" "text", "p_type" "text", "p_sha" "text") RETURNS "text"
    LANGUAGE "sql"
    SET "search_path" TO ''
    AS $$
    SELECT b.commit_sha FROM public.corpora_corpus_bootstrap b
    JOIN public.corpora_corpus_provision_ops o ON o.id=b.operation_id
    JOIN public.corpora_corpus_metadata m ON m.id=b.corpus_id
    WHERE b.corpus_id=p_corpus AND b.operation_id=p_operation AND b.worker_claim=p_claim
        AND b.commit_sha=p_sha AND o.corpus_id=b.corpus_id AND o.worker_claim=b.worker_claim
        AND o.repo_id=p_repo AND o.repo_type=p_type AND o.state='completed'
        AND m.hf_repo_id=p_repo AND m.hf_repo_type=p_type;
$$;


ALTER FUNCTION "public"."corpora_find_creation_result"("p_corpus" "uuid", "p_operation" "uuid", "p_claim" "uuid", "p_repo" "text", "p_type" "text", "p_sha" "text") OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."corpora_find_import_result"("p_operation" "uuid", "p_claim" "uuid", "p_publication" "uuid", "p_sha" "text") RETURNS boolean
    LANGUAGE "sql"
    SET "search_path" TO ''
    AS $$
    SELECT EXISTS (SELECT 1 FROM public.corpora_corpus_write_ops o
        JOIN public.corpora_corpus_versions v ON v.operation_id=o.id AND v.corpus_id=o.corpus_id
        WHERE o.id=p_operation AND o.worker_claim=p_claim AND o.state='completed'
            AND v.publication_claim=p_publication AND v.commit_sha=p_sha AND v.sequence=1);
$$;


ALTER FUNCTION "public"."corpora_find_import_result"("p_operation" "uuid", "p_claim" "uuid", "p_publication" "uuid", "p_sha" "text") OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."corpora_find_version_save_result"("p_operation" "uuid", "p_claim" "uuid", "p_publication" "uuid", "p_sha" "text") RETURNS boolean
    LANGUAGE "sql"
    SET "search_path" TO ''
    AS $$
    SELECT EXISTS(SELECT 1 FROM public.corpora_corpus_write_ops o
        JOIN public.corpora_corpus_versions v ON v.operation_id=o.id AND v.corpus_id=o.corpus_id
        WHERE o.id=p_operation AND o.expected_sequence>0 AND o.worker_claim=p_claim AND o.state='completed'
            AND v.publication_claim=p_publication AND v.commit_sha=p_sha AND v.sequence=o.expected_sequence+1);
$$;


ALTER FUNCTION "public"."corpora_find_version_save_result"("p_operation" "uuid", "p_claim" "uuid", "p_publication" "uuid", "p_sha" "text") OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."corpora_finish_corpus_deletion"("p_operation" "uuid", "p_claim" "uuid", "p_completed" boolean) RETURNS boolean
    LANGUAGE "sql"
    SET "search_path" TO ''
    AS $$
 SELECT corpora_session_guard.finish_deletion(p_operation,p_claim,p_completed);
$$;


ALTER FUNCTION "public"."corpora_finish_corpus_deletion"("p_operation" "uuid", "p_claim" "uuid", "p_completed" boolean) OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."corpora_finish_save_reconciliation"("p_operation" "uuid", "p_claim" "uuid", "p_publication" "uuid", "p_sha" "text") RETURNS boolean
    LANGUAGE "sql"
    SET "search_path" TO ''
    AS $$
    SELECT corpora_session_guard.reconcile_version_save(p_operation,p_claim,p_publication,p_sha);
$$;


ALTER FUNCTION "public"."corpora_finish_save_reconciliation"("p_operation" "uuid", "p_claim" "uuid", "p_publication" "uuid", "p_sha" "text") OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."corpora_get_corpus_deletion"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_operation" "uuid") RETURNS "jsonb"
    LANGUAGE "sql"
    SET "search_path" TO ''
    AS $$
 SELECT corpora_session_guard.deletion_status(p_actor,p_session,p_token_exp,p_corpus,p_operation);
$$;


ALTER FUNCTION "public"."corpora_get_corpus_deletion"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_operation" "uuid") OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."corpora_get_creation"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid") RETURNS "jsonb"
    LANGUAGE "sql"
    SET "search_path" TO ''
    AS $$
    SELECT corpora_session_guard.creation_status(p_actor,p_session,p_token_exp,p_corpus);
$$;


ALTER FUNCTION "public"."corpora_get_creation"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid") OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."corpora_get_edit_intent"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_operation" "uuid", "p_intent" "text") RETURNS "jsonb"
    LANGUAGE "sql"
    SET "search_path" TO ''
    AS $$
    SELECT corpora_session_guard.edit_intent_status(
        p_actor,p_session,p_token_exp,p_corpus,p_operation,p_intent);
$$;


ALTER FUNCTION "public"."corpora_get_edit_intent"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_operation" "uuid", "p_intent" "text") OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."corpora_get_import"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_operation" "uuid") RETURNS "jsonb"
    LANGUAGE "sql"
    SET "search_path" TO ''
    AS $$
    SELECT corpora_session_guard.import_status(p_actor,p_session,p_token_exp,p_corpus,p_operation);
$$;


ALTER FUNCTION "public"."corpora_get_import"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_operation" "uuid") OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."corpora_get_import_reconciliation"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_operation" "uuid") RETURNS "jsonb"
    LANGUAGE "sql"
    SET "search_path" TO ''
    AS $$
    SELECT corpora_session_guard.import_reconciliation_target(p_actor,p_session,p_token_exp,p_corpus,p_operation);
$$;


ALTER FUNCTION "public"."corpora_get_import_reconciliation"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_operation" "uuid") OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."corpora_get_metadata"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid") RETURNS "jsonb"
    LANGUAGE "sql"
    SET "search_path" TO ''
    AS $$
    SELECT corpora_session_guard.get_metadata(p_actor,p_session,p_token_exp,p_corpus);
$$;


ALTER FUNCTION "public"."corpora_get_metadata"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid") OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."corpora_get_restore_source"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_sequence" bigint) RETURNS "jsonb"
    LANGUAGE "sql"
    SET "search_path" TO ''
    AS $$
    SELECT corpora_session_guard.restore_source(p_actor,p_session,p_token_exp,p_corpus,p_sequence);
$$;


ALTER FUNCTION "public"."corpora_get_restore_source"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_sequence" bigint) OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."corpora_get_save_reconciliation"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_operation" "uuid") RETURNS "jsonb"
    LANGUAGE "sql"
    SET "search_path" TO ''
    AS $$
    SELECT corpora_session_guard.reconciliation_target(p_actor,p_session,p_token_exp,p_corpus,p_operation);
$$;


ALTER FUNCTION "public"."corpora_get_save_reconciliation"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_operation" "uuid") OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."corpora_get_version_save"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_operation" "uuid") RETURNS "jsonb"
    LANGUAGE "sql"
    SET "search_path" TO ''
    AS $$
    SELECT corpora_session_guard.version_save_status(p_actor,p_session,p_token_exp,p_corpus,p_operation);
$$;


ALTER FUNCTION "public"."corpora_get_version_save"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_operation" "uuid") OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."corpora_ingest_session_active"() RETURNS boolean
    LANGUAGE "sql" STABLE SECURITY DEFINER
    SET "search_path" TO ''
    AS $_$
  select exists (
    select 1 from auth.sessions s
    where s.id = case
      when (select auth.jwt()) ->> 'session_id' ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
      then ((select auth.jwt()) ->> 'session_id')::uuid
      else null
    end
    and s.user_id = (select auth.uid())
    and (s.not_after is null or s.not_after > statement_timestamp())
  )
$_$;


ALTER FUNCTION "public"."corpora_ingest_session_active"() OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."corpora_legacy_document_editable"("p_document" "uuid") RETURNS boolean
    LANGUAGE "plpgsql" SECURITY DEFINER
    SET "search_path" TO ''
    SET "lock_timeout" TO '3s'
    AS $$
DECLARE actor uuid; owner uuid;
BEGIN
    actor := public.corpora_project_actor();
    SELECT owner_id INTO owner FROM public.corpus_documents WHERE id=p_document FOR SHARE;
    IF NOT FOUND THEN RETURN false; END IF;
    RETURN (actor IS NOT NULL AND owner=actor) OR EXISTS (
        SELECT 1 FROM corpora_session_guard.superadmins WHERE user_id=actor);
END;
$$;


ALTER FUNCTION "public"."corpora_legacy_document_editable"("p_document" "uuid") OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."corpora_legacy_storage_session_readable"() RETURNS boolean
    LANGUAGE "plpgsql" SECURITY DEFINER
    SET "search_path" TO ''
    AS $$
BEGIN
    RETURN public.corpora_directory_reader();
EXCEPTION WHEN raise_exception THEN
    IF SQLERRM='AUTH_SESSION_INVALID' THEN RETURN false; END IF;
    RAISE;
END;
$$;


ALTER FUNCTION "public"."corpora_legacy_storage_session_readable"() OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."corpora_legacy_superadmin_readable"() RETURNS boolean
    LANGUAGE "plpgsql" SECURITY DEFINER
    SET "search_path" TO ''
    AS $$
BEGIN
    IF NOT public.corpora_legacy_storage_session_readable() THEN RETURN false; END IF;
    RETURN EXISTS(SELECT 1 FROM corpora_session_guard.superadmins WHERE user_id=auth.uid());
END;
$$;


ALTER FUNCTION "public"."corpora_legacy_superadmin_readable"() OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."corpora_list_corpus_readers"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_after" "uuid", "p_limit" integer) RETURNS "jsonb"
    LANGUAGE "sql"
    SET "search_path" TO ''
    AS $$
    SELECT corpora_session_guard.corpus_readers_list(p_actor,p_session,p_token_exp,p_corpus,p_after,p_limit);
$$;


ALTER FUNCTION "public"."corpora_list_corpus_readers"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_after" "uuid", "p_limit" integer) OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."corpora_list_corpus_versions"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_before" bigint, "p_limit" integer) RETURNS "jsonb"
    LANGUAGE "sql"
    SET "search_path" TO ''
    AS $$
    SELECT corpora_session_guard.list_corpus_versions(
        p_actor,p_session,p_token_exp,p_corpus,p_before,p_limit
    );
$$;


ALTER FUNCTION "public"."corpora_list_corpus_versions"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_before" bigint, "p_limit" integer) OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."corpora_list_metadata"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_owner" "uuid", "p_after" "uuid", "p_limit" integer) RETURNS "jsonb"
    LANGUAGE "sql"
    SET "search_path" TO ''
    AS $$
    SELECT corpora_session_guard.list_metadata(p_actor,p_session,p_token_exp,p_owner,p_after,p_limit);
$$;


ALTER FUNCTION "public"."corpora_list_metadata"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_owner" "uuid", "p_after" "uuid", "p_limit" integer) OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."corpora_list_project_corpora"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_project" "uuid", "p_linked_only" boolean, "p_after" "uuid", "p_limit" integer) RETURNS "jsonb"
    LANGUAGE "sql"
    SET "search_path" TO ''
    AS $$
    SELECT corpora_session_guard.project_corpora_list(p_actor,p_session,p_token_exp,p_project,p_linked_only,p_after,p_limit);
$$;


ALTER FUNCTION "public"."corpora_list_project_corpora"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_project" "uuid", "p_linked_only" boolean, "p_after" "uuid", "p_limit" integer) OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."corpora_list_project_members"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_project" "uuid", "p_after" "uuid", "p_limit" integer) RETURNS "jsonb"
    LANGUAGE "sql"
    SET "search_path" TO ''
    AS $$
    SELECT corpora_session_guard.project_members_list(p_actor,p_session,p_token_exp,p_project,p_after,p_limit);
$$;


ALTER FUNCTION "public"."corpora_list_project_members"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_project" "uuid", "p_after" "uuid", "p_limit" integer) OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."corpora_list_shared_corpora"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_after" "uuid", "p_limit" integer) RETURNS "jsonb"
    LANGUAGE "sql"
    SET "search_path" TO ''
    AS $$
    SELECT corpora_session_guard.shared_corpora_list(p_actor,p_session,p_token_exp,p_after,p_limit);
$$;


ALTER FUNCTION "public"."corpora_list_shared_corpora"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_after" "uuid", "p_limit" integer) OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."corpora_mark_creation_uncertain"("p_operation" "uuid", "p_claim" "uuid") RETURNS boolean
    LANGUAGE "sql"
    SET "search_path" TO ''
    AS $$
    SELECT corpora_session_guard.mark_provision_uncertain(p_operation,p_claim);
$$;


ALTER FUNCTION "public"."corpora_mark_creation_uncertain"("p_operation" "uuid", "p_claim" "uuid") OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."corpora_mark_import_uncertain"("p_operation" "uuid", "p_claim" "uuid") RETURNS boolean
    LANGUAGE "sql"
    SET "search_path" TO ''
    AS $$
    SELECT corpora_session_guard.mark_import_uncertain(p_operation,p_claim);
$$;


ALTER FUNCTION "public"."corpora_mark_import_uncertain"("p_operation" "uuid", "p_claim" "uuid") OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."corpora_mark_version_save_uncertain"("p_operation" "uuid", "p_claim" "uuid") RETURNS boolean
    LANGUAGE "sql"
    SET "search_path" TO ''
    AS $$
    SELECT corpora_session_guard.mark_version_save_uncertain(p_operation,p_claim);
$$;


ALTER FUNCTION "public"."corpora_mark_version_save_uncertain"("p_operation" "uuid", "p_claim" "uuid") OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."corpora_observe_provision"("p_operation" "uuid") RETURNS "jsonb"
    LANGUAGE "sql"
    SET "search_path" TO ''
    AS $$
    SELECT pg_catalog.jsonb_build_object('operation',row_to_json(o),'metadata',row_to_json(m))
    FROM public.corpora_corpus_provision_ops o
    JOIN public.corpora_corpus_metadata m ON m.id=o.corpus_id
    WHERE o.id=p_operation AND o.state IN ('executing','uncertain')
        AND o.worker_claim IS NOT NULL;
$$;


ALTER FUNCTION "public"."corpora_observe_provision"("p_operation" "uuid") OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."corpora_patch_metadata"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_revision" bigint, "p_fields" "jsonb") RETURNS "jsonb"
    LANGUAGE "sql"
    SET "search_path" TO ''
    AS $$
    SELECT corpora_session_guard.patch_metadata(p_actor,p_session,p_token_exp,p_corpus,p_revision,p_fields);
$$;


ALTER FUNCTION "public"."corpora_patch_metadata"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_revision" bigint, "p_fields" "jsonb") OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."corpora_project_actor"() RETURNS "uuid"
    LANGUAGE "plpgsql" SECURITY DEFINER
    SET "search_path" TO ''
    AS $$
DECLARE actor uuid:=auth.uid(); claims jsonb:=auth.jwt(); session_id uuid; token_exp bigint;
BEGIN
    BEGIN
        session_id:=(claims->>'session_id')::uuid;
        token_exp:=(claims->>'exp')::bigint;
    EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range THEN
        RAISE EXCEPTION 'AUTH_SESSION_INVALID';
    END;
    PERFORM corpora_session_guard.lock_verified_session(actor,session_id,token_exp);
    RETURN actor;
END;
$$;


ALTER FUNCTION "public"."corpora_project_actor"() OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."corpora_project_association_editable"("p_project" "uuid") RETURNS boolean
    LANGUAGE "plpgsql" SECURITY DEFINER
    SET "search_path" TO ''
    SET "lock_timeout" TO '3s'
    AS $$
DECLARE actor uuid; owner uuid;
BEGIN
    actor := public.corpora_project_actor();
    SELECT owner_id INTO owner FROM public.projects WHERE id=p_project FOR SHARE;
    IF NOT FOUND THEN RETURN false; END IF;
    RETURN (actor IS NOT NULL AND owner=actor) OR EXISTS (
        SELECT 1 FROM corpora_session_guard.superadmins WHERE user_id=actor);
END;
$$;


ALTER FUNCTION "public"."corpora_project_association_editable"("p_project" "uuid") OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."corpora_project_association_readable"("p_project" "uuid") RETURNS boolean
    LANGUAGE "plpgsql" SECURITY DEFINER
    SET "search_path" TO ''
    AS $$
DECLARE actor uuid:=auth.uid(); owner uuid;
BEGIN
    IF NOT public.corpora_directory_reader() THEN RETURN false; END IF;
    SELECT owner_id INTO owner FROM public.projects WHERE id=p_project;
    IF NOT FOUND THEN RETURN false; END IF;
    RETURN (actor IS NOT NULL AND owner=actor) OR EXISTS (
        SELECT 1 FROM corpora_session_guard.superadmins WHERE user_id=actor);
END;
$$;


ALTER FUNCTION "public"."corpora_project_association_readable"("p_project" "uuid") OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."corpora_project_discovery_history"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_project" "uuid", "p_discovery" "uuid", "p_before" bigint, "p_limit" integer) RETURNS "jsonb"
    LANGUAGE "sql"
    SET "search_path" TO ''
    AS $$
    SELECT corpora_session_guard.project_discovery_history(p_actor,p_session,p_token_exp,p_project,p_discovery,p_before,p_limit);
$$;


ALTER FUNCTION "public"."corpora_project_discovery_history"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_project" "uuid", "p_discovery" "uuid", "p_before" bigint, "p_limit" integer) OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."corpora_project_link_history"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_project" "uuid", "p_reference" "uuid", "p_before" bigint, "p_limit" integer) RETURNS "jsonb"
    LANGUAGE "sql"
    SET "search_path" TO ''
    AS $$
    SELECT corpora_session_guard.project_link_history(
        p_actor,p_session,p_token_exp,p_project,p_reference,p_before,p_limit);
$$;


ALTER FUNCTION "public"."corpora_project_link_history"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_project" "uuid", "p_reference" "uuid", "p_before" bigint, "p_limit" integer) OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."corpora_project_superadmin"() RETURNS boolean
    LANGUAGE "sql" SECURITY DEFINER
    SET "search_path" TO ''
    AS $$
    SELECT EXISTS(SELECT 1 FROM corpora_session_guard.superadmins
        WHERE user_id=public.corpora_project_actor());
$$;


ALTER FUNCTION "public"."corpora_project_superadmin"() OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."corpora_read_corpus_access"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid") RETURNS "jsonb"
    LANGUAGE "sql"
    SET "search_path" TO ''
    AS $$
 SELECT corpora_session_guard.read_access(p_actor,p_session,p_token_exp,p_corpus);
$$;


ALTER FUNCTION "public"."corpora_read_corpus_access"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid") OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."corpora_read_current_project_link"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_project" "uuid", "p_reference" "uuid") RETURNS "jsonb"
    LANGUAGE "sql"
    SET "search_path" TO ''
    AS $$
    SELECT corpora_session_guard.project_link_read_current(p_actor,p_session,p_token_exp,p_project,p_reference);
$$;


ALTER FUNCTION "public"."corpora_read_current_project_link"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_project" "uuid", "p_reference" "uuid") OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."corpora_register_project_discovery"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_project" "uuid", "p_discovery" "uuid", "p_reason" "text", "p_evidence" "jsonb") RETURNS "jsonb"
    LANGUAGE "sql"
    SET "search_path" TO ''
    AS $$
    SELECT corpora_session_guard.project_discovery_register(p_actor,p_session,p_token_exp,p_project,p_discovery,p_reason,p_evidence);
$$;


ALTER FUNCTION "public"."corpora_register_project_discovery"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_project" "uuid", "p_discovery" "uuid", "p_reason" "text", "p_evidence" "jsonb") OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."corpora_save_project_link"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_project" "uuid", "p_reference" "uuid", "p_expected" bigint, "p_reason" "text", "p_value" "jsonb") RETURNS "jsonb"
    LANGUAGE "sql"
    SET "search_path" TO ''
    AS $$
    SELECT corpora_session_guard.project_link_save(
        p_actor,p_session,p_token_exp,p_project,p_reference,p_expected,p_reason,p_value);
$$;


ALTER FUNCTION "public"."corpora_save_project_link"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_project" "uuid", "p_reference" "uuid", "p_expected" bigint, "p_reason" "text", "p_value" "jsonb") OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."corpora_set_corpus_reader"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_reader" "uuid", "p_present" boolean) RETURNS "jsonb"
    LANGUAGE "sql"
    SET "search_path" TO ''
    AS $$
    SELECT corpora_session_guard.corpus_reader_set(p_actor,p_session,p_token_exp,p_corpus,p_reader,p_present);
$$;


ALTER FUNCTION "public"."corpora_set_corpus_reader"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_reader" "uuid", "p_present" boolean) OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."corpora_set_corpus_visibility"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_revision" bigint, "p_visibility" "text") RETURNS "jsonb"
    LANGUAGE "sql"
    SET "search_path" TO ''
    AS $$
 SELECT corpora_session_guard.set_visibility(p_actor,p_session,p_token_exp,p_corpus,p_revision,p_visibility);
$$;


ALTER FUNCTION "public"."corpora_set_corpus_visibility"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_revision" bigint, "p_visibility" "text") OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."corpora_set_project_corpus"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_project" "uuid", "p_corpus" "uuid", "p_present" boolean) RETURNS "jsonb"
    LANGUAGE "sql"
    SET "search_path" TO ''
    AS $$
    SELECT corpora_session_guard.project_corpus_set(p_actor,p_session,p_token_exp,p_project,p_corpus,p_present);
$$;


ALTER FUNCTION "public"."corpora_set_project_corpus"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_project" "uuid", "p_corpus" "uuid", "p_present" boolean) OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."corpora_set_project_member"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_project" "uuid", "p_member" "uuid", "p_present" boolean) RETURNS "jsonb"
    LANGUAGE "sql"
    SET "search_path" TO ''
    AS $$
    SELECT corpora_session_guard.project_member_set(p_actor,p_session,p_token_exp,p_project,p_member,p_present);
$$;


ALTER FUNCTION "public"."corpora_set_project_member"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_project" "uuid", "p_member" "uuid", "p_present" boolean) OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."corpora_stage_import"("p_operation" "uuid", "p_claim" "uuid", "p_stage_sha" "text", "p_publication" "uuid") RETURNS boolean
    LANGUAGE "sql"
    SET "search_path" TO ''
    AS $$
    SELECT corpora_session_guard.stage_import(p_operation,p_claim,p_stage_sha,p_publication);
$$;


ALTER FUNCTION "public"."corpora_stage_import"("p_operation" "uuid", "p_claim" "uuid", "p_stage_sha" "text", "p_publication" "uuid") OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."corpora_stage_version_save"("p_operation" "uuid", "p_claim" "uuid", "p_stage_sha" "text", "p_publication" "uuid") RETURNS boolean
    LANGUAGE "sql"
    SET "search_path" TO ''
    AS $$
    SELECT corpora_session_guard.stage_version_save(p_operation,p_claim,p_stage_sha,p_publication);
$$;


ALTER FUNCTION "public"."corpora_stage_version_save"("p_operation" "uuid", "p_claim" "uuid", "p_stage_sha" "text", "p_publication" "uuid") OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."corpora_uncertain_discard_status"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_operation" "uuid", "p_revision" bigint) RETURNS "jsonb"
    LANGUAGE "plpgsql" SECURITY DEFINER
    SET "search_path" TO ''
    SET "lock_timeout" TO '3s'
    AS $$
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


ALTER FUNCTION "public"."corpora_uncertain_discard_status"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_operation" "uuid", "p_revision" bigint) OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."corpora_uncertain_discard_target"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_revision" bigint) RETURNS "jsonb"
    LANGUAGE "sql"
    SET "search_path" TO ''
    AS $$
    SELECT corpora_session_guard.uncertain_discard_target(p_actor,p_session,p_token_exp,p_corpus,p_revision);
$$;


ALTER FUNCTION "public"."corpora_uncertain_discard_target"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_revision" bigint) OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."corpora_version_save_base"("p_operation" "uuid", "p_claim" "uuid") RETURNS "jsonb"
    LANGUAGE "sql"
    SET "search_path" TO ''
    AS $$
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


ALTER FUNCTION "public"."corpora_version_save_base"("p_operation" "uuid", "p_claim" "uuid") OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."handle_new_user"() RETURNS "trigger"
    LANGUAGE "plpgsql" SECURITY DEFINER
    SET "search_path" TO ''
    AS $$
begin
  insert into public.users (id, display_name, user_type)
  values (
    new.id,
    coalesce(new.raw_user_meta_data ->> 'display_name', 'Guest'),
    case when new.is_anonymous then 'anonymous' else 'registered' end
  );
  return new;
end;
$$;


ALTER FUNCTION "public"."handle_new_user"() OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."rls_auto_enable"() RETURNS "event_trigger"
    LANGUAGE "plpgsql" SECURITY DEFINER
    SET "search_path" TO 'pg_catalog'
    AS $$
DECLARE
  cmd record;
BEGIN
  FOR cmd IN
    SELECT *
    FROM pg_event_trigger_ddl_commands()
    WHERE command_tag IN ('CREATE TABLE', 'CREATE TABLE AS', 'SELECT INTO')
      AND object_type IN ('table','partitioned table')
  LOOP
     IF cmd.schema_name IS NOT NULL AND cmd.schema_name IN ('public') AND cmd.schema_name NOT IN ('pg_catalog','information_schema') AND cmd.schema_name NOT LIKE 'pg_toast%' AND cmd.schema_name NOT LIKE 'pg_temp%' THEN
      BEGIN
        EXECUTE format('alter table if exists %s enable row level security', cmd.object_identity);
        RAISE LOG 'rls_auto_enable: enabled RLS on %', cmd.object_identity;
      EXCEPTION
        WHEN OTHERS THEN
          RAISE LOG 'rls_auto_enable: failed to enable RLS on %', cmd.object_identity;
      END;
     ELSE
        RAISE LOG 'rls_auto_enable: skip % (either system schema or not in enforced list: %.)', cmd.object_identity, cmd.schema_name;
     END IF;
  END LOOP;
END;
$$;


ALTER FUNCTION "public"."rls_auto_enable"() OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."set_updated_at"() RETURNS "trigger"
    LANGUAGE "plpgsql"
    SET "search_path" TO ''
    AS $$
begin
  new.updated_at := now();
  return new;
end;
$$;


ALTER FUNCTION "public"."set_updated_at"() OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."touch_updated_at"() RETURNS "trigger"
    LANGUAGE "plpgsql"
    AS $$
begin
  new.updated_at = now();
  return new;
end $$;


ALTER FUNCTION "public"."touch_updated_at"() OWNER TO "postgres";

SET default_tablespace = '';

SET default_table_access_method = "heap";


CREATE TABLE IF NOT EXISTS "corpora_session_guard"."superadmins" (
    "user_id" "uuid" NOT NULL
);


ALTER TABLE "corpora_session_guard"."superadmins" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."addresses" (
    "id" "uuid" DEFAULT "gen_random_uuid"() NOT NULL,
    "street" "text" NOT NULL,
    "suite" "text",
    "city" "text" NOT NULL,
    "zipcode" "text" NOT NULL,
    "country" "text" NOT NULL,
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "updated_at" timestamp with time zone
);


ALTER TABLE "public"."addresses" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."authors" (
    "id" "uuid" DEFAULT "gen_random_uuid"() NOT NULL,
    "name" "text" NOT NULL,
    "first_name" "text",
    "last_name" "text",
    "period" "text",
    "date_of_birth" "text",
    "date_of_death" "text",
    "origin" "text",
    "biography" "text",
    "image_url" "text",
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "updated_at" timestamp with time zone
);


ALTER TABLE "public"."authors" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."books" (
    "id" "text" NOT NULL,
    "title" "text",
    "description" "text",
    "type" "text",
    "language" "text",
    "period" "text",
    "category" "text" DEFAULT '[]'::"text" NOT NULL,
    "licence" "text",
    "credits" "text",
    "corpus_id" "uuid"
);


ALTER TABLE "public"."books" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."conversion_jobs" (
    "id" "text" NOT NULL,
    "source_format" "text" NOT NULL,
    "name" "text" NOT NULL,
    "status" "text" NOT NULL,
    "created_at" double precision NOT NULL,
    "started_at" double precision,
    "finished_at" double precision,
    "result_key" "text",
    "error" "text",
    "logs" "jsonb" DEFAULT '[]'::"jsonb" NOT NULL,
    "owner" "text",
    "display_name" "text",
    "validation" "jsonb",
    "output_format" "text" DEFAULT 'corpus'::"text" NOT NULL
);


ALTER TABLE "public"."conversion_jobs" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."corpora" (
    "id" "uuid" DEFAULT "gen_random_uuid"() NOT NULL,
    "uid" "text" NOT NULL,
    "name" "text" NOT NULL,
    "description" "text",
    "version" "text" DEFAULT '0.0.0'::"text" NOT NULL,
    "language" "text",
    "language_code" "text",
    "type" "text",
    "category" "text",
    "hf_path" "text",
    "available" boolean DEFAULT true NOT NULL,
    "owner_id" "uuid",
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "updated_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "author_id" "uuid"
);


ALTER TABLE "public"."corpora" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."corpora_corpus_access" (
    "corpus_id" "uuid" NOT NULL,
    "visibility" "text" DEFAULT 'private'::"text" NOT NULL,
    "deleted_at" timestamp with time zone,
    CONSTRAINT "corpora_corpus_access_check" CHECK ((("deleted_at" IS NULL) OR ("visibility" = 'private'::"text"))),
    CONSTRAINT "corpora_corpus_access_visibility_check" CHECK (("visibility" = ANY (ARRAY['private'::"text", 'public'::"text"])))
);


ALTER TABLE "public"."corpora_corpus_access" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."corpora_corpus_bootstrap" (
    "corpus_id" "uuid" NOT NULL,
    "operation_id" "uuid" NOT NULL,
    "worker_claim" "uuid" NOT NULL,
    "commit_sha" "text" NOT NULL,
    "created_at" timestamp with time zone DEFAULT "clock_timestamp"() NOT NULL,
    CONSTRAINT "corpora_corpus_bootstrap_commit_sha_check" CHECK (("commit_sha" ~ '^[0-9a-f]{40}$'::"text"))
);


ALTER TABLE "public"."corpora_corpus_bootstrap" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."corpora_corpus_deletions" (
    "id" "uuid" NOT NULL,
    "corpus_id" "uuid" NOT NULL,
    "actor_id" "uuid" NOT NULL,
    "expected_revision" bigint NOT NULL,
    "worker_claim" "uuid" NOT NULL,
    "state" "text" DEFAULT 'executing'::"text" NOT NULL,
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "updated_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    CONSTRAINT "corpora_corpus_deletions_expected_revision_check" CHECK (("expected_revision" > 0)),
    CONSTRAINT "corpora_corpus_deletions_state_check" CHECK (("state" = ANY (ARRAY['executing'::"text", 'uncertain'::"text", 'completed'::"text"])))
);


ALTER TABLE "public"."corpora_corpus_deletions" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."corpora_corpus_heads" (
    "corpus_id" "uuid" NOT NULL,
    "sequence" bigint NOT NULL,
    "commit_sha" "text" NOT NULL,
    CONSTRAINT "corpora_corpus_heads_sequence_check" CHECK (("sequence" > 0))
);


ALTER TABLE "public"."corpora_corpus_heads" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."corpora_corpus_imports" (
    "operation_id" "uuid" NOT NULL,
    "corpus_id" "uuid" NOT NULL,
    "metadata_revision" bigint NOT NULL,
    "archive_size" bigint NOT NULL,
    CONSTRAINT "corpora_corpus_imports_archive_size_check" CHECK ((("archive_size" >= 1) AND ("archive_size" <= 3000000))),
    CONSTRAINT "corpora_corpus_imports_metadata_revision_check" CHECK (("metadata_revision" > 0))
);


ALTER TABLE "public"."corpora_corpus_imports" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."corpora_corpus_metadata" (
    "id" "uuid" NOT NULL,
    "owner_id" "uuid" NOT NULL,
    "title" "text" NOT NULL,
    "description" "text",
    "language" "text",
    "properties" "jsonb" DEFAULT '{}'::"jsonb" NOT NULL,
    "hf_repo_id" "text" NOT NULL,
    "hf_repo_type" "text" NOT NULL,
    "state" "text" DEFAULT 'provisioning'::"text" NOT NULL,
    "revision" bigint DEFAULT 1 NOT NULL,
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "updated_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    CONSTRAINT "corpora_corpus_metadata_description_check" CHECK ((("description" IS NULL) OR ("length"("description") <= 10000))),
    CONSTRAINT "corpora_corpus_metadata_hf_repo_id_check" CHECK ((("length"("hf_repo_id") <= 193) AND ("hf_repo_id" ~ '^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$'::"text"))),
    CONSTRAINT "corpora_corpus_metadata_hf_repo_type_check" CHECK (("hf_repo_type" = ANY (ARRAY['dataset'::"text", 'model'::"text"]))),
    CONSTRAINT "corpora_corpus_metadata_language_check" CHECK ((("language" IS NULL) OR ("length"("language") <= 128))),
    CONSTRAINT "corpora_corpus_metadata_properties_check" CHECK ((("jsonb_typeof"("properties") = 'object'::"text") AND ("octet_length"(("properties")::"text") <= 16384))),
    CONSTRAINT "corpora_corpus_metadata_revision_check" CHECK (("revision" > 0)),
    CONSTRAINT "corpora_corpus_metadata_state_check" CHECK (("state" = ANY (ARRAY['provisioning'::"text", 'ready'::"text", 'failed'::"text"]))),
    CONSTRAINT "corpora_corpus_metadata_title_check" CHECK ((("length"("btrim"("title")) >= 1) AND ("length"("btrim"("title")) <= 512)))
);


ALTER TABLE "public"."corpora_corpus_metadata" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."corpora_corpus_provision_ops" (
    "id" "uuid" NOT NULL,
    "corpus_id" "uuid" NOT NULL,
    "actor_id" "uuid" NOT NULL,
    "session_id" "uuid" NOT NULL,
    "repo_id" "text" NOT NULL,
    "repo_type" "text" NOT NULL,
    "state" "text" DEFAULT 'admitted'::"text" NOT NULL,
    "worker_claim" "uuid",
    "claimed_at" timestamp with time zone,
    "created_at" timestamp with time zone DEFAULT "clock_timestamp"() NOT NULL,
    "creation_title" "text" NOT NULL,
    CONSTRAINT "corpora_corpus_provision_ops_check" CHECK (((("state" = 'admitted'::"text") AND ("worker_claim" IS NULL) AND ("claimed_at" IS NULL)) OR (("state" <> 'admitted'::"text") AND ("worker_claim" IS NOT NULL) AND ("claimed_at" IS NOT NULL)))),
    CONSTRAINT "corpora_corpus_provision_ops_repo_type_check" CHECK (("repo_type" = ANY (ARRAY['dataset'::"text", 'model'::"text"]))),
    CONSTRAINT "corpora_corpus_provision_ops_state_check" CHECK (("state" = ANY (ARRAY['admitted'::"text", 'executing'::"text", 'uncertain'::"text", 'completed'::"text"]))),
    CONSTRAINT "corpora_creation_title_length" CHECK ((("length"("btrim"("creation_title")) >= 1) AND ("length"("btrim"("creation_title")) <= 512)))
);


ALTER TABLE "public"."corpora_corpus_provision_ops" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."corpora_corpus_publication_claims" (
    "operation_id" "uuid" NOT NULL,
    "corpus_id" "uuid" NOT NULL,
    "stage_claim" "uuid" NOT NULL,
    "publication_claim" "uuid" NOT NULL,
    "created_at" timestamp with time zone DEFAULT "clock_timestamp"() NOT NULL,
    CONSTRAINT "corpora_corpus_publication_claims_check" CHECK (("publication_claim" <> "stage_claim"))
);


ALTER TABLE "public"."corpora_corpus_publication_claims" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."corpora_corpus_readers" (
    "corpus_id" "uuid" NOT NULL,
    "user_id" "uuid" NOT NULL,
    "added_by" "uuid" NOT NULL,
    "created_at" timestamp with time zone DEFAULT "clock_timestamp"() NOT NULL
);


ALTER TABLE "public"."corpora_corpus_readers" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."corpora_corpus_staged_inputs" (
    "operation_id" "uuid" NOT NULL,
    "corpus_id" "uuid" NOT NULL,
    "worker_claim" "uuid" NOT NULL,
    "repo_id" "text" NOT NULL,
    "repo_type" "text" NOT NULL,
    "input_format" "text" NOT NULL,
    "input_sha256" "text" NOT NULL,
    "archive_size" bigint NOT NULL,
    "requested_base_sha" "text" NOT NULL,
    "stage_sha" "text" NOT NULL,
    "stage_ref" "text" NOT NULL,
    "stage_path" "text" NOT NULL,
    "created_at" timestamp with time zone DEFAULT "clock_timestamp"() NOT NULL,
    CONSTRAINT "corpora_corpus_staged_inputs_archive_size_check" CHECK ((("archive_size" > 0) AND ("archive_size" <= 3000000))),
    CONSTRAINT "corpora_corpus_staged_inputs_check" CHECK ((("stage_sha" ~ '^[0-9a-f]{40}$'::"text") AND ("stage_sha" <> "requested_base_sha"))),
    CONSTRAINT "corpora_corpus_staged_inputs_check1" CHECK (("stage_ref" = ('corpora-stage-'::"text" || ("operation_id")::"text"))),
    CONSTRAINT "corpora_corpus_staged_inputs_check2" CHECK (("stage_path" = ((((('.corpora-staging/'::"text" || ("operation_id")::"text") || '/'::"text") || ("worker_claim")::"text") || '.'::"text") || "input_format"))),
    CONSTRAINT "corpora_corpus_staged_inputs_input_format_check" CHECK (("input_format" = ANY (ARRAY['zip'::"text", 'corpus'::"text"]))),
    CONSTRAINT "corpora_corpus_staged_inputs_input_sha256_check" CHECK (("input_sha256" ~ '^[0-9a-f]{64}$'::"text")),
    CONSTRAINT "corpora_corpus_staged_inputs_repo_type_check" CHECK (("repo_type" = ANY (ARRAY['dataset'::"text", 'model'::"text"]))),
    CONSTRAINT "corpora_corpus_staged_inputs_requested_base_sha_check" CHECK (("requested_base_sha" ~ '^[0-9a-f]{40}$'::"text"))
);


ALTER TABLE "public"."corpora_corpus_staged_inputs" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."corpora_corpus_uncertain_discards" (
    "deletion_id" "uuid" NOT NULL,
    "corpus_id" "uuid" NOT NULL,
    "provision_id" "uuid" NOT NULL,
    "observed_sha" "text" NOT NULL,
    CONSTRAINT "corpora_corpus_uncertain_discards_observed_sha_check" CHECK (("observed_sha" ~ '^[0-9a-f]{40}$'::"text"))
);


ALTER TABLE "public"."corpora_corpus_uncertain_discards" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."corpora_corpus_version_intents" (
    "operation_id" "uuid" NOT NULL,
    "corpus_id" "uuid" NOT NULL,
    "intent_sha256" "text" NOT NULL,
    "kind" "text" NOT NULL,
    "restore_sequence" bigint,
    CONSTRAINT "corpora_corpus_version_intents_check" CHECK ((("kind" = 'restore'::"text") = ("restore_sequence" IS NOT NULL))),
    CONSTRAINT "corpora_corpus_version_intents_intent_sha256_check" CHECK (("intent_sha256" ~ '^[0-9a-f]{64}$'::"text")),
    CONSTRAINT "corpora_corpus_version_intents_kind_check" CHECK (("kind" = ANY (ARRAY['manifest'::"text", 'node'::"text", 'restore'::"text"])))
);


ALTER TABLE "public"."corpora_corpus_version_intents" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."corpora_corpus_version_saves" (
    "operation_id" "uuid" NOT NULL,
    "corpus_id" "uuid" NOT NULL,
    "metadata_revision" bigint NOT NULL,
    "archive_size" bigint NOT NULL,
    CONSTRAINT "corpora_corpus_version_saves_archive_size_check" CHECK ((("archive_size" >= 1) AND ("archive_size" <= 3000000))),
    CONSTRAINT "corpora_corpus_version_saves_metadata_revision_check" CHECK (("metadata_revision" > 0))
);


ALTER TABLE "public"."corpora_corpus_version_saves" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."corpora_corpus_versions" (
    "corpus_id" "uuid" NOT NULL,
    "sequence" bigint NOT NULL,
    "commit_sha" "text" NOT NULL,
    "parent_sha" "text",
    "operation_id" "uuid" NOT NULL,
    "publication_claim" "uuid" NOT NULL,
    "retained_ref" "text" NOT NULL,
    "created_at" timestamp with time zone DEFAULT "clock_timestamp"() NOT NULL,
    CONSTRAINT "corpora_corpus_versions_check" CHECK (("retained_ref" = ('corpora-op-'::"text" || ("operation_id")::"text"))),
    CONSTRAINT "corpora_corpus_versions_check1" CHECK ((("sequence" = 1) = ("parent_sha" IS NULL))),
    CONSTRAINT "corpora_corpus_versions_check2" CHECK ((("parent_sha" IS NULL) OR ("parent_sha" <> "commit_sha"))),
    CONSTRAINT "corpora_corpus_versions_commit_sha_check" CHECK (("commit_sha" ~ '^[0-9a-f]{40}$'::"text")),
    CONSTRAINT "corpora_corpus_versions_parent_sha_check" CHECK ((("parent_sha" IS NULL) OR ("parent_sha" ~ '^[0-9a-f]{40}$'::"text"))),
    CONSTRAINT "corpora_corpus_versions_sequence_check" CHECK (("sequence" > 0))
);


ALTER TABLE "public"."corpora_corpus_versions" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."corpora_corpus_write_ops" (
    "id" "uuid" NOT NULL,
    "corpus_id" "uuid" NOT NULL,
    "actor_id" "uuid" NOT NULL,
    "session_id" "uuid" NOT NULL,
    "expected_sequence" bigint NOT NULL,
    "expected_parent_sha" "text",
    "input_sha256" "text" NOT NULL,
    "input_format" "text" NOT NULL,
    "state" "text" DEFAULT 'admitted'::"text" NOT NULL,
    "worker_claim" "uuid",
    "claimed_at" timestamp with time zone,
    "created_at" timestamp with time zone DEFAULT "clock_timestamp"() NOT NULL,
    "updated_at" timestamp with time zone DEFAULT "clock_timestamp"() NOT NULL,
    CONSTRAINT "corpora_corpus_write_ops_check" CHECK ((("expected_sequence" = 0) = ("expected_parent_sha" IS NULL))),
    CONSTRAINT "corpora_corpus_write_ops_check1" CHECK (((("state" = 'admitted'::"text") AND ("worker_claim" IS NULL) AND ("claimed_at" IS NULL)) OR (("state" <> 'admitted'::"text") AND ("worker_claim" IS NOT NULL) AND ("claimed_at" IS NOT NULL)))),
    CONSTRAINT "corpora_corpus_write_ops_expected_parent_sha_check" CHECK ((("expected_parent_sha" IS NULL) OR ("expected_parent_sha" ~ '^[0-9a-f]{40}$'::"text"))),
    CONSTRAINT "corpora_corpus_write_ops_expected_sequence_check" CHECK (("expected_sequence" >= 0)),
    CONSTRAINT "corpora_corpus_write_ops_input_format_check" CHECK (("input_format" = ANY (ARRAY['zip'::"text", 'corpus'::"text"]))),
    CONSTRAINT "corpora_corpus_write_ops_input_sha256_check" CHECK (("input_sha256" ~ '^[0-9a-f]{64}$'::"text")),
    CONSTRAINT "corpora_corpus_write_ops_state_check" CHECK (("state" = ANY (ARRAY['admitted'::"text", 'executing'::"text", 'uncertain'::"text", 'completed'::"text", 'failed'::"text"])))
);


ALTER TABLE "public"."corpora_corpus_write_ops" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."corpora_ingest_requests" (
    "owner_id" "uuid" NOT NULL,
    "request_key_hash" "text" NOT NULL,
    "input_hash" "text" NOT NULL,
    "job_id" "uuid" NOT NULL,
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    CONSTRAINT "corpora_ingest_requests_input_hash_check" CHECK (("input_hash" ~ '^[0-9a-f]{64}$'::"text")),
    CONSTRAINT "corpora_ingest_requests_request_key_hash_check" CHECK (("request_key_hash" ~ '^[0-9a-f]{64}$'::"text"))
);


ALTER TABLE "public"."corpora_ingest_requests" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."corpora_log" (
    "id" "uuid" DEFAULT "gen_random_uuid"() NOT NULL,
    "type" "text" NOT NULL,
    "log_timestamp" timestamp with time zone NOT NULL,
    "message" "text" NOT NULL,
    "metadata" "jsonb",
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    CONSTRAINT "corpora_log_type_check" CHECK (("type" = ANY (ARRAY['auth'::"text", 'database'::"text", 'storage'::"text", 'edge'::"text", 'realtime'::"text"])))
);

ALTER TABLE ONLY "public"."corpora_log" REPLICA IDENTITY FULL;


ALTER TABLE "public"."corpora_log" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."corpora_project_discoveries" (
    "project_id" "uuid" NOT NULL,
    "discovery_id" "uuid" NOT NULL,
    "source_corpus" "uuid" NOT NULL,
    "version" bigint NOT NULL,
    CONSTRAINT "corpora_project_discoveries_version_check" CHECK (("version" > 0))
);


ALTER TABLE "public"."corpora_project_discoveries" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."corpora_project_discovery_revisions" (
    "project_id" "uuid" NOT NULL,
    "discovery_id" "uuid" NOT NULL,
    "version" bigint NOT NULL,
    "actor_id" "uuid" NOT NULL,
    "recorded_at" timestamp with time zone DEFAULT "clock_timestamp"() NOT NULL,
    "reason" "text" NOT NULL,
    "action" "text" NOT NULL,
    "evidence" "jsonb" NOT NULL,
    CONSTRAINT "corpora_project_discovery_revisions_action_check" CHECK (("action" = ANY (ARRAY['register'::"text", 'accept'::"text", 'reject'::"text"]))),
    CONSTRAINT "corpora_project_discovery_revisions_evidence_check" CHECK (("pg_column_size"("evidence") <= 65536)),
    CONSTRAINT "corpora_project_discovery_revisions_reason_check" CHECK (((("length"("reason") >= 1) AND ("length"("reason") <= 2000)) AND ("btrim"("reason") <> ''::"text"))),
    CONSTRAINT "corpora_project_discovery_revisions_version_check" CHECK (("version" > 0))
);


ALTER TABLE "public"."corpora_project_discovery_revisions" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."corpora_project_link_heads" (
    "project_id" "uuid" NOT NULL,
    "reference_id" "uuid" NOT NULL,
    "version" bigint NOT NULL,
    CONSTRAINT "corpora_project_link_heads_version_check" CHECK (("version" > 0))
);


ALTER TABLE "public"."corpora_project_link_heads" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."corpora_project_link_revisions" (
    "project_id" "uuid" NOT NULL,
    "reference_id" "uuid" NOT NULL,
    "version" bigint NOT NULL,
    "actor_id" "uuid" NOT NULL,
    "recorded_at" timestamp with time zone DEFAULT "clock_timestamp"() NOT NULL,
    "reason" "text" NOT NULL,
    "action" "text" NOT NULL,
    "source_corpus" "uuid" NOT NULL,
    "target_corpus" "uuid" NOT NULL,
    "reference" "jsonb" NOT NULL,
    CONSTRAINT "corpora_project_link_revisions_action_check" CHECK (("action" = ANY (ARRAY['edit'::"text", 'resolve'::"text", 'approve'::"text", 'reject'::"text", 'reopen'::"text", 'publish'::"text", 'withdraw'::"text"]))),
    CONSTRAINT "corpora_project_link_revisions_reason_check" CHECK (((("length"("reason") >= 1) AND ("length"("reason") <= 2000)) AND ("btrim"("reason") <> ''::"text"))),
    CONSTRAINT "corpora_project_link_revisions_reference_check" CHECK (("pg_column_size"("reference") <= 65536)),
    CONSTRAINT "corpora_project_link_revisions_version_check" CHECK (("version" > 0))
);


ALTER TABLE "public"."corpora_project_link_revisions" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."corpora_project_managed_corpora" (
    "project_id" "uuid" NOT NULL,
    "corpus_id" "uuid" NOT NULL,
    "linked_by" "uuid" NOT NULL,
    "linked_at" timestamp with time zone DEFAULT "clock_timestamp"() NOT NULL
);


ALTER TABLE "public"."corpora_project_managed_corpora" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."corpora_project_members" (
    "project_id" "uuid" NOT NULL,
    "user_id" "uuid" NOT NULL,
    "added_by" "uuid" NOT NULL,
    "created_at" timestamp with time zone DEFAULT "clock_timestamp"() NOT NULL
);


ALTER TABLE "public"."corpora_project_members" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."corpus_changes" (
    "id" "uuid" DEFAULT "gen_random_uuid"() NOT NULL,
    "document_id" "uuid" NOT NULL,
    "commit_id" "uuid",
    "reverts_change_id" "uuid",
    "version" "text" NOT NULL,
    "node_id" bigint NOT NULL,
    "node_type" "text",
    "field" "text" NOT NULL,
    "previous_value" "text",
    "new_value" "text",
    "kind" "text" NOT NULL,
    "status" "text" DEFAULT 'pending'::"text" NOT NULL,
    "resp" "text"[] NOT NULL,
    "applied_by" "uuid" NOT NULL,
    "applied_at" timestamp with time zone,
    "suggestion_id" "text",
    "thread_id" "text",
    "rule" "text",
    "rationale" "text",
    "diff" "jsonb",
    "scope" "jsonb",
    "content_hash_before" "text",
    "source" "text" DEFAULT 'ai_panel'::"text" NOT NULL,
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    CONSTRAINT "corpus_changes_applied_time" CHECK ((("status" <> 'applied'::"text") OR ("applied_at" IS NOT NULL))),
    CONSTRAINT "corpus_changes_kind_check" CHECK (("kind" = ANY (ARRAY['annotation'::"text", 'label'::"text", 'boundary'::"text", 'formatting'::"text", 'reference'::"text", 'text'::"text"]))),
    CONSTRAINT "corpus_changes_resp_check" CHECK (("cardinality"("resp") >= 2)),
    CONSTRAINT "corpus_changes_source_check" CHECK (("source" = ANY (ARRAY['ai_panel'::"text", 'api'::"text", 'import'::"text"]))),
    CONSTRAINT "corpus_changes_status_check" CHECK (("status" = ANY (ARRAY['pending'::"text", 'applied'::"text", 'failed'::"text", 'reverted'::"text"]))),
    CONSTRAINT "corpus_changes_value_change" CHECK (("previous_value" IS DISTINCT FROM "new_value"))
);


ALTER TABLE "public"."corpus_changes" OWNER TO "postgres";


COMMENT ON TABLE "public"."corpus_changes" IS 'Write-ahead version-history log for AI-curation edits to corpus nodes. With no approval flow (deferred), this table is the safety net: every applied change is recorded here with dual resp attribution and its displaced value; feeds the Activity tab, reader marks, undo, and TEI export attribution.';



COMMENT ON COLUMN "public"."corpus_changes"."commit_id" IS 'Filled when the working version is committed — joins panel edits to corpus_commits.';



COMMENT ON COLUMN "public"."corpus_changes"."resp" IS 'TEI-style responsibility chain, e.g. {#corpora-ai, #<user-id>}; CHECK enforces both.';



CREATE TABLE IF NOT EXISTS "public"."corpus_commits" (
    "id" "uuid" DEFAULT "gen_random_uuid"() NOT NULL,
    "sha" "text" NOT NULL,
    "message" "text" NOT NULL,
    "author_name" "text",
    "author_email" "text",
    "branch" "text",
    "committed_at" timestamp with time zone,
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "document_id" "uuid" NOT NULL
);


ALTER TABLE "public"."corpus_commits" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."corpus_documents" (
    "id" "uuid" DEFAULT "gen_random_uuid"() NOT NULL,
    "name" "text" NOT NULL,
    "source" "text" NOT NULL,
    "path" "text" NOT NULL,
    "filename" "text",
    "uploaded_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "corpus_type" "text",
    "source_format" "text",
    "licence" "text",
    "language" "text",
    "size_bytes" bigint,
    "docs_count" integer,
    "nodes" integer,
    "words" integer,
    "status" "text",
    "converted_at" timestamp with time zone,
    "description" "text",
    "toc" "jsonb",
    "job_id" "text",
    "owner_id" "uuid" DEFAULT "auth"."uid"(),
    CONSTRAINT "corpus_documents_source_check" CHECK (("source" = ANY (ARRAY['upload'::"text", 'huggingface'::"text"]))),
    CONSTRAINT "corpus_documents_status_check" CHECK (("status" = ANY (ARRAY['converted'::"text", 'uploaded'::"text"])))
);


ALTER TABLE "public"."corpus_documents" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."devices" (
    "id" "uuid" DEFAULT "gen_random_uuid"() NOT NULL,
    "name" "text" NOT NULL,
    "system_uuid" "text" NOT NULL,
    "os" "text" NOT NULL,
    "version" "text" NOT NULL,
    "is_app_installed" boolean DEFAULT false NOT NULL,
    "has_data" boolean DEFAULT false NOT NULL,
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "updated_at" timestamp with time zone
);


ALTER TABLE "public"."devices" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."licences" (
    "id" "text" NOT NULL,
    "title" "text" NOT NULL,
    "url" "text",
    "maintainer" "text",
    "family" "text",
    "status" "public"."licence_status" DEFAULT 'active'::"public"."licence_status" NOT NULL,
    "domain_content" boolean DEFAULT false NOT NULL,
    "domain_data" boolean DEFAULT false NOT NULL,
    "domain_software" boolean DEFAULT false NOT NULL,
    "od_conformance" "public"."licence_conformance",
    "osd_conformance" "public"."licence_conformance",
    "is_generic" boolean DEFAULT false NOT NULL,
    "legacy_ids" "text"[],
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "updated_at" timestamp with time zone,
    "full_text" "text"
);


ALTER TABLE "public"."licences" OWNER TO "postgres";


COMMENT ON TABLE "public"."licences" IS 'Open licence catalogue sourced from https://licenses.opendefinition.org/licenses/groups/all.json';



CREATE TABLE IF NOT EXISTS "public"."logs" (
    "id" "uuid" DEFAULT "gen_random_uuid"() NOT NULL,
    "type" "public"."logs_category" DEFAULT 'other'::"public"."logs_category" NOT NULL,
    "product" "text" NOT NULL,
    "source" "text" NOT NULL,
    "level" "public"."logs_level" DEFAULT 'info'::"public"."logs_level" NOT NULL,
    "message" "text" NOT NULL,
    "details" "jsonb",
    "timestamp" timestamp with time zone NOT NULL,
    "created_at" timestamp with time zone DEFAULT "now"(),
    CONSTRAINT "logs_valid_timestamp" CHECK (("timestamp" <= ("now"() + '00:00:01'::interval)))
);


ALTER TABLE "public"."logs" OWNER TO "postgres";


COMMENT ON TABLE "public"."logs" IS 'Stores logs from the log-drains edge function for audit trail and monitoring';



CREATE TABLE IF NOT EXISTS "public"."organizations" (
    "id" "uuid" DEFAULT "gen_random_uuid"() NOT NULL,
    "name" "text" NOT NULL,
    "website" "text",
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    CONSTRAINT "organizations_name_check" CHECK (("length"(TRIM(BOTH FROM "name")) > 0))
);


ALTER TABLE "public"."organizations" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."project_corpora" (
    "project_id" "uuid" NOT NULL,
    "corpus_id" "uuid" NOT NULL,
    "linked_at" timestamp with time zone DEFAULT "now"() NOT NULL
);


ALTER TABLE "public"."project_corpora" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."project_licences" (
    "id" "uuid" DEFAULT "gen_random_uuid"() NOT NULL,
    "project_id" "uuid" NOT NULL,
    "licence_id" "text" NOT NULL,
    "agreed_by_user_id" "uuid",
    "agreed_at" timestamp with time zone DEFAULT "now"(),
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    CONSTRAINT "project_licences_agreement_pair" CHECK (((("agreed_at" IS NULL) AND ("agreed_by_user_id" IS NULL)) OR (("agreed_at" IS NOT NULL) AND ("agreed_by_user_id" IS NOT NULL))))
);


ALTER TABLE "public"."project_licences" OWNER TO "postgres";


COMMENT ON TABLE "public"."project_licences" IS 'Many-to-many join between projects and licences, plus per-project licence agreement metadata.';



COMMENT ON COLUMN "public"."project_licences"."agreed_by_user_id" IS 'User who agreed to the licence terms for this project. Set together with agreed_at.';



COMMENT ON COLUMN "public"."project_licences"."agreed_at" IS 'Timestamp at which the project owner agreed to the licence terms.';



CREATE TABLE IF NOT EXISTS "public"."project_references" (
    "id" "uuid" DEFAULT "gen_random_uuid"() NOT NULL,
    "project_id" "uuid" NOT NULL,
    "title" "text" NOT NULL,
    "authors" "text",
    "year" smallint,
    "publication" "text",
    "url" "text",
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "updated_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    CONSTRAINT "project_references_title_check" CHECK (("length"(TRIM(BOTH FROM "title")) > 0))
);


ALTER TABLE "public"."project_references" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."projects" (
    "id" "uuid" DEFAULT "gen_random_uuid"() NOT NULL,
    "name" "text" NOT NULL,
    "description" "text",
    "owner_id" "uuid" DEFAULT "auth"."uid"(),
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "updated_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "status" "text" DEFAULT 'draft'::"text" NOT NULL,
    "type" "text",
    "language" "text"[],
    "category" "text",
    "organization_id" "uuid",
    "user_id" "uuid" NOT NULL,
    "corpus_document_id" "uuid",
    CONSTRAINT "projects_category_check" CHECK (("category" = ANY (ARRAY['biblical'::"text", 'religious'::"text", 'literary'::"text", 'historical'::"text", 'paratext'::"text"]))),
    CONSTRAINT "projects_classification_check" CHECK (((("type" = ANY (ARRAY['bible'::"text", 'tanakh'::"text", 'quran'::"text", 'apocrypha'::"text"])) AND ("language" IS NOT NULL) AND ("category" IS NULL)) OR (("type" = ANY (ARRAY['biography'::"text", 'commentary'::"text", 'review'::"text"])) AND ("category" IS NOT NULL) AND ("language" IS NULL)) OR ((("type" IS NULL) OR ("type" = ANY (ARRAY['lexicon'::"text", 'manuscript'::"text", 'regular'::"text"]))) AND ("language" IS NULL) AND ("category" IS NULL)))),
    CONSTRAINT "projects_language_check" CHECK ((("language" IS NULL) OR (("cardinality"("language") > 0) AND ("language" <@ ARRAY['hebrew'::"text", 'greek'::"text", 'syriac'::"text", 'arabic'::"text", 'aramaic'::"text", 'protoCuneiform'::"text", 'akkadian'::"text", 'ugaritic'::"text", 'pali'::"text", 'latin'::"text", 'dutch'::"text", 'french'::"text", 'italian'::"text", 'english'::"text"])))),
    CONSTRAINT "projects_name_check" CHECK (("length"(TRIM(BOTH FROM "name")) > 0)),
    CONSTRAINT "projects_status_check" CHECK (("status" = ANY (ARRAY['draft'::"text", 'started'::"text", 'ready-for-review'::"text", 'published'::"text", 'failed'::"text"]))),
    CONSTRAINT "projects_type_check" CHECK (("type" = ANY (ARRAY['bible'::"text", 'commentary'::"text", 'lexicon'::"text", 'biography'::"text", 'review'::"text", 'manuscript'::"text", 'tanakh'::"text", 'quran'::"text", 'apocrypha'::"text", 'regular'::"text"])))
);


ALTER TABLE "public"."projects" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."repositories" (
    "uuid" "uuid" DEFAULT "gen_random_uuid"() NOT NULL,
    "name" "text" NOT NULL,
    "type" "public"."book_type",
    "version" integer,
    "format" "public"."format_type",
    "description" "text",
    "download_uri" "text",
    "licence" "text",
    "date" timestamp with time zone,
    "credits" "text",
    "language" "public"."language_type" NOT NULL,
    "period" "text" NOT NULL,
    "repository" "text" NOT NULL,
    "size" "text",
    "image_url" "text",
    "category" "public"."category_type"[] NOT NULL,
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "updated_at" timestamp with time zone
);


ALTER TABLE "public"."repositories" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."user_directory" (
    "id" "uuid" NOT NULL,
    "name" "text",
    "username" "text" NOT NULL,
    "email" "text" NOT NULL,
    "phone" "text",
    "website" "text",
    "system_path" "text",
    "operating_system" "text",
    "address_street" "text",
    "address_suite" "text",
    "address_city" "text",
    "address_zipcode" "text",
    "address_country" "text",
    "organization_id" "uuid",
    "auth_id" "uuid",
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    CONSTRAINT "user_directory_username_check" CHECK (("length"(TRIM(BOTH FROM "username")) > 0))
);


ALTER TABLE "public"."user_directory" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."users" (
    "id" "uuid" NOT NULL,
    "display_name" "text" DEFAULT 'Guest'::"text" NOT NULL,
    "avatar_url" "text",
    "user_type" "text" DEFAULT 'anonymous'::"text" NOT NULL,
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "updated_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "name" "text",
    "username" "text",
    "email" "text",
    "phone" "text",
    "website" "text",
    "system_path" "text",
    "auth_id" "text",
    "operating_system" "text",
    "address_id" "uuid",
    "organization_id" "uuid",
    "device_id" "uuid",
    CONSTRAINT "users_user_type_check" CHECK (("user_type" = ANY (ARRAY['anonymous'::"text", 'registered'::"text", 'admin'::"text"])))
);


ALTER TABLE "public"."users" OWNER TO "postgres";


COMMENT ON TABLE "public"."users" IS 'App-level user profiles, 1:1 with auth.users. Source of truth for user properties.';



ALTER TABLE ONLY "corpora_session_guard"."superadmins"
    ADD CONSTRAINT "superadmins_pkey" PRIMARY KEY ("user_id");



ALTER TABLE ONLY "public"."addresses"
    ADD CONSTRAINT "addresses_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."authors"
    ADD CONSTRAINT "authors_name_key" UNIQUE ("name");



ALTER TABLE ONLY "public"."authors"
    ADD CONSTRAINT "authors_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."books"
    ADD CONSTRAINT "books_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."conversion_jobs"
    ADD CONSTRAINT "conversion_jobs_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."corpora_corpus_access"
    ADD CONSTRAINT "corpora_corpus_access_pkey" PRIMARY KEY ("corpus_id");



ALTER TABLE ONLY "public"."corpora_corpus_bootstrap"
    ADD CONSTRAINT "corpora_corpus_bootstrap_operation_id_key" UNIQUE ("operation_id");



ALTER TABLE ONLY "public"."corpora_corpus_bootstrap"
    ADD CONSTRAINT "corpora_corpus_bootstrap_pkey" PRIMARY KEY ("corpus_id");



ALTER TABLE ONLY "public"."corpora_corpus_deletions"
    ADD CONSTRAINT "corpora_corpus_deletions_corpus_id_key" UNIQUE ("corpus_id");



ALTER TABLE ONLY "public"."corpora_corpus_deletions"
    ADD CONSTRAINT "corpora_corpus_deletions_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."corpora_corpus_deletions"
    ADD CONSTRAINT "corpora_corpus_deletions_worker_claim_key" UNIQUE ("worker_claim");



ALTER TABLE ONLY "public"."corpora_corpus_heads"
    ADD CONSTRAINT "corpora_corpus_heads_pkey" PRIMARY KEY ("corpus_id");



ALTER TABLE ONLY "public"."corpora_corpus_imports"
    ADD CONSTRAINT "corpora_corpus_imports_pkey" PRIMARY KEY ("operation_id");



ALTER TABLE ONLY "public"."corpora_corpus_metadata"
    ADD CONSTRAINT "corpora_corpus_metadata_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."corpora_corpus_provision_ops"
    ADD CONSTRAINT "corpora_corpus_provision_ops_corpus_id_key" UNIQUE ("corpus_id");



ALTER TABLE ONLY "public"."corpora_corpus_provision_ops"
    ADD CONSTRAINT "corpora_corpus_provision_ops_id_corpus_id_worker_claim_key" UNIQUE ("id", "corpus_id", "worker_claim");



ALTER TABLE ONLY "public"."corpora_corpus_provision_ops"
    ADD CONSTRAINT "corpora_corpus_provision_ops_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."corpora_corpus_provision_ops"
    ADD CONSTRAINT "corpora_corpus_provision_ops_worker_claim_key" UNIQUE ("worker_claim");



ALTER TABLE ONLY "public"."corpora_corpus_publication_claims"
    ADD CONSTRAINT "corpora_corpus_publication_cl_operation_id_corpus_id_public_key" UNIQUE ("operation_id", "corpus_id", "publication_claim");



ALTER TABLE ONLY "public"."corpora_corpus_publication_claims"
    ADD CONSTRAINT "corpora_corpus_publication_claims_pkey" PRIMARY KEY ("operation_id");



ALTER TABLE ONLY "public"."corpora_corpus_publication_claims"
    ADD CONSTRAINT "corpora_corpus_publication_claims_publication_claim_key" UNIQUE ("publication_claim");



ALTER TABLE ONLY "public"."corpora_corpus_readers"
    ADD CONSTRAINT "corpora_corpus_readers_pkey" PRIMARY KEY ("corpus_id", "user_id");



ALTER TABLE ONLY "public"."corpora_corpus_staged_inputs"
    ADD CONSTRAINT "corpora_corpus_staged_inputs_operation_id_corpus_id_worker__key" UNIQUE ("operation_id", "corpus_id", "worker_claim");



ALTER TABLE ONLY "public"."corpora_corpus_staged_inputs"
    ADD CONSTRAINT "corpora_corpus_staged_inputs_pkey" PRIMARY KEY ("operation_id");



ALTER TABLE ONLY "public"."corpora_corpus_uncertain_discards"
    ADD CONSTRAINT "corpora_corpus_uncertain_discards_pkey" PRIMARY KEY ("deletion_id");



ALTER TABLE ONLY "public"."corpora_corpus_version_intents"
    ADD CONSTRAINT "corpora_corpus_version_intents_pkey" PRIMARY KEY ("operation_id");



ALTER TABLE ONLY "public"."corpora_corpus_version_saves"
    ADD CONSTRAINT "corpora_corpus_version_saves_identity" UNIQUE ("operation_id", "corpus_id");



ALTER TABLE ONLY "public"."corpora_corpus_version_saves"
    ADD CONSTRAINT "corpora_corpus_version_saves_pkey" PRIMARY KEY ("operation_id");



ALTER TABLE ONLY "public"."corpora_corpus_versions"
    ADD CONSTRAINT "corpora_corpus_versions_corpus_id_commit_sha_key" UNIQUE ("corpus_id", "commit_sha");



ALTER TABLE ONLY "public"."corpora_corpus_versions"
    ADD CONSTRAINT "corpora_corpus_versions_corpus_id_sequence_commit_sha_key" UNIQUE ("corpus_id", "sequence", "commit_sha");



ALTER TABLE ONLY "public"."corpora_corpus_versions"
    ADD CONSTRAINT "corpora_corpus_versions_operation_id_key" UNIQUE ("operation_id");



ALTER TABLE ONLY "public"."corpora_corpus_versions"
    ADD CONSTRAINT "corpora_corpus_versions_pkey" PRIMARY KEY ("corpus_id", "sequence");



ALTER TABLE ONLY "public"."corpora_corpus_write_ops"
    ADD CONSTRAINT "corpora_corpus_write_ops_id_corpus_id_key" UNIQUE ("id", "corpus_id");



ALTER TABLE ONLY "public"."corpora_corpus_write_ops"
    ADD CONSTRAINT "corpora_corpus_write_ops_id_corpus_id_worker_claim_key" UNIQUE ("id", "corpus_id", "worker_claim");



ALTER TABLE ONLY "public"."corpora_corpus_write_ops"
    ADD CONSTRAINT "corpora_corpus_write_ops_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."corpora_corpus_write_ops"
    ADD CONSTRAINT "corpora_corpus_write_ops_worker_claim_key" UNIQUE ("worker_claim");



ALTER TABLE ONLY "public"."corpora_ingest_requests"
    ADD CONSTRAINT "corpora_ingest_requests_job_id_key" UNIQUE ("job_id");



ALTER TABLE ONLY "public"."corpora_ingest_requests"
    ADD CONSTRAINT "corpora_ingest_requests_pkey" PRIMARY KEY ("owner_id", "request_key_hash");



ALTER TABLE ONLY "public"."corpora_log"
    ADD CONSTRAINT "corpora_log_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."corpora"
    ADD CONSTRAINT "corpora_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."corpora_project_discoveries"
    ADD CONSTRAINT "corpora_project_discoveries_pkey" PRIMARY KEY ("project_id", "discovery_id");



ALTER TABLE ONLY "public"."corpora_project_discovery_revisions"
    ADD CONSTRAINT "corpora_project_discovery_revisions_pkey" PRIMARY KEY ("project_id", "discovery_id", "version");



ALTER TABLE ONLY "public"."corpora_project_link_heads"
    ADD CONSTRAINT "corpora_project_link_heads_pkey" PRIMARY KEY ("project_id", "reference_id");



ALTER TABLE ONLY "public"."corpora_project_link_revisions"
    ADD CONSTRAINT "corpora_project_link_revisions_pkey" PRIMARY KEY ("project_id", "reference_id", "version");



ALTER TABLE ONLY "public"."corpora_project_managed_corpora"
    ADD CONSTRAINT "corpora_project_managed_corpora_pkey" PRIMARY KEY ("project_id", "corpus_id");



ALTER TABLE ONLY "public"."corpora_project_members"
    ADD CONSTRAINT "corpora_project_members_pkey" PRIMARY KEY ("project_id", "user_id");



ALTER TABLE ONLY "public"."corpora"
    ADD CONSTRAINT "corpora_uid_key" UNIQUE ("uid");



ALTER TABLE ONLY "public"."corpus_changes"
    ADD CONSTRAINT "corpus_changes_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."corpus_changes"
    ADD CONSTRAINT "corpus_changes_revert_once" UNIQUE ("reverts_change_id");



ALTER TABLE ONLY "public"."corpus_commits"
    ADD CONSTRAINT "corpus_commits_document_sha_key" UNIQUE ("document_id", "sha");



ALTER TABLE ONLY "public"."corpus_commits"
    ADD CONSTRAINT "corpus_commits_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."corpus_documents"
    ADD CONSTRAINT "corpus_documents_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."devices"
    ADD CONSTRAINT "devices_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."devices"
    ADD CONSTRAINT "devices_system_uuid_key" UNIQUE ("system_uuid");



ALTER TABLE ONLY "public"."licences"
    ADD CONSTRAINT "licences_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."logs"
    ADD CONSTRAINT "logs_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."organizations"
    ADD CONSTRAINT "organizations_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."project_corpora"
    ADD CONSTRAINT "project_corpora_pkey" PRIMARY KEY ("project_id", "corpus_id");



ALTER TABLE ONLY "public"."project_licences"
    ADD CONSTRAINT "project_licences_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."project_licences"
    ADD CONSTRAINT "project_licences_project_id_licence_id_key" UNIQUE ("project_id", "licence_id");



ALTER TABLE ONLY "public"."project_references"
    ADD CONSTRAINT "project_references_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."projects"
    ADD CONSTRAINT "projects_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."repositories"
    ADD CONSTRAINT "repositories_name_key" UNIQUE ("name");



ALTER TABLE ONLY "public"."repositories"
    ADD CONSTRAINT "repositories_pkey" PRIMARY KEY ("uuid");



ALTER TABLE ONLY "public"."user_directory"
    ADD CONSTRAINT "user_directory_auth_id_key" UNIQUE ("auth_id");



ALTER TABLE ONLY "public"."user_directory"
    ADD CONSTRAINT "user_directory_email_key" UNIQUE ("email");



ALTER TABLE ONLY "public"."user_directory"
    ADD CONSTRAINT "user_directory_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."user_directory"
    ADD CONSTRAINT "user_directory_username_key" UNIQUE ("username");



ALTER TABLE ONLY "public"."users"
    ADD CONSTRAINT "users_pkey" PRIMARY KEY ("id");



CREATE INDEX "conversion_jobs_finished_at_idx" ON "public"."conversion_jobs" USING "btree" ("finished_at");



CREATE INDEX "conversion_jobs_owner_created_at_idx" ON "public"."conversion_jobs" USING "btree" ("owner", "created_at" DESC);



CREATE UNIQUE INDEX "corpora_corpus_metadata_hf_repo_id_unique" ON "public"."corpora_corpus_metadata" USING "btree" ("lower"("hf_repo_id"));



CREATE INDEX "corpora_corpus_metadata_owner_id_id" ON "public"."corpora_corpus_metadata" USING "btree" ("owner_id", "id");



CREATE INDEX "corpora_corpus_readers_user" ON "public"."corpora_corpus_readers" USING "btree" ("user_id", "corpus_id");



CREATE UNIQUE INDEX "corpora_corpus_write_ops_one_active" ON "public"."corpora_corpus_write_ops" USING "btree" ("corpus_id") WHERE ("state" = ANY (ARRAY['admitted'::"text", 'executing'::"text", 'uncertain'::"text"]));



CREATE INDEX "corpora_log_timestamp_idx" ON "public"."corpora_log" USING "btree" ("log_timestamp" DESC);



CREATE INDEX "corpora_log_type_idx" ON "public"."corpora_log" USING "btree" ("type");



CREATE INDEX "corpora_owner_id_idx" ON "public"."corpora" USING "btree" ("owner_id");



CREATE INDEX "corpus_changes_document_applied_idx" ON "public"."corpus_changes" USING "btree" ("document_id", "applied_at" DESC);



CREATE INDEX "corpus_changes_document_node_idx" ON "public"."corpus_changes" USING "btree" ("document_id", "node_id");



CREATE INDEX "corpus_changes_pending_idx" ON "public"."corpus_changes" USING "btree" ("created_at") WHERE ("status" = 'pending'::"text");



CREATE INDEX "corpus_changes_suggestion_idx" ON "public"."corpus_changes" USING "btree" ("suggestion_id") WHERE ("suggestion_id" IS NOT NULL);



CREATE INDEX "idx_corpus_commits_document" ON "public"."corpus_commits" USING "btree" ("document_id", "committed_at" DESC);



CREATE INDEX "idx_licences_domain_content" ON "public"."licences" USING "btree" ("domain_content") WHERE "domain_content";



CREATE INDEX "idx_licences_domain_data" ON "public"."licences" USING "btree" ("domain_data") WHERE "domain_data";



CREATE INDEX "idx_licences_domain_software" ON "public"."licences" USING "btree" ("domain_software") WHERE "domain_software";



CREATE INDEX "idx_licences_status" ON "public"."licences" USING "btree" ("status");



CREATE INDEX "idx_logs_created_at" ON "public"."logs" USING "btree" ("created_at" DESC);



CREATE INDEX "idx_logs_level" ON "public"."logs" USING "btree" ("level");



CREATE INDEX "idx_logs_product" ON "public"."logs" USING "btree" ("product");



CREATE INDEX "idx_logs_timestamp" ON "public"."logs" USING "btree" ("timestamp" DESC);



CREATE INDEX "idx_logs_type" ON "public"."logs" USING "btree" ("type");



CREATE INDEX "idx_logs_type_level_timestamp" ON "public"."logs" USING "btree" ("type", "level", "timestamp" DESC);



CREATE INDEX "project_corpora_corpus_id_idx" ON "public"."project_corpora" USING "btree" ("corpus_id");



CREATE INDEX "project_licences_agreed_by_user_id_idx" ON "public"."project_licences" USING "btree" ("agreed_by_user_id");



CREATE INDEX "project_licences_licence_id_idx" ON "public"."project_licences" USING "btree" ("licence_id");



CREATE INDEX "project_licences_project_id_idx" ON "public"."project_licences" USING "btree" ("project_id");



CREATE INDEX "project_references_project_id_idx" ON "public"."project_references" USING "btree" ("project_id");



CREATE INDEX "projects_organization_id_idx" ON "public"."projects" USING "btree" ("organization_id");



CREATE INDEX "projects_owner_id_idx" ON "public"."projects" USING "btree" ("owner_id");



CREATE INDEX "projects_updated_at_idx" ON "public"."projects" USING "btree" ("updated_at" DESC);



CREATE INDEX "projects_user_id_idx" ON "public"."projects" USING "btree" ("user_id");



CREATE INDEX "users_address_id_idx" ON "public"."users" USING "btree" ("address_id");



CREATE UNIQUE INDEX "users_auth_id_key" ON "public"."users" USING "btree" ("auth_id") WHERE ("auth_id" IS NOT NULL);



CREATE UNIQUE INDEX "users_device_id_idx" ON "public"."users" USING "btree" ("device_id");



CREATE UNIQUE INDEX "users_email_key" ON "public"."users" USING "btree" ("email");



CREATE INDEX "users_organization_id_idx" ON "public"."users" USING "btree" ("organization_id");



CREATE UNIQUE INDEX "users_username_key" ON "public"."users" USING "btree" ("username");



CREATE OR REPLACE TRIGGER "addresses_set_updated_at" BEFORE UPDATE ON "public"."addresses" FOR EACH ROW EXECUTE FUNCTION "public"."set_updated_at"();



CREATE OR REPLACE TRIGGER "authors_set_updated_at" BEFORE UPDATE ON "public"."authors" FOR EACH ROW EXECUTE FUNCTION "public"."set_updated_at"();



CREATE OR REPLACE TRIGGER "corpora_corpus_access_guard" BEFORE DELETE OR UPDATE ON "public"."corpora_corpus_access" FOR EACH ROW EXECUTE FUNCTION "corpora_session_guard"."guard_access"();



CREATE CONSTRAINT TRIGGER "corpora_corpus_bootstrap_finalized" AFTER INSERT ON "public"."corpora_corpus_bootstrap" DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION "public"."corpora_corpus_bootstrap_finalized"();



CREATE OR REPLACE TRIGGER "corpora_corpus_bootstrap_immutable" BEFORE DELETE OR UPDATE ON "public"."corpora_corpus_bootstrap" FOR EACH ROW EXECUTE FUNCTION "public"."corpora_corpus_preserve_history"();



CREATE OR REPLACE TRIGGER "corpora_corpus_creation_input" BEFORE INSERT OR UPDATE ON "public"."corpora_corpus_provision_ops" FOR EACH ROW EXECUTE FUNCTION "public"."corpora_corpus_creation_input"();



CREATE OR REPLACE TRIGGER "corpora_corpus_deletion_guard" BEFORE DELETE OR UPDATE ON "public"."corpora_corpus_deletions" FOR EACH ROW EXECUTE FUNCTION "corpora_session_guard"."guard_deletion"();



CREATE OR REPLACE TRIGGER "corpora_corpus_heads_no_delete" BEFORE DELETE ON "public"."corpora_corpus_heads" FOR EACH ROW EXECUTE FUNCTION "public"."corpora_corpus_preserve_history"();



CREATE OR REPLACE TRIGGER "corpora_corpus_imports_immutable" BEFORE DELETE OR UPDATE ON "public"."corpora_corpus_imports" FOR EACH ROW EXECUTE FUNCTION "public"."corpora_corpus_preserve_history"();



CREATE OR REPLACE TRIGGER "corpora_corpus_initialize_access" AFTER INSERT ON "public"."corpora_corpus_metadata" FOR EACH ROW EXECUTE FUNCTION "corpora_session_guard"."initialize_access"();



CREATE OR REPLACE TRIGGER "corpora_corpus_live_metadata" BEFORE UPDATE ON "public"."corpora_corpus_metadata" FOR EACH ROW EXECUTE FUNCTION "corpora_session_guard"."require_live_corpus"();



CREATE OR REPLACE TRIGGER "corpora_corpus_live_provision" BEFORE INSERT ON "public"."corpora_corpus_provision_ops" FOR EACH ROW EXECUTE FUNCTION "corpora_session_guard"."require_live_corpus"();



CREATE OR REPLACE TRIGGER "corpora_corpus_live_write" BEFORE INSERT ON "public"."corpora_corpus_write_ops" FOR EACH ROW EXECUTE FUNCTION "corpora_session_guard"."require_live_corpus"();



CREATE OR REPLACE TRIGGER "corpora_corpus_metadata_stamp" BEFORE UPDATE ON "public"."corpora_corpus_metadata" FOR EACH ROW EXECUTE FUNCTION "public"."corpora_corpus_metadata_stamp"();



CREATE OR REPLACE TRIGGER "corpora_corpus_provision_ops_no_delete" BEFORE DELETE ON "public"."corpora_corpus_provision_ops" FOR EACH ROW EXECUTE FUNCTION "public"."corpora_corpus_preserve_history"();



CREATE OR REPLACE TRIGGER "corpora_corpus_publication_claims_immutable" BEFORE DELETE OR UPDATE ON "public"."corpora_corpus_publication_claims" FOR EACH ROW EXECUTE FUNCTION "public"."corpora_corpus_preserve_history"();



CREATE OR REPLACE TRIGGER "corpora_corpus_staged_inputs_immutable" BEFORE DELETE OR UPDATE ON "public"."corpora_corpus_staged_inputs" FOR EACH ROW EXECUTE FUNCTION "public"."corpora_corpus_preserve_history"();



CREATE OR REPLACE TRIGGER "corpora_corpus_uncertain_discards_immutable" BEFORE DELETE OR UPDATE ON "public"."corpora_corpus_uncertain_discards" FOR EACH ROW EXECUTE FUNCTION "public"."corpora_corpus_preserve_history"();



CREATE OR REPLACE TRIGGER "corpora_corpus_validate_bootstrap" BEFORE INSERT ON "public"."corpora_corpus_bootstrap" FOR EACH ROW EXECUTE FUNCTION "public"."corpora_corpus_validate_bootstrap"();



CREATE OR REPLACE TRIGGER "corpora_corpus_validate_head" BEFORE INSERT OR UPDATE ON "public"."corpora_corpus_heads" FOR EACH ROW EXECUTE FUNCTION "public"."corpora_corpus_validate_head"();



CREATE OR REPLACE TRIGGER "corpora_corpus_validate_operation" BEFORE INSERT OR UPDATE ON "public"."corpora_corpus_write_ops" FOR EACH ROW EXECUTE FUNCTION "public"."corpora_corpus_validate_operation"();



CREATE OR REPLACE TRIGGER "corpora_corpus_validate_provision" BEFORE INSERT OR UPDATE ON "public"."corpora_corpus_provision_ops" FOR EACH ROW EXECUTE FUNCTION "public"."corpora_corpus_validate_provision"();



CREATE OR REPLACE TRIGGER "corpora_corpus_validate_publication_claim" BEFORE INSERT ON "public"."corpora_corpus_publication_claims" FOR EACH ROW EXECUTE FUNCTION "public"."corpora_corpus_validate_publication_claim"();



CREATE OR REPLACE TRIGGER "corpora_corpus_validate_staged_input" BEFORE INSERT ON "public"."corpora_corpus_staged_inputs" FOR EACH ROW EXECUTE FUNCTION "public"."corpora_corpus_validate_staged_input"();



CREATE OR REPLACE TRIGGER "corpora_corpus_validate_version" BEFORE INSERT ON "public"."corpora_corpus_versions" FOR EACH ROW EXECUTE FUNCTION "public"."corpora_corpus_validate_version"();



CREATE CONSTRAINT TRIGGER "corpora_corpus_version_finalized" AFTER INSERT ON "public"."corpora_corpus_versions" DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION "public"."corpora_corpus_validate_finalized_version"();



CREATE OR REPLACE TRIGGER "corpora_corpus_version_intents_immutable" BEFORE DELETE OR UPDATE ON "public"."corpora_corpus_version_intents" FOR EACH ROW EXECUTE FUNCTION "public"."corpora_corpus_preserve_history"();



CREATE OR REPLACE TRIGGER "corpora_corpus_version_saves_immutable" BEFORE DELETE OR UPDATE ON "public"."corpora_corpus_version_saves" FOR EACH ROW EXECUTE FUNCTION "public"."corpora_corpus_preserve_history"();



CREATE OR REPLACE TRIGGER "corpora_corpus_versions_immutable" BEFORE DELETE OR UPDATE ON "public"."corpora_corpus_versions" FOR EACH ROW EXECUTE FUNCTION "public"."corpora_corpus_preserve_history"();



CREATE OR REPLACE TRIGGER "corpora_corpus_write_ops_no_delete" BEFORE DELETE ON "public"."corpora_corpus_write_ops" FOR EACH ROW EXECUTE FUNCTION "public"."corpora_corpus_preserve_history"();



CREATE OR REPLACE TRIGGER "corpora_touch" BEFORE UPDATE ON "public"."corpora" FOR EACH ROW EXECUTE FUNCTION "public"."touch_updated_at"();



CREATE OR REPLACE TRIGGER "devices_set_updated_at" BEFORE UPDATE ON "public"."devices" FOR EACH ROW EXECUTE FUNCTION "public"."set_updated_at"();



CREATE OR REPLACE TRIGGER "licences_set_updated_at" BEFORE UPDATE ON "public"."licences" FOR EACH ROW EXECUTE FUNCTION "public"."set_updated_at"();



CREATE OR REPLACE TRIGGER "project_references_touch" BEFORE UPDATE ON "public"."project_references" FOR EACH ROW EXECUTE FUNCTION "public"."touch_updated_at"();



CREATE OR REPLACE TRIGGER "projects_touch" BEFORE UPDATE ON "public"."projects" FOR EACH ROW EXECUTE FUNCTION "public"."touch_updated_at"();



CREATE OR REPLACE TRIGGER "repository_set_updated_at" BEFORE UPDATE ON "public"."repositories" FOR EACH ROW EXECUTE FUNCTION "public"."set_updated_at"();



CREATE OR REPLACE TRIGGER "users_set_updated_at" BEFORE UPDATE ON "public"."users" FOR EACH ROW EXECUTE FUNCTION "public"."set_updated_at"();



ALTER TABLE ONLY "corpora_session_guard"."superadmins"
    ADD CONSTRAINT "superadmins_user_id_fkey" FOREIGN KEY ("user_id") REFERENCES "auth"."users"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."books"
    ADD CONSTRAINT "books_corpus_id_fkey" FOREIGN KEY ("corpus_id") REFERENCES "public"."corpora"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."corpora"
    ADD CONSTRAINT "corpora_author_id_fkey" FOREIGN KEY ("author_id") REFERENCES "public"."authors"("id") ON DELETE SET NULL;



ALTER TABLE ONLY "public"."corpora_corpus_access"
    ADD CONSTRAINT "corpora_corpus_access_corpus_id_fkey" FOREIGN KEY ("corpus_id") REFERENCES "public"."corpora_corpus_metadata"("id") ON DELETE RESTRICT;



ALTER TABLE ONLY "public"."corpora_corpus_bootstrap"
    ADD CONSTRAINT "corpora_corpus_bootstrap_corpus_id_fkey" FOREIGN KEY ("corpus_id") REFERENCES "public"."corpora_corpus_metadata"("id") ON DELETE RESTRICT;



ALTER TABLE ONLY "public"."corpora_corpus_bootstrap"
    ADD CONSTRAINT "corpora_corpus_bootstrap_operation_id_corpus_id_worker_cla_fkey" FOREIGN KEY ("operation_id", "corpus_id", "worker_claim") REFERENCES "public"."corpora_corpus_provision_ops"("id", "corpus_id", "worker_claim") ON DELETE RESTRICT;



ALTER TABLE ONLY "public"."corpora_corpus_deletions"
    ADD CONSTRAINT "corpora_corpus_deletions_actor_id_fkey" FOREIGN KEY ("actor_id") REFERENCES "auth"."users"("id") ON DELETE RESTRICT;



ALTER TABLE ONLY "public"."corpora_corpus_deletions"
    ADD CONSTRAINT "corpora_corpus_deletions_corpus_id_fkey" FOREIGN KEY ("corpus_id") REFERENCES "public"."corpora_corpus_metadata"("id") ON DELETE RESTRICT;



ALTER TABLE ONLY "public"."corpora_corpus_heads"
    ADD CONSTRAINT "corpora_corpus_heads_corpus_id_fkey" FOREIGN KEY ("corpus_id") REFERENCES "public"."corpora_corpus_metadata"("id") ON DELETE RESTRICT;



ALTER TABLE ONLY "public"."corpora_corpus_heads"
    ADD CONSTRAINT "corpora_corpus_heads_corpus_id_sequence_commit_sha_fkey" FOREIGN KEY ("corpus_id", "sequence", "commit_sha") REFERENCES "public"."corpora_corpus_versions"("corpus_id", "sequence", "commit_sha") ON DELETE RESTRICT;



ALTER TABLE ONLY "public"."corpora_corpus_imports"
    ADD CONSTRAINT "corpora_corpus_imports_corpus_id_fkey" FOREIGN KEY ("corpus_id") REFERENCES "public"."corpora_corpus_metadata"("id") ON DELETE RESTRICT;



ALTER TABLE ONLY "public"."corpora_corpus_imports"
    ADD CONSTRAINT "corpora_corpus_imports_operation_id_fkey" FOREIGN KEY ("operation_id") REFERENCES "public"."corpora_corpus_write_ops"("id") ON DELETE RESTRICT;



ALTER TABLE ONLY "public"."corpora_corpus_metadata"
    ADD CONSTRAINT "corpora_corpus_metadata_owner_id_fkey" FOREIGN KEY ("owner_id") REFERENCES "auth"."users"("id") ON DELETE RESTRICT;



ALTER TABLE ONLY "public"."corpora_corpus_provision_ops"
    ADD CONSTRAINT "corpora_corpus_provision_ops_actor_id_fkey" FOREIGN KEY ("actor_id") REFERENCES "auth"."users"("id") ON DELETE RESTRICT;



ALTER TABLE ONLY "public"."corpora_corpus_provision_ops"
    ADD CONSTRAINT "corpora_corpus_provision_ops_corpus_id_fkey" FOREIGN KEY ("corpus_id") REFERENCES "public"."corpora_corpus_metadata"("id") ON DELETE RESTRICT;



ALTER TABLE ONLY "public"."corpora_corpus_publication_claims"
    ADD CONSTRAINT "corpora_corpus_publication_cl_operation_id_corpus_id_stage_fkey" FOREIGN KEY ("operation_id", "corpus_id", "stage_claim") REFERENCES "public"."corpora_corpus_staged_inputs"("operation_id", "corpus_id", "worker_claim") ON DELETE RESTRICT;



ALTER TABLE ONLY "public"."corpora_corpus_readers"
    ADD CONSTRAINT "corpora_corpus_readers_corpus_id_fkey" FOREIGN KEY ("corpus_id") REFERENCES "public"."corpora_corpus_metadata"("id") ON DELETE RESTRICT;



ALTER TABLE ONLY "public"."corpora_corpus_readers"
    ADD CONSTRAINT "corpora_corpus_readers_user_id_fkey" FOREIGN KEY ("user_id") REFERENCES "auth"."users"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."corpora_corpus_staged_inputs"
    ADD CONSTRAINT "corpora_corpus_staged_inputs_operation_id_corpus_id_worker_fkey" FOREIGN KEY ("operation_id", "corpus_id", "worker_claim") REFERENCES "public"."corpora_corpus_write_ops"("id", "corpus_id", "worker_claim") ON DELETE RESTRICT;



ALTER TABLE ONLY "public"."corpora_corpus_uncertain_discards"
    ADD CONSTRAINT "corpora_corpus_uncertain_discards_corpus_id_fkey" FOREIGN KEY ("corpus_id") REFERENCES "public"."corpora_corpus_metadata"("id") ON DELETE RESTRICT;



ALTER TABLE ONLY "public"."corpora_corpus_uncertain_discards"
    ADD CONSTRAINT "corpora_corpus_uncertain_discards_deletion_id_fkey" FOREIGN KEY ("deletion_id") REFERENCES "public"."corpora_corpus_deletions"("id") ON DELETE RESTRICT;



ALTER TABLE ONLY "public"."corpora_corpus_uncertain_discards"
    ADD CONSTRAINT "corpora_corpus_uncertain_discards_provision_id_fkey" FOREIGN KEY ("provision_id") REFERENCES "public"."corpora_corpus_provision_ops"("id") ON DELETE RESTRICT;



ALTER TABLE ONLY "public"."corpora_corpus_version_intents"
    ADD CONSTRAINT "corpora_corpus_version_intents_corpus_id_restore_sequence_fkey" FOREIGN KEY ("corpus_id", "restore_sequence") REFERENCES "public"."corpora_corpus_versions"("corpus_id", "sequence") ON DELETE RESTRICT;



ALTER TABLE ONLY "public"."corpora_corpus_version_intents"
    ADD CONSTRAINT "corpora_corpus_version_intents_operation_id_corpus_id_fkey" FOREIGN KEY ("operation_id", "corpus_id") REFERENCES "public"."corpora_corpus_version_saves"("operation_id", "corpus_id") ON DELETE RESTRICT;



ALTER TABLE ONLY "public"."corpora_corpus_version_saves"
    ADD CONSTRAINT "corpora_corpus_version_saves_corpus_id_fkey" FOREIGN KEY ("corpus_id") REFERENCES "public"."corpora_corpus_metadata"("id") ON DELETE RESTRICT;



ALTER TABLE ONLY "public"."corpora_corpus_version_saves"
    ADD CONSTRAINT "corpora_corpus_version_saves_operation_id_corpus_id_fkey" FOREIGN KEY ("operation_id", "corpus_id") REFERENCES "public"."corpora_corpus_write_ops"("id", "corpus_id") ON DELETE RESTRICT;



ALTER TABLE ONLY "public"."corpora_corpus_versions"
    ADD CONSTRAINT "corpora_corpus_versions_corpus_id_fkey" FOREIGN KEY ("corpus_id") REFERENCES "public"."corpora_corpus_metadata"("id") ON DELETE RESTRICT;



ALTER TABLE ONLY "public"."corpora_corpus_versions"
    ADD CONSTRAINT "corpora_corpus_versions_operation_id_corpus_id_fkey" FOREIGN KEY ("operation_id", "corpus_id") REFERENCES "public"."corpora_corpus_write_ops"("id", "corpus_id") ON DELETE RESTRICT;



ALTER TABLE ONLY "public"."corpora_corpus_versions"
    ADD CONSTRAINT "corpora_corpus_versions_operation_id_corpus_id_publication_fkey" FOREIGN KEY ("operation_id", "corpus_id", "publication_claim") REFERENCES "public"."corpora_corpus_publication_claims"("operation_id", "corpus_id", "publication_claim") ON DELETE RESTRICT;



ALTER TABLE ONLY "public"."corpora_corpus_write_ops"
    ADD CONSTRAINT "corpora_corpus_write_ops_actor_id_fkey" FOREIGN KEY ("actor_id") REFERENCES "auth"."users"("id") ON DELETE RESTRICT;



ALTER TABLE ONLY "public"."corpora_corpus_write_ops"
    ADD CONSTRAINT "corpora_corpus_write_ops_corpus_id_fkey" FOREIGN KEY ("corpus_id") REFERENCES "public"."corpora_corpus_metadata"("id") ON DELETE RESTRICT;



ALTER TABLE ONLY "public"."corpora_project_discoveries"
    ADD CONSTRAINT "corpora_project_discoveries_project_id_fkey" FOREIGN KEY ("project_id") REFERENCES "public"."projects"("id") ON DELETE RESTRICT;



ALTER TABLE ONLY "public"."corpora_project_discoveries"
    ADD CONSTRAINT "corpora_project_discoveries_source_corpus_fkey" FOREIGN KEY ("source_corpus") REFERENCES "public"."corpora_corpus_metadata"("id") ON DELETE RESTRICT;



ALTER TABLE ONLY "public"."corpora_project_discovery_revisions"
    ADD CONSTRAINT "corpora_project_discovery_revision_project_id_discovery_id_fkey" FOREIGN KEY ("project_id", "discovery_id") REFERENCES "public"."corpora_project_discoveries"("project_id", "discovery_id") ON DELETE RESTRICT;



ALTER TABLE ONLY "public"."corpora_project_link_heads"
    ADD CONSTRAINT "corpora_project_link_heads_project_id_fkey" FOREIGN KEY ("project_id") REFERENCES "public"."projects"("id") ON DELETE RESTRICT;



ALTER TABLE ONLY "public"."corpora_project_link_revisions"
    ADD CONSTRAINT "corpora_project_link_revisions_project_id_reference_id_fkey" FOREIGN KEY ("project_id", "reference_id") REFERENCES "public"."corpora_project_link_heads"("project_id", "reference_id") ON DELETE RESTRICT;



ALTER TABLE ONLY "public"."corpora_project_link_revisions"
    ADD CONSTRAINT "corpora_project_link_revisions_source_corpus_fkey" FOREIGN KEY ("source_corpus") REFERENCES "public"."corpora_corpus_metadata"("id") ON DELETE RESTRICT;



ALTER TABLE ONLY "public"."corpora_project_link_revisions"
    ADD CONSTRAINT "corpora_project_link_revisions_target_corpus_fkey" FOREIGN KEY ("target_corpus") REFERENCES "public"."corpora_corpus_metadata"("id") ON DELETE RESTRICT;



ALTER TABLE ONLY "public"."corpora_project_managed_corpora"
    ADD CONSTRAINT "corpora_project_managed_corpora_corpus_id_fkey" FOREIGN KEY ("corpus_id") REFERENCES "public"."corpora_corpus_metadata"("id") ON DELETE RESTRICT;



ALTER TABLE ONLY "public"."corpora_project_managed_corpora"
    ADD CONSTRAINT "corpora_project_managed_corpora_project_id_fkey" FOREIGN KEY ("project_id") REFERENCES "public"."projects"("id") ON DELETE RESTRICT;



ALTER TABLE ONLY "public"."corpora_project_members"
    ADD CONSTRAINT "corpora_project_members_project_id_fkey" FOREIGN KEY ("project_id") REFERENCES "public"."projects"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."corpora_project_members"
    ADD CONSTRAINT "corpora_project_members_user_id_fkey" FOREIGN KEY ("user_id") REFERENCES "auth"."users"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."corpus_changes"
    ADD CONSTRAINT "corpus_changes_applied_by_fkey" FOREIGN KEY ("applied_by") REFERENCES "public"."users"("id") ON DELETE RESTRICT;



ALTER TABLE ONLY "public"."corpus_changes"
    ADD CONSTRAINT "corpus_changes_commit_id_fkey" FOREIGN KEY ("commit_id") REFERENCES "public"."corpus_commits"("id") ON DELETE SET NULL;



ALTER TABLE ONLY "public"."corpus_changes"
    ADD CONSTRAINT "corpus_changes_document_id_fkey" FOREIGN KEY ("document_id") REFERENCES "public"."corpus_documents"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."corpus_changes"
    ADD CONSTRAINT "corpus_changes_reverts_change_id_fkey" FOREIGN KEY ("reverts_change_id") REFERENCES "public"."corpus_changes"("id") ON DELETE RESTRICT;



ALTER TABLE ONLY "public"."corpus_commits"
    ADD CONSTRAINT "corpus_commits_document_id_fkey" FOREIGN KEY ("document_id") REFERENCES "public"."corpus_documents"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."corpus_documents"
    ADD CONSTRAINT "corpus_documents_owner_id_fkey" FOREIGN KEY ("owner_id") REFERENCES "auth"."users"("id") ON DELETE RESTRICT;



ALTER TABLE ONLY "public"."project_corpora"
    ADD CONSTRAINT "project_corpora_corpus_id_fkey" FOREIGN KEY ("corpus_id") REFERENCES "public"."corpora"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."project_corpora"
    ADD CONSTRAINT "project_corpora_project_id_fkey" FOREIGN KEY ("project_id") REFERENCES "public"."projects"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."project_licences"
    ADD CONSTRAINT "project_licences_agreed_by_user_id_fkey" FOREIGN KEY ("agreed_by_user_id") REFERENCES "public"."user_directory"("id") ON DELETE RESTRICT;



ALTER TABLE ONLY "public"."project_licences"
    ADD CONSTRAINT "project_licences_licence_id_fkey" FOREIGN KEY ("licence_id") REFERENCES "public"."licences"("id") ON DELETE RESTRICT;



ALTER TABLE ONLY "public"."project_licences"
    ADD CONSTRAINT "project_licences_project_id_fkey" FOREIGN KEY ("project_id") REFERENCES "public"."projects"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."project_references"
    ADD CONSTRAINT "project_references_project_id_fkey" FOREIGN KEY ("project_id") REFERENCES "public"."projects"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."projects"
    ADD CONSTRAINT "projects_corpus_document_id_fkey" FOREIGN KEY ("corpus_document_id") REFERENCES "public"."corpus_documents"("id") ON DELETE SET NULL;



ALTER TABLE ONLY "public"."projects"
    ADD CONSTRAINT "projects_organization_id_fkey" FOREIGN KEY ("organization_id") REFERENCES "public"."organizations"("id") ON DELETE SET NULL;



ALTER TABLE ONLY "public"."projects"
    ADD CONSTRAINT "projects_owner_auth_fkey" FOREIGN KEY ("owner_id") REFERENCES "auth"."users"("id") ON DELETE RESTRICT;



ALTER TABLE ONLY "public"."projects"
    ADD CONSTRAINT "projects_user_id_fkey" FOREIGN KEY ("user_id") REFERENCES "public"."user_directory"("id") ON DELETE RESTRICT;



ALTER TABLE ONLY "public"."user_directory"
    ADD CONSTRAINT "user_directory_organization_id_fkey" FOREIGN KEY ("organization_id") REFERENCES "public"."organizations"("id") ON DELETE SET NULL;



ALTER TABLE ONLY "public"."users"
    ADD CONSTRAINT "users_address_id_fkey" FOREIGN KEY ("address_id") REFERENCES "public"."addresses"("id") ON DELETE SET NULL;



ALTER TABLE ONLY "public"."users"
    ADD CONSTRAINT "users_device_id_fkey" FOREIGN KEY ("device_id") REFERENCES "public"."users"("id") ON DELETE SET NULL;



ALTER TABLE ONLY "public"."users"
    ADD CONSTRAINT "users_id_fkey" FOREIGN KEY ("id") REFERENCES "auth"."users"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."users"
    ADD CONSTRAINT "users_organization_id_fkey" FOREIGN KEY ("organization_id") REFERENCES "public"."organizations"("id") ON DELETE SET NULL;



CREATE POLICY "Authenticated users can view logs" ON "public"."logs" FOR SELECT USING (("auth"."role"() = 'authenticated'::"text"));



CREATE POLICY "Service role can insert logs" ON "public"."logs" FOR INSERT WITH CHECK (("auth"."role"() = 'service_role'::"text"));



ALTER TABLE "public"."addresses" ENABLE ROW LEVEL SECURITY;


CREATE POLICY "addresses_insert_authenticated" ON "public"."addresses" FOR INSERT WITH CHECK (("auth"."role"() = 'authenticated'::"text"));



CREATE POLICY "addresses_select_authenticated" ON "public"."addresses" FOR SELECT USING (("auth"."role"() = 'authenticated'::"text"));



CREATE POLICY "addresses_update_authenticated" ON "public"."addresses" FOR UPDATE USING (("auth"."role"() = 'authenticated'::"text"));



CREATE POLICY "anon read orgs" ON "public"."organizations" FOR SELECT USING (true);



ALTER TABLE "public"."authors" ENABLE ROW LEVEL SECURITY;


CREATE POLICY "authors_select_public" ON "public"."authors" FOR SELECT USING (true);



ALTER TABLE "public"."books" ENABLE ROW LEVEL SECURITY;


CREATE POLICY "books_select_public" ON "public"."books" FOR SELECT USING (true);



ALTER TABLE "public"."conversion_jobs" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."corpora" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."corpora_corpus_access" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."corpora_corpus_bootstrap" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."corpora_corpus_deletions" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."corpora_corpus_heads" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."corpora_corpus_imports" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."corpora_corpus_metadata" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."corpora_corpus_provision_ops" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."corpora_corpus_publication_claims" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."corpora_corpus_readers" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."corpora_corpus_staged_inputs" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."corpora_corpus_uncertain_discards" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."corpora_corpus_version_intents" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."corpora_corpus_version_saves" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."corpora_corpus_versions" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."corpora_corpus_write_ops" ENABLE ROW LEVEL SECURITY;


CREATE POLICY "corpora_directory_read" ON "public"."user_directory" FOR SELECT TO "authenticated" USING ("public"."corpora_directory_reader"());



ALTER TABLE "public"."corpora_ingest_requests" ENABLE ROW LEVEL SECURITY;


CREATE POLICY "corpora_legacy_change_read" ON "public"."corpus_changes" FOR SELECT TO "authenticated" USING ((EXISTS ( SELECT 1
   FROM "public"."corpus_documents" "d"
  WHERE ("d"."id" = "corpus_changes"."document_id"))));



CREATE POLICY "corpora_legacy_commit_delete" ON "public"."corpus_commits" FOR DELETE TO "authenticated" USING ("public"."corpora_legacy_document_editable"("document_id"));



CREATE POLICY "corpora_legacy_commit_insert" ON "public"."corpus_commits" FOR INSERT TO "authenticated" WITH CHECK ("public"."corpora_legacy_document_editable"("document_id"));



CREATE POLICY "corpora_legacy_commit_read" ON "public"."corpus_commits" FOR SELECT TO "authenticated" USING ((EXISTS ( SELECT 1
   FROM "public"."corpus_documents" "d"
  WHERE ("d"."id" = "corpus_commits"."document_id"))));



CREATE POLICY "corpora_legacy_commit_update" ON "public"."corpus_commits" FOR UPDATE TO "authenticated" USING ("public"."corpora_legacy_document_editable"("document_id")) WITH CHECK ("public"."corpora_legacy_document_editable"("document_id"));



CREATE POLICY "corpora_legacy_document_delete" ON "public"."corpus_documents" FOR DELETE TO "authenticated" USING ((("owner_id" = "public"."corpora_project_actor"()) OR "public"."corpora_project_superadmin"()));



CREATE POLICY "corpora_legacy_document_insert" ON "public"."corpus_documents" FOR INSERT TO "authenticated" WITH CHECK ((("owner_id" = "public"."corpora_project_actor"()) OR "public"."corpora_project_superadmin"()));



CREATE POLICY "corpora_legacy_document_read" ON "public"."corpus_documents" FOR SELECT TO "authenticated" USING (("public"."corpora_directory_reader"() AND (("owner_id" = "auth"."uid"()) OR "public"."corpora_legacy_superadmin_readable"())));



CREATE POLICY "corpora_legacy_document_update" ON "public"."corpus_documents" FOR UPDATE TO "authenticated" USING ((("owner_id" = "public"."corpora_project_actor"()) OR "public"."corpora_project_superadmin"())) WITH CHECK ((("owner_id" = "public"."corpora_project_actor"()) OR "public"."corpora_project_superadmin"()));



CREATE POLICY "corpora_licence_catalog_anon" ON "public"."licences" FOR SELECT TO "anon" USING (true);



CREATE POLICY "corpora_licence_catalog_insert" ON "public"."licences" FOR INSERT TO "authenticated" WITH CHECK ("public"."corpora_project_superadmin"());



CREATE POLICY "corpora_licence_catalog_update" ON "public"."licences" FOR UPDATE TO "authenticated" USING ("public"."corpora_project_superadmin"()) WITH CHECK ("public"."corpora_project_superadmin"());



CREATE POLICY "corpora_licence_catalog_user" ON "public"."licences" FOR SELECT TO "authenticated" USING ("public"."corpora_directory_reader"());



ALTER TABLE "public"."corpora_log" ENABLE ROW LEVEL SECURITY;


CREATE POLICY "corpora_log_select_anon" ON "public"."corpora_log" FOR SELECT USING (true);



CREATE POLICY "corpora_project_association_delete" ON "public"."project_corpora" FOR DELETE TO "authenticated" USING ("public"."corpora_project_association_editable"("project_id"));



CREATE POLICY "corpora_project_association_insert" ON "public"."project_corpora" FOR INSERT TO "authenticated" WITH CHECK ("public"."corpora_project_association_editable"("project_id"));



CREATE POLICY "corpora_project_association_read" ON "public"."project_corpora" FOR SELECT TO "authenticated" USING ("public"."corpora_project_association_readable"("project_id"));



CREATE POLICY "corpora_project_association_update" ON "public"."project_corpora" FOR UPDATE TO "authenticated" USING ("public"."corpora_project_association_editable"("project_id")) WITH CHECK ("public"."corpora_project_association_editable"("project_id"));



CREATE POLICY "corpora_project_create" ON "public"."projects" FOR INSERT TO "authenticated" WITH CHECK ((("owner_id" = "public"."corpora_project_actor"()) OR "public"."corpora_project_superadmin"()));



CREATE POLICY "corpora_project_delete" ON "public"."projects" FOR DELETE TO "authenticated" USING ((("owner_id" = "public"."corpora_project_actor"()) OR "public"."corpora_project_superadmin"()));



ALTER TABLE "public"."corpora_project_discoveries" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."corpora_project_discovery_revisions" ENABLE ROW LEVEL SECURITY;


CREATE POLICY "corpora_project_licence_delete" ON "public"."project_licences" FOR DELETE TO "authenticated" USING ("public"."corpora_project_association_editable"("project_id"));



CREATE POLICY "corpora_project_licence_insert" ON "public"."project_licences" FOR INSERT TO "authenticated" WITH CHECK ("public"."corpora_project_association_editable"("project_id"));



CREATE POLICY "corpora_project_licence_read_anon" ON "public"."project_licences" FOR SELECT TO "anon" USING (true);



CREATE POLICY "corpora_project_licence_read_user" ON "public"."project_licences" FOR SELECT TO "authenticated" USING ("public"."corpora_directory_reader"());



CREATE POLICY "corpora_project_licence_update" ON "public"."project_licences" FOR UPDATE TO "authenticated" USING ("public"."corpora_project_association_editable"("project_id")) WITH CHECK ("public"."corpora_project_association_editable"("project_id"));



ALTER TABLE "public"."corpora_project_link_heads" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."corpora_project_link_revisions" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."corpora_project_managed_corpora" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."corpora_project_members" ENABLE ROW LEVEL SECURITY;


CREATE POLICY "corpora_project_read" ON "public"."projects" FOR SELECT TO "anon", "authenticated" USING (true);



CREATE POLICY "corpora_project_update" ON "public"."projects" FOR UPDATE TO "authenticated" USING ((("owner_id" = "public"."corpora_project_actor"()) OR "public"."corpora_project_superadmin"())) WITH CHECK ((("owner_id" = "public"."corpora_project_actor"()) OR "public"."corpora_project_superadmin"()));



ALTER TABLE "public"."corpus_changes" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."corpus_commits" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."corpus_documents" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."devices" ENABLE ROW LEVEL SECURITY;


CREATE POLICY "devices_insert_authenticated" ON "public"."devices" FOR INSERT WITH CHECK (("auth"."role"() = 'authenticated'::"text"));



CREATE POLICY "devices_select_authenticated" ON "public"."devices" FOR SELECT USING (("auth"."role"() = 'authenticated'::"text"));



CREATE POLICY "devices_update_authenticated" ON "public"."devices" FOR UPDATE USING (("auth"."role"() = 'authenticated'::"text"));



CREATE POLICY "ingest_bound_jobs_insert" ON "public"."conversion_jobs" AS RESTRICTIVE FOR INSERT WITH CHECK ((("owner" = (( SELECT "auth"."uid"() AS "uid"))::"text") AND ("result_key" = (((('conversion-jobs/'::"text" || "owner") || '/'::"text") || "id") || '.graph.json'::"text")) AND ("id" ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'::"text") AND ("output_format" = 'corpus'::"text") AND ("status" = 'succeeded'::"text")));



CREATE POLICY "ingest_bound_jobs_select" ON "public"."conversion_jobs" AS RESTRICTIVE FOR SELECT USING (("owner" = (( SELECT "auth"."uid"() AS "uid"))::"text"));



CREATE POLICY "ingest_bound_requests_insert" ON "public"."corpora_ingest_requests" AS RESTRICTIVE FOR INSERT WITH CHECK (("owner_id" = ( SELECT "auth"."uid"() AS "uid")));



CREATE POLICY "ingest_bound_requests_select" ON "public"."corpora_ingest_requests" AS RESTRICTIVE FOR SELECT USING (("owner_id" = ( SELECT "auth"."uid"() AS "uid")));



CREATE POLICY "ingest_own_jobs_insert" ON "public"."conversion_jobs" FOR INSERT TO "authenticated" WITH CHECK ((("owner" = (( SELECT "auth"."uid"() AS "uid"))::"text") AND ("result_key" = (((('conversion-jobs/'::"text" || "owner") || '/'::"text") || "id") || '.graph.json'::"text")) AND ("id" ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'::"text") AND ("output_format" = 'corpus'::"text") AND ("status" = 'succeeded'::"text")));



CREATE POLICY "ingest_own_jobs_select" ON "public"."conversion_jobs" FOR SELECT TO "authenticated" USING (("owner" = (( SELECT "auth"."uid"() AS "uid"))::"text"));



CREATE POLICY "ingest_own_requests_insert" ON "public"."corpora_ingest_requests" FOR INSERT TO "authenticated" WITH CHECK (("owner_id" = ( SELECT "auth"."uid"() AS "uid")));



CREATE POLICY "ingest_own_requests_select" ON "public"."corpora_ingest_requests" FOR SELECT TO "authenticated" USING (("owner_id" = ( SELECT "auth"."uid"() AS "uid")));



CREATE POLICY "ingest_session_jobs_insert" ON "public"."conversion_jobs" AS RESTRICTIVE FOR INSERT TO "authenticated" WITH CHECK (( SELECT "public"."corpora_ingest_session_active"() AS "corpora_ingest_session_active"));



CREATE POLICY "ingest_session_jobs_select" ON "public"."conversion_jobs" AS RESTRICTIVE FOR SELECT TO "authenticated" USING (( SELECT "public"."corpora_ingest_session_active"() AS "corpora_ingest_session_active"));



CREATE POLICY "ingest_session_requests_insert" ON "public"."corpora_ingest_requests" AS RESTRICTIVE FOR INSERT TO "authenticated" WITH CHECK (( SELECT "public"."corpora_ingest_session_active"() AS "corpora_ingest_session_active"));



CREATE POLICY "ingest_session_requests_select" ON "public"."corpora_ingest_requests" AS RESTRICTIVE FOR SELECT TO "authenticated" USING (( SELECT "public"."corpora_ingest_session_active"() AS "corpora_ingest_session_active"));



ALTER TABLE "public"."licences" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."logs" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."organizations" ENABLE ROW LEVEL SECURITY;


CREATE POLICY "organizations_authenticated_insert" ON "public"."organizations" FOR INSERT TO "authenticated" WITH CHECK (true);



CREATE POLICY "organizations_live_session_insert" ON "public"."organizations" AS RESTRICTIVE FOR INSERT TO "authenticated" WITH CHECK ("public"."corpora_directory_reader"());



CREATE POLICY "organizations_no_anon_insert" ON "public"."organizations" AS RESTRICTIVE FOR INSERT TO "anon" WITH CHECK (false);



CREATE POLICY "organizations_no_client_update" ON "public"."organizations" AS RESTRICTIVE FOR UPDATE TO "anon", "authenticated" USING (false) WITH CHECK (false);



CREATE POLICY "organizations_select_authenticated" ON "public"."organizations" FOR SELECT USING (("auth"."role"() = 'authenticated'::"text"));



ALTER TABLE "public"."project_corpora" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."project_licences" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."project_references" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."projects" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."repositories" ENABLE ROW LEVEL SECURITY;


CREATE POLICY "repositories_insert_authenticated" ON "public"."repositories" FOR INSERT WITH CHECK (("auth"."role"() = 'authenticated'::"text"));



CREATE POLICY "repositories_select_public" ON "public"."repositories" FOR SELECT USING (true);



CREATE POLICY "repositories_update_authenticated" ON "public"."repositories" FOR UPDATE USING (("auth"."role"() = 'authenticated'::"text"));



ALTER TABLE "public"."user_directory" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."users" ENABLE ROW LEVEL SECURITY;


CREATE POLICY "users_select_own" ON "public"."users" FOR SELECT USING (("auth"."uid"() = "id"));



CREATE POLICY "users_update_own" ON "public"."users" FOR UPDATE USING ((( SELECT "auth"."uid"() AS "uid") = "id")) WITH CHECK ((( SELECT "auth"."uid"() AS "uid") = "id"));



GRANT USAGE ON SCHEMA "corpora_session_guard" TO "service_role";



GRANT USAGE ON SCHEMA "public" TO "postgres";
GRANT USAGE ON SCHEMA "public" TO "anon";
GRANT USAGE ON SCHEMA "public" TO "authenticated";
GRANT USAGE ON SCHEMA "public" TO "service_role";



REVOKE ALL ON FUNCTION "corpora_session_guard"."admit_provision"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid") FROM PUBLIC;
GRANT ALL ON FUNCTION "corpora_session_guard"."admit_provision"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid") TO "service_role";



REVOKE ALL ON FUNCTION "corpora_session_guard"."begin_corpus_edit"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_operation" "uuid", "p_sequence" bigint, "p_parent" "text", "p_digest" "text", "p_size" bigint, "p_intent" "text", "p_kind" "text", "p_restore" bigint) FROM PUBLIC;
GRANT ALL ON FUNCTION "corpora_session_guard"."begin_corpus_edit"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_operation" "uuid", "p_sequence" bigint, "p_parent" "text", "p_digest" "text", "p_size" bigint, "p_intent" "text", "p_kind" "text", "p_restore" bigint) TO "service_role";



REVOKE ALL ON FUNCTION "corpora_session_guard"."begin_creation"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_title" "text", "p_organization" "text") FROM PUBLIC;
GRANT ALL ON FUNCTION "corpora_session_guard"."begin_creation"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_title" "text", "p_organization" "text") TO "service_role";



REVOKE ALL ON FUNCTION "corpora_session_guard"."begin_deletion"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_operation" "uuid", "p_revision" bigint) FROM PUBLIC;
GRANT ALL ON FUNCTION "corpora_session_guard"."begin_deletion"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_operation" "uuid", "p_revision" bigint) TO "service_role";



REVOKE ALL ON FUNCTION "corpora_session_guard"."begin_import"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_operation" "uuid", "p_revision" bigint, "p_digest" "text", "p_size" bigint) FROM PUBLIC;
GRANT ALL ON FUNCTION "corpora_session_guard"."begin_import"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_operation" "uuid", "p_revision" bigint, "p_digest" "text", "p_size" bigint) TO "service_role";



REVOKE ALL ON FUNCTION "corpora_session_guard"."begin_uncertain_discard"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_operation" "uuid", "p_revision" bigint, "p_observed_sha" "text") FROM PUBLIC;
GRANT ALL ON FUNCTION "corpora_session_guard"."begin_uncertain_discard"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_operation" "uuid", "p_revision" bigint, "p_observed_sha" "text") TO "service_role";



REVOKE ALL ON FUNCTION "corpora_session_guard"."begin_version_save"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_operation" "uuid", "p_sequence" bigint, "p_parent" "text", "p_digest" "text", "p_size" bigint) FROM PUBLIC;
GRANT ALL ON FUNCTION "corpora_session_guard"."begin_version_save"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_operation" "uuid", "p_sequence" bigint, "p_parent" "text", "p_digest" "text", "p_size" bigint) TO "service_role";



REVOKE ALL ON FUNCTION "corpora_session_guard"."complete_import"("p_operation" "uuid", "p_claim" "uuid", "p_publication" "uuid", "p_sha" "text") FROM PUBLIC;
GRANT ALL ON FUNCTION "corpora_session_guard"."complete_import"("p_operation" "uuid", "p_claim" "uuid", "p_publication" "uuid", "p_sha" "text") TO "service_role";



REVOKE ALL ON FUNCTION "corpora_session_guard"."complete_version_save"("p_operation" "uuid", "p_claim" "uuid", "p_publication" "uuid", "p_sha" "text") FROM PUBLIC;
GRANT ALL ON FUNCTION "corpora_session_guard"."complete_version_save"("p_operation" "uuid", "p_claim" "uuid", "p_publication" "uuid", "p_sha" "text") TO "service_role";



REVOKE ALL ON FUNCTION "corpora_session_guard"."corpus_reader_add_email"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_email" "text") FROM PUBLIC;
GRANT ALL ON FUNCTION "corpora_session_guard"."corpus_reader_add_email"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_email" "text") TO "service_role";



REVOKE ALL ON FUNCTION "corpora_session_guard"."corpus_reader_set"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_reader" "uuid", "p_present" boolean) FROM PUBLIC;
GRANT ALL ON FUNCTION "corpora_session_guard"."corpus_reader_set"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_reader" "uuid", "p_present" boolean) TO "service_role";



REVOKE ALL ON FUNCTION "corpora_session_guard"."corpus_readers_list"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_after" "uuid", "p_limit" integer) FROM PUBLIC;
GRANT ALL ON FUNCTION "corpora_session_guard"."corpus_readers_list"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_after" "uuid", "p_limit" integer) TO "service_role";



REVOKE ALL ON FUNCTION "corpora_session_guard"."creation_status"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid") FROM PUBLIC;
GRANT ALL ON FUNCTION "corpora_session_guard"."creation_status"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid") TO "service_role";



REVOKE ALL ON FUNCTION "corpora_session_guard"."deletion_status"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_operation" "uuid") FROM PUBLIC;
GRANT ALL ON FUNCTION "corpora_session_guard"."deletion_status"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_operation" "uuid") TO "service_role";



REVOKE ALL ON FUNCTION "corpora_session_guard"."edit_intent_status"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_operation" "uuid", "p_intent" "text") FROM PUBLIC;
GRANT ALL ON FUNCTION "corpora_session_guard"."edit_intent_status"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_operation" "uuid", "p_intent" "text") TO "service_role";



REVOKE ALL ON FUNCTION "corpora_session_guard"."finalize_provision"("p_corpus" "uuid", "p_operation" "uuid", "p_claim" "uuid", "p_repo" "text", "p_type" "text", "p_sha" "text") FROM PUBLIC;
GRANT ALL ON FUNCTION "corpora_session_guard"."finalize_provision"("p_corpus" "uuid", "p_operation" "uuid", "p_claim" "uuid", "p_repo" "text", "p_type" "text", "p_sha" "text") TO "service_role";



REVOKE ALL ON FUNCTION "corpora_session_guard"."finish_deletion"("p_operation" "uuid", "p_claim" "uuid", "p_completed" boolean) FROM PUBLIC;
GRANT ALL ON FUNCTION "corpora_session_guard"."finish_deletion"("p_operation" "uuid", "p_claim" "uuid", "p_completed" boolean) TO "service_role";



REVOKE ALL ON FUNCTION "corpora_session_guard"."get_metadata"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid") FROM PUBLIC;
GRANT ALL ON FUNCTION "corpora_session_guard"."get_metadata"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid") TO "service_role";



REVOKE ALL ON FUNCTION "corpora_session_guard"."guard_access"() FROM PUBLIC;



REVOKE ALL ON FUNCTION "corpora_session_guard"."guard_deletion"() FROM PUBLIC;



REVOKE ALL ON FUNCTION "corpora_session_guard"."import_reconciliation_target"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_operation" "uuid") FROM PUBLIC;
GRANT ALL ON FUNCTION "corpora_session_guard"."import_reconciliation_target"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_operation" "uuid") TO "service_role";



REVOKE ALL ON FUNCTION "corpora_session_guard"."import_status"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_operation" "uuid") FROM PUBLIC;
GRANT ALL ON FUNCTION "corpora_session_guard"."import_status"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_operation" "uuid") TO "service_role";



REVOKE ALL ON FUNCTION "corpora_session_guard"."initialize_access"() FROM PUBLIC;



REVOKE ALL ON FUNCTION "corpora_session_guard"."list_corpus_versions"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_before" bigint, "p_limit" integer) FROM PUBLIC;
GRANT ALL ON FUNCTION "corpora_session_guard"."list_corpus_versions"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_before" bigint, "p_limit" integer) TO "service_role";



REVOKE ALL ON FUNCTION "corpora_session_guard"."list_metadata"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_owner" "uuid", "p_after" "uuid", "p_limit" integer) FROM PUBLIC;
GRANT ALL ON FUNCTION "corpora_session_guard"."list_metadata"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_owner" "uuid", "p_after" "uuid", "p_limit" integer) TO "service_role";



REVOKE ALL ON FUNCTION "corpora_session_guard"."lock_verified_session"("p_owner" "uuid", "p_session" "uuid", "p_token_exp" bigint) FROM PUBLIC;
GRANT ALL ON FUNCTION "corpora_session_guard"."lock_verified_session"("p_owner" "uuid", "p_session" "uuid", "p_token_exp" bigint) TO "service_role";



REVOKE ALL ON FUNCTION "corpora_session_guard"."mark_import_uncertain"("p_operation" "uuid", "p_claim" "uuid") FROM PUBLIC;
GRANT ALL ON FUNCTION "corpora_session_guard"."mark_import_uncertain"("p_operation" "uuid", "p_claim" "uuid") TO "service_role";



REVOKE ALL ON FUNCTION "corpora_session_guard"."mark_provision_uncertain"("p_operation" "uuid", "p_claim" "uuid") FROM PUBLIC;
GRANT ALL ON FUNCTION "corpora_session_guard"."mark_provision_uncertain"("p_operation" "uuid", "p_claim" "uuid") TO "service_role";



REVOKE ALL ON FUNCTION "corpora_session_guard"."mark_version_save_uncertain"("p_operation" "uuid", "p_claim" "uuid") FROM PUBLIC;
GRANT ALL ON FUNCTION "corpora_session_guard"."mark_version_save_uncertain"("p_operation" "uuid", "p_claim" "uuid") TO "service_role";



REVOKE ALL ON FUNCTION "corpora_session_guard"."patch_metadata"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_revision" bigint, "p_fields" "jsonb") FROM PUBLIC;
GRANT ALL ON FUNCTION "corpora_session_guard"."patch_metadata"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_revision" bigint, "p_fields" "jsonb") TO "service_role";



REVOKE ALL ON FUNCTION "corpora_session_guard"."project_corpora_list"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_project" "uuid", "p_linked_only" boolean, "p_after" "uuid", "p_limit" integer) FROM PUBLIC;
GRANT ALL ON FUNCTION "corpora_session_guard"."project_corpora_list"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_project" "uuid", "p_linked_only" boolean, "p_after" "uuid", "p_limit" integer) TO "service_role";



REVOKE ALL ON FUNCTION "corpora_session_guard"."project_corpus_set"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_project" "uuid", "p_corpus" "uuid", "p_present" boolean) FROM PUBLIC;
GRANT ALL ON FUNCTION "corpora_session_guard"."project_corpus_set"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_project" "uuid", "p_corpus" "uuid", "p_present" boolean) TO "service_role";



REVOKE ALL ON FUNCTION "corpora_session_guard"."project_discovery_admit"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_project" "uuid", "p_discovery" "uuid", "p_reason" "text", "p_evidence" "jsonb") FROM PUBLIC;
GRANT ALL ON FUNCTION "corpora_session_guard"."project_discovery_admit"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_project" "uuid", "p_discovery" "uuid", "p_reason" "text", "p_evidence" "jsonb") TO "service_role";



REVOKE ALL ON FUNCTION "corpora_session_guard"."project_discovery_decide"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_project" "uuid", "p_discovery" "uuid", "p_expected" bigint, "p_reason" "text", "p_action" "text") FROM PUBLIC;
GRANT ALL ON FUNCTION "corpora_session_guard"."project_discovery_decide"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_project" "uuid", "p_discovery" "uuid", "p_expected" bigint, "p_reason" "text", "p_action" "text") TO "service_role";



REVOKE ALL ON FUNCTION "corpora_session_guard"."project_discovery_history"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_project" "uuid", "p_discovery" "uuid", "p_before" bigint, "p_limit" integer) FROM PUBLIC;
GRANT ALL ON FUNCTION "corpora_session_guard"."project_discovery_history"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_project" "uuid", "p_discovery" "uuid", "p_before" bigint, "p_limit" integer) TO "service_role";



REVOKE ALL ON FUNCTION "corpora_session_guard"."project_discovery_register"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_project" "uuid", "p_discovery" "uuid", "p_reason" "text", "p_evidence" "jsonb") FROM PUBLIC;
GRANT ALL ON FUNCTION "corpora_session_guard"."project_discovery_register"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_project" "uuid", "p_discovery" "uuid", "p_reason" "text", "p_evidence" "jsonb") TO "service_role";



REVOKE ALL ON FUNCTION "corpora_session_guard"."project_link_access"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_project" "uuid", "p_edit" boolean) FROM PUBLIC;



REVOKE ALL ON FUNCTION "corpora_session_guard"."project_link_current"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_project" "uuid", "p_reference" "uuid") FROM PUBLIC;
GRANT ALL ON FUNCTION "corpora_session_guard"."project_link_current"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_project" "uuid", "p_reference" "uuid") TO "service_role";



REVOKE ALL ON FUNCTION "corpora_session_guard"."project_link_decide"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_project" "uuid", "p_reference" "uuid", "p_expected" bigint, "p_reason" "text", "p_action" "text") FROM PUBLIC;
GRANT ALL ON FUNCTION "corpora_session_guard"."project_link_decide"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_project" "uuid", "p_reference" "uuid", "p_expected" bigint, "p_reason" "text", "p_action" "text") TO "service_role";



REVOKE ALL ON FUNCTION "corpora_session_guard"."project_link_export"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_project" "uuid", "p_references" "uuid"[]) FROM PUBLIC;
GRANT ALL ON FUNCTION "corpora_session_guard"."project_link_export"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_project" "uuid", "p_references" "uuid"[]) TO "service_role";



REVOKE ALL ON FUNCTION "corpora_session_guard"."project_link_history"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_project" "uuid", "p_reference" "uuid", "p_before" bigint, "p_limit" integer) FROM PUBLIC;
GRANT ALL ON FUNCTION "corpora_session_guard"."project_link_history"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_project" "uuid", "p_reference" "uuid", "p_before" bigint, "p_limit" integer) TO "service_role";



REVOKE ALL ON FUNCTION "corpora_session_guard"."project_link_read_current"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_project" "uuid", "p_reference" "uuid") FROM PUBLIC;
GRANT ALL ON FUNCTION "corpora_session_guard"."project_link_read_current"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_project" "uuid", "p_reference" "uuid") TO "service_role";



REVOKE ALL ON FUNCTION "corpora_session_guard"."project_link_save"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_project" "uuid", "p_reference" "uuid", "p_expected" bigint, "p_reason" "text", "p_value" "jsonb") FROM PUBLIC;
GRANT ALL ON FUNCTION "corpora_session_guard"."project_link_save"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_project" "uuid", "p_reference" "uuid", "p_expected" bigint, "p_reason" "text", "p_value" "jsonb") TO "service_role";



REVOKE ALL ON FUNCTION "corpora_session_guard"."project_member_add_email"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_project" "uuid", "p_email" "text") FROM PUBLIC;
GRANT ALL ON FUNCTION "corpora_session_guard"."project_member_add_email"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_project" "uuid", "p_email" "text") TO "service_role";



REVOKE ALL ON FUNCTION "corpora_session_guard"."project_member_set"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_project" "uuid", "p_member" "uuid", "p_present" boolean) FROM PUBLIC;
GRANT ALL ON FUNCTION "corpora_session_guard"."project_member_set"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_project" "uuid", "p_member" "uuid", "p_present" boolean) TO "service_role";



REVOKE ALL ON FUNCTION "corpora_session_guard"."project_members_list"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_project" "uuid", "p_after" "uuid", "p_limit" integer) FROM PUBLIC;
GRANT ALL ON FUNCTION "corpora_session_guard"."project_members_list"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_project" "uuid", "p_after" "uuid", "p_limit" integer) TO "service_role";



REVOKE ALL ON FUNCTION "corpora_session_guard"."read_access"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid") FROM PUBLIC;
GRANT ALL ON FUNCTION "corpora_session_guard"."read_access"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid") TO "service_role";



REVOKE ALL ON FUNCTION "corpora_session_guard"."reconcile_version_save"("p_operation" "uuid", "p_claim" "uuid", "p_publication" "uuid", "p_sha" "text") FROM PUBLIC;
GRANT ALL ON FUNCTION "corpora_session_guard"."reconcile_version_save"("p_operation" "uuid", "p_claim" "uuid", "p_publication" "uuid", "p_sha" "text") TO "service_role";



REVOKE ALL ON FUNCTION "corpora_session_guard"."reconciliation_target"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_operation" "uuid") FROM PUBLIC;
GRANT ALL ON FUNCTION "corpora_session_guard"."reconciliation_target"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_operation" "uuid") TO "service_role";



REVOKE ALL ON FUNCTION "corpora_session_guard"."require_live_corpus"() FROM PUBLIC;



REVOKE ALL ON FUNCTION "corpora_session_guard"."restore_source"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_sequence" bigint) FROM PUBLIC;
GRANT ALL ON FUNCTION "corpora_session_guard"."restore_source"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_sequence" bigint) TO "service_role";



REVOKE ALL ON FUNCTION "corpora_session_guard"."set_visibility"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_revision" bigint, "p_visibility" "text") FROM PUBLIC;
GRANT ALL ON FUNCTION "corpora_session_guard"."set_visibility"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_revision" bigint, "p_visibility" "text") TO "service_role";



REVOKE ALL ON FUNCTION "corpora_session_guard"."shared_corpora_list"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_after" "uuid", "p_limit" integer) FROM PUBLIC;
GRANT ALL ON FUNCTION "corpora_session_guard"."shared_corpora_list"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_after" "uuid", "p_limit" integer) TO "service_role";



REVOKE ALL ON FUNCTION "corpora_session_guard"."stage_import"("p_operation" "uuid", "p_claim" "uuid", "p_stage_sha" "text", "p_publication" "uuid") FROM PUBLIC;
GRANT ALL ON FUNCTION "corpora_session_guard"."stage_import"("p_operation" "uuid", "p_claim" "uuid", "p_stage_sha" "text", "p_publication" "uuid") TO "service_role";



REVOKE ALL ON FUNCTION "corpora_session_guard"."stage_version_save"("p_operation" "uuid", "p_claim" "uuid", "p_stage_sha" "text", "p_publication" "uuid") FROM PUBLIC;
GRANT ALL ON FUNCTION "corpora_session_guard"."stage_version_save"("p_operation" "uuid", "p_claim" "uuid", "p_stage_sha" "text", "p_publication" "uuid") TO "service_role";



REVOKE ALL ON FUNCTION "corpora_session_guard"."uncertain_discard_target"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_revision" bigint) FROM PUBLIC;
GRANT ALL ON FUNCTION "corpora_session_guard"."uncertain_discard_target"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_revision" bigint) TO "service_role";



REVOKE ALL ON FUNCTION "corpora_session_guard"."version_save_status"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_operation" "uuid") FROM PUBLIC;
GRANT ALL ON FUNCTION "corpora_session_guard"."version_save_status"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_operation" "uuid") TO "service_role";



REVOKE ALL ON FUNCTION "public"."corpora_add_corpus_reader_by_email"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_email" "text") FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."corpora_add_corpus_reader_by_email"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_email" "text") TO "service_role";



REVOKE ALL ON FUNCTION "public"."corpora_add_project_member_by_email"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_project" "uuid", "p_email" "text") FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."corpora_add_project_member_by_email"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_project" "uuid", "p_email" "text") TO "service_role";



REVOKE ALL ON FUNCTION "public"."corpora_admit_project_discovery"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_project" "uuid", "p_discovery" "uuid", "p_reason" "text", "p_evidence" "jsonb") FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."corpora_admit_project_discovery"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_project" "uuid", "p_discovery" "uuid", "p_reason" "text", "p_evidence" "jsonb") TO "service_role";



REVOKE ALL ON FUNCTION "public"."corpora_begin_corpus_deletion"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_operation" "uuid", "p_revision" bigint) FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."corpora_begin_corpus_deletion"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_operation" "uuid", "p_revision" bigint) TO "service_role";



REVOKE ALL ON FUNCTION "public"."corpora_begin_corpus_edit"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_operation" "uuid", "p_sequence" bigint, "p_parent" "text", "p_digest" "text", "p_size" bigint, "p_intent" "text", "p_kind" "text", "p_restore" bigint) FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."corpora_begin_corpus_edit"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_operation" "uuid", "p_sequence" bigint, "p_parent" "text", "p_digest" "text", "p_size" bigint, "p_intent" "text", "p_kind" "text", "p_restore" bigint) TO "service_role";



REVOKE ALL ON FUNCTION "public"."corpora_begin_creation"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_title" "text", "p_organization" "text") FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."corpora_begin_creation"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_title" "text", "p_organization" "text") TO "service_role";



REVOKE ALL ON FUNCTION "public"."corpora_begin_import"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_operation" "uuid", "p_revision" bigint, "p_digest" "text", "p_size" bigint) FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."corpora_begin_import"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_operation" "uuid", "p_revision" bigint, "p_digest" "text", "p_size" bigint) TO "service_role";



REVOKE ALL ON FUNCTION "public"."corpora_begin_uncertain_discard"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_operation" "uuid", "p_revision" bigint, "p_observed_sha" "text") FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."corpora_begin_uncertain_discard"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_operation" "uuid", "p_revision" bigint, "p_observed_sha" "text") TO "service_role";



REVOKE ALL ON FUNCTION "public"."corpora_begin_version_save"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_operation" "uuid", "p_sequence" bigint, "p_parent" "text", "p_digest" "text", "p_size" bigint) FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."corpora_begin_version_save"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_operation" "uuid", "p_sequence" bigint, "p_parent" "text", "p_digest" "text", "p_size" bigint) TO "service_role";



REVOKE ALL ON FUNCTION "public"."corpora_complete_import"("p_operation" "uuid", "p_claim" "uuid", "p_publication" "uuid", "p_sha" "text") FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."corpora_complete_import"("p_operation" "uuid", "p_claim" "uuid", "p_publication" "uuid", "p_sha" "text") TO "service_role";



REVOKE ALL ON FUNCTION "public"."corpora_complete_version_save"("p_operation" "uuid", "p_claim" "uuid", "p_publication" "uuid", "p_sha" "text") FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."corpora_complete_version_save"("p_operation" "uuid", "p_claim" "uuid", "p_publication" "uuid", "p_sha" "text") TO "service_role";



REVOKE ALL ON FUNCTION "public"."corpora_corpus_bootstrap_finalized"() FROM PUBLIC;



REVOKE ALL ON FUNCTION "public"."corpora_corpus_creation_input"() FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."corpora_corpus_creation_input"() TO "service_role";



REVOKE ALL ON FUNCTION "public"."corpora_corpus_metadata_stamp"() FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."corpora_corpus_metadata_stamp"() TO "service_role";



REVOKE ALL ON FUNCTION "public"."corpora_corpus_preserve_history"() FROM PUBLIC;



REVOKE ALL ON FUNCTION "public"."corpora_corpus_validate_bootstrap"() FROM PUBLIC;



REVOKE ALL ON FUNCTION "public"."corpora_corpus_validate_finalized_version"() FROM PUBLIC;



REVOKE ALL ON FUNCTION "public"."corpora_corpus_validate_head"() FROM PUBLIC;



REVOKE ALL ON FUNCTION "public"."corpora_corpus_validate_operation"() FROM PUBLIC;



REVOKE ALL ON FUNCTION "public"."corpora_corpus_validate_provision"() FROM PUBLIC;



REVOKE ALL ON FUNCTION "public"."corpora_corpus_validate_publication_claim"() FROM PUBLIC;



REVOKE ALL ON FUNCTION "public"."corpora_corpus_validate_staged_input"() FROM PUBLIC;



REVOKE ALL ON FUNCTION "public"."corpora_corpus_validate_version"() FROM PUBLIC;



REVOKE ALL ON FUNCTION "public"."corpora_current_project_link"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_project" "uuid", "p_reference" "uuid") FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."corpora_current_project_link"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_project" "uuid", "p_reference" "uuid") TO "service_role";



REVOKE ALL ON FUNCTION "public"."corpora_decide_project_discovery"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_project" "uuid", "p_discovery" "uuid", "p_expected" bigint, "p_reason" "text", "p_action" "text") FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."corpora_decide_project_discovery"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_project" "uuid", "p_discovery" "uuid", "p_expected" bigint, "p_reason" "text", "p_action" "text") TO "service_role";



REVOKE ALL ON FUNCTION "public"."corpora_decide_project_link"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_project" "uuid", "p_reference" "uuid", "p_expected" bigint, "p_reason" "text", "p_action" "text") FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."corpora_decide_project_link"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_project" "uuid", "p_reference" "uuid", "p_expected" bigint, "p_reason" "text", "p_action" "text") TO "service_role";



REVOKE ALL ON FUNCTION "public"."corpora_directory_reader"() FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."corpora_directory_reader"() TO "authenticated";
GRANT ALL ON FUNCTION "public"."corpora_directory_reader"() TO "service_role";



REVOKE ALL ON FUNCTION "public"."corpora_export_project_links"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_project" "uuid", "p_references" "uuid"[]) FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."corpora_export_project_links"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_project" "uuid", "p_references" "uuid"[]) TO "service_role";



REVOKE ALL ON FUNCTION "public"."corpora_finalize_creation"("p_corpus" "uuid", "p_operation" "uuid", "p_claim" "uuid", "p_repo" "text", "p_type" "text", "p_sha" "text") FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."corpora_finalize_creation"("p_corpus" "uuid", "p_operation" "uuid", "p_claim" "uuid", "p_repo" "text", "p_type" "text", "p_sha" "text") TO "service_role";



REVOKE ALL ON FUNCTION "public"."corpora_find_creation_result"("p_corpus" "uuid", "p_operation" "uuid", "p_claim" "uuid", "p_repo" "text", "p_type" "text", "p_sha" "text") FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."corpora_find_creation_result"("p_corpus" "uuid", "p_operation" "uuid", "p_claim" "uuid", "p_repo" "text", "p_type" "text", "p_sha" "text") TO "service_role";



REVOKE ALL ON FUNCTION "public"."corpora_find_import_result"("p_operation" "uuid", "p_claim" "uuid", "p_publication" "uuid", "p_sha" "text") FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."corpora_find_import_result"("p_operation" "uuid", "p_claim" "uuid", "p_publication" "uuid", "p_sha" "text") TO "service_role";



REVOKE ALL ON FUNCTION "public"."corpora_find_version_save_result"("p_operation" "uuid", "p_claim" "uuid", "p_publication" "uuid", "p_sha" "text") FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."corpora_find_version_save_result"("p_operation" "uuid", "p_claim" "uuid", "p_publication" "uuid", "p_sha" "text") TO "service_role";



REVOKE ALL ON FUNCTION "public"."corpora_finish_corpus_deletion"("p_operation" "uuid", "p_claim" "uuid", "p_completed" boolean) FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."corpora_finish_corpus_deletion"("p_operation" "uuid", "p_claim" "uuid", "p_completed" boolean) TO "service_role";



REVOKE ALL ON FUNCTION "public"."corpora_finish_save_reconciliation"("p_operation" "uuid", "p_claim" "uuid", "p_publication" "uuid", "p_sha" "text") FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."corpora_finish_save_reconciliation"("p_operation" "uuid", "p_claim" "uuid", "p_publication" "uuid", "p_sha" "text") TO "service_role";



REVOKE ALL ON FUNCTION "public"."corpora_get_corpus_deletion"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_operation" "uuid") FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."corpora_get_corpus_deletion"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_operation" "uuid") TO "service_role";



REVOKE ALL ON FUNCTION "public"."corpora_get_creation"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid") FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."corpora_get_creation"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid") TO "service_role";



REVOKE ALL ON FUNCTION "public"."corpora_get_edit_intent"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_operation" "uuid", "p_intent" "text") FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."corpora_get_edit_intent"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_operation" "uuid", "p_intent" "text") TO "service_role";



REVOKE ALL ON FUNCTION "public"."corpora_get_import"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_operation" "uuid") FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."corpora_get_import"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_operation" "uuid") TO "service_role";



REVOKE ALL ON FUNCTION "public"."corpora_get_import_reconciliation"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_operation" "uuid") FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."corpora_get_import_reconciliation"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_operation" "uuid") TO "service_role";



REVOKE ALL ON FUNCTION "public"."corpora_get_metadata"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid") FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."corpora_get_metadata"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid") TO "service_role";



REVOKE ALL ON FUNCTION "public"."corpora_get_restore_source"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_sequence" bigint) FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."corpora_get_restore_source"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_sequence" bigint) TO "service_role";



REVOKE ALL ON FUNCTION "public"."corpora_get_save_reconciliation"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_operation" "uuid") FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."corpora_get_save_reconciliation"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_operation" "uuid") TO "service_role";



REVOKE ALL ON FUNCTION "public"."corpora_get_version_save"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_operation" "uuid") FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."corpora_get_version_save"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_operation" "uuid") TO "service_role";



REVOKE ALL ON FUNCTION "public"."corpora_ingest_session_active"() FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."corpora_ingest_session_active"() TO "authenticated";
GRANT ALL ON FUNCTION "public"."corpora_ingest_session_active"() TO "service_role";



REVOKE ALL ON FUNCTION "public"."corpora_legacy_document_editable"("p_document" "uuid") FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."corpora_legacy_document_editable"("p_document" "uuid") TO "service_role";
GRANT ALL ON FUNCTION "public"."corpora_legacy_document_editable"("p_document" "uuid") TO "authenticated";



REVOKE ALL ON FUNCTION "public"."corpora_legacy_storage_session_readable"() FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."corpora_legacy_storage_session_readable"() TO "service_role";
GRANT ALL ON FUNCTION "public"."corpora_legacy_storage_session_readable"() TO "authenticated";



REVOKE ALL ON FUNCTION "public"."corpora_legacy_superadmin_readable"() FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."corpora_legacy_superadmin_readable"() TO "service_role";
GRANT ALL ON FUNCTION "public"."corpora_legacy_superadmin_readable"() TO "authenticated";



REVOKE ALL ON FUNCTION "public"."corpora_list_corpus_readers"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_after" "uuid", "p_limit" integer) FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."corpora_list_corpus_readers"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_after" "uuid", "p_limit" integer) TO "service_role";



REVOKE ALL ON FUNCTION "public"."corpora_list_corpus_versions"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_before" bigint, "p_limit" integer) FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."corpora_list_corpus_versions"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_before" bigint, "p_limit" integer) TO "service_role";



REVOKE ALL ON FUNCTION "public"."corpora_list_metadata"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_owner" "uuid", "p_after" "uuid", "p_limit" integer) FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."corpora_list_metadata"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_owner" "uuid", "p_after" "uuid", "p_limit" integer) TO "service_role";



REVOKE ALL ON FUNCTION "public"."corpora_list_project_corpora"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_project" "uuid", "p_linked_only" boolean, "p_after" "uuid", "p_limit" integer) FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."corpora_list_project_corpora"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_project" "uuid", "p_linked_only" boolean, "p_after" "uuid", "p_limit" integer) TO "service_role";



REVOKE ALL ON FUNCTION "public"."corpora_list_project_members"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_project" "uuid", "p_after" "uuid", "p_limit" integer) FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."corpora_list_project_members"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_project" "uuid", "p_after" "uuid", "p_limit" integer) TO "service_role";



REVOKE ALL ON FUNCTION "public"."corpora_list_shared_corpora"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_after" "uuid", "p_limit" integer) FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."corpora_list_shared_corpora"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_after" "uuid", "p_limit" integer) TO "service_role";



REVOKE ALL ON FUNCTION "public"."corpora_mark_creation_uncertain"("p_operation" "uuid", "p_claim" "uuid") FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."corpora_mark_creation_uncertain"("p_operation" "uuid", "p_claim" "uuid") TO "service_role";



REVOKE ALL ON FUNCTION "public"."corpora_mark_import_uncertain"("p_operation" "uuid", "p_claim" "uuid") FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."corpora_mark_import_uncertain"("p_operation" "uuid", "p_claim" "uuid") TO "service_role";



REVOKE ALL ON FUNCTION "public"."corpora_mark_version_save_uncertain"("p_operation" "uuid", "p_claim" "uuid") FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."corpora_mark_version_save_uncertain"("p_operation" "uuid", "p_claim" "uuid") TO "service_role";



REVOKE ALL ON FUNCTION "public"."corpora_observe_provision"("p_operation" "uuid") FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."corpora_observe_provision"("p_operation" "uuid") TO "service_role";



REVOKE ALL ON FUNCTION "public"."corpora_patch_metadata"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_revision" bigint, "p_fields" "jsonb") FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."corpora_patch_metadata"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_revision" bigint, "p_fields" "jsonb") TO "service_role";



REVOKE ALL ON FUNCTION "public"."corpora_project_actor"() FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."corpora_project_actor"() TO "authenticated";
GRANT ALL ON FUNCTION "public"."corpora_project_actor"() TO "service_role";



REVOKE ALL ON FUNCTION "public"."corpora_project_association_editable"("p_project" "uuid") FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."corpora_project_association_editable"("p_project" "uuid") TO "service_role";
GRANT ALL ON FUNCTION "public"."corpora_project_association_editable"("p_project" "uuid") TO "authenticated";



REVOKE ALL ON FUNCTION "public"."corpora_project_association_readable"("p_project" "uuid") FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."corpora_project_association_readable"("p_project" "uuid") TO "service_role";
GRANT ALL ON FUNCTION "public"."corpora_project_association_readable"("p_project" "uuid") TO "authenticated";



REVOKE ALL ON FUNCTION "public"."corpora_project_discovery_history"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_project" "uuid", "p_discovery" "uuid", "p_before" bigint, "p_limit" integer) FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."corpora_project_discovery_history"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_project" "uuid", "p_discovery" "uuid", "p_before" bigint, "p_limit" integer) TO "service_role";



REVOKE ALL ON FUNCTION "public"."corpora_project_link_history"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_project" "uuid", "p_reference" "uuid", "p_before" bigint, "p_limit" integer) FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."corpora_project_link_history"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_project" "uuid", "p_reference" "uuid", "p_before" bigint, "p_limit" integer) TO "service_role";



REVOKE ALL ON FUNCTION "public"."corpora_project_superadmin"() FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."corpora_project_superadmin"() TO "authenticated";
GRANT ALL ON FUNCTION "public"."corpora_project_superadmin"() TO "service_role";



REVOKE ALL ON FUNCTION "public"."corpora_read_corpus_access"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid") FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."corpora_read_corpus_access"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid") TO "service_role";



REVOKE ALL ON FUNCTION "public"."corpora_read_current_project_link"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_project" "uuid", "p_reference" "uuid") FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."corpora_read_current_project_link"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_project" "uuid", "p_reference" "uuid") TO "service_role";



REVOKE ALL ON FUNCTION "public"."corpora_register_project_discovery"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_project" "uuid", "p_discovery" "uuid", "p_reason" "text", "p_evidence" "jsonb") FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."corpora_register_project_discovery"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_project" "uuid", "p_discovery" "uuid", "p_reason" "text", "p_evidence" "jsonb") TO "service_role";



REVOKE ALL ON FUNCTION "public"."corpora_save_project_link"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_project" "uuid", "p_reference" "uuid", "p_expected" bigint, "p_reason" "text", "p_value" "jsonb") FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."corpora_save_project_link"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_project" "uuid", "p_reference" "uuid", "p_expected" bigint, "p_reason" "text", "p_value" "jsonb") TO "service_role";



REVOKE ALL ON FUNCTION "public"."corpora_set_corpus_reader"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_reader" "uuid", "p_present" boolean) FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."corpora_set_corpus_reader"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_reader" "uuid", "p_present" boolean) TO "service_role";



REVOKE ALL ON FUNCTION "public"."corpora_set_corpus_visibility"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_revision" bigint, "p_visibility" "text") FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."corpora_set_corpus_visibility"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_revision" bigint, "p_visibility" "text") TO "service_role";



REVOKE ALL ON FUNCTION "public"."corpora_set_project_corpus"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_project" "uuid", "p_corpus" "uuid", "p_present" boolean) FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."corpora_set_project_corpus"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_project" "uuid", "p_corpus" "uuid", "p_present" boolean) TO "service_role";



REVOKE ALL ON FUNCTION "public"."corpora_set_project_member"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_project" "uuid", "p_member" "uuid", "p_present" boolean) FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."corpora_set_project_member"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_project" "uuid", "p_member" "uuid", "p_present" boolean) TO "service_role";



REVOKE ALL ON FUNCTION "public"."corpora_stage_import"("p_operation" "uuid", "p_claim" "uuid", "p_stage_sha" "text", "p_publication" "uuid") FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."corpora_stage_import"("p_operation" "uuid", "p_claim" "uuid", "p_stage_sha" "text", "p_publication" "uuid") TO "service_role";



REVOKE ALL ON FUNCTION "public"."corpora_stage_version_save"("p_operation" "uuid", "p_claim" "uuid", "p_stage_sha" "text", "p_publication" "uuid") FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."corpora_stage_version_save"("p_operation" "uuid", "p_claim" "uuid", "p_stage_sha" "text", "p_publication" "uuid") TO "service_role";



REVOKE ALL ON FUNCTION "public"."corpora_uncertain_discard_status"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_operation" "uuid", "p_revision" bigint) FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."corpora_uncertain_discard_status"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_operation" "uuid", "p_revision" bigint) TO "service_role";



REVOKE ALL ON FUNCTION "public"."corpora_uncertain_discard_target"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_revision" bigint) FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."corpora_uncertain_discard_target"("p_actor" "uuid", "p_session" "uuid", "p_token_exp" bigint, "p_corpus" "uuid", "p_revision" bigint) TO "service_role";



REVOKE ALL ON FUNCTION "public"."corpora_version_save_base"("p_operation" "uuid", "p_claim" "uuid") FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."corpora_version_save_base"("p_operation" "uuid", "p_claim" "uuid") TO "service_role";



REVOKE ALL ON FUNCTION "public"."handle_new_user"() FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."handle_new_user"() TO "service_role";



GRANT ALL ON FUNCTION "public"."rls_auto_enable"() TO "anon";
GRANT ALL ON FUNCTION "public"."rls_auto_enable"() TO "authenticated";
GRANT ALL ON FUNCTION "public"."rls_auto_enable"() TO "service_role";



REVOKE ALL ON FUNCTION "public"."set_updated_at"() FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."set_updated_at"() TO "service_role";



GRANT ALL ON FUNCTION "public"."touch_updated_at"() TO "anon";
GRANT ALL ON FUNCTION "public"."touch_updated_at"() TO "authenticated";
GRANT ALL ON FUNCTION "public"."touch_updated_at"() TO "service_role";



GRANT ALL ON TABLE "public"."addresses" TO "anon";
GRANT ALL ON TABLE "public"."addresses" TO "authenticated";
GRANT ALL ON TABLE "public"."addresses" TO "service_role";



GRANT ALL ON TABLE "public"."authors" TO "anon";
GRANT ALL ON TABLE "public"."authors" TO "authenticated";
GRANT ALL ON TABLE "public"."authors" TO "service_role";



GRANT ALL ON TABLE "public"."books" TO "anon";
GRANT ALL ON TABLE "public"."books" TO "authenticated";
GRANT ALL ON TABLE "public"."books" TO "service_role";



GRANT ALL ON TABLE "public"."conversion_jobs" TO "service_role";
GRANT SELECT,INSERT ON TABLE "public"."conversion_jobs" TO "authenticated";



GRANT ALL ON TABLE "public"."corpora" TO "service_role";
GRANT SELECT ON TABLE "public"."corpora" TO "anon";
GRANT SELECT ON TABLE "public"."corpora" TO "authenticated";



GRANT SELECT ON TABLE "public"."corpora_corpus_access" TO "service_role";



GRANT SELECT ON TABLE "public"."corpora_corpus_bootstrap" TO "service_role";



GRANT SELECT ON TABLE "public"."corpora_corpus_deletions" TO "service_role";



GRANT SELECT ON TABLE "public"."corpora_corpus_heads" TO "service_role";



GRANT SELECT ON TABLE "public"."corpora_corpus_imports" TO "service_role";



GRANT SELECT ON TABLE "public"."corpora_corpus_metadata" TO "service_role";



GRANT SELECT ON TABLE "public"."corpora_corpus_provision_ops" TO "service_role";



GRANT SELECT ON TABLE "public"."corpora_corpus_publication_claims" TO "service_role";



GRANT SELECT ON TABLE "public"."corpora_corpus_readers" TO "service_role";



GRANT SELECT ON TABLE "public"."corpora_corpus_staged_inputs" TO "service_role";



GRANT SELECT ON TABLE "public"."corpora_corpus_uncertain_discards" TO "service_role";



GRANT SELECT ON TABLE "public"."corpora_corpus_version_intents" TO "service_role";



GRANT SELECT ON TABLE "public"."corpora_corpus_version_saves" TO "service_role";



GRANT SELECT ON TABLE "public"."corpora_corpus_versions" TO "service_role";



GRANT SELECT ON TABLE "public"."corpora_corpus_write_ops" TO "service_role";



GRANT SELECT,INSERT ON TABLE "public"."corpora_ingest_requests" TO "service_role";
GRANT SELECT,INSERT ON TABLE "public"."corpora_ingest_requests" TO "authenticated";



GRANT ALL ON TABLE "public"."corpora_log" TO "anon";
GRANT ALL ON TABLE "public"."corpora_log" TO "authenticated";
GRANT ALL ON TABLE "public"."corpora_log" TO "service_role";



GRANT SELECT ON TABLE "public"."corpora_project_discoveries" TO "service_role";



GRANT SELECT ON TABLE "public"."corpora_project_discovery_revisions" TO "service_role";



GRANT SELECT ON TABLE "public"."corpora_project_link_heads" TO "service_role";



GRANT SELECT ON TABLE "public"."corpora_project_link_revisions" TO "service_role";



GRANT SELECT ON TABLE "public"."corpora_project_managed_corpora" TO "service_role";



GRANT SELECT ON TABLE "public"."corpora_project_members" TO "service_role";



GRANT ALL ON TABLE "public"."corpus_changes" TO "service_role";
GRANT SELECT ON TABLE "public"."corpus_changes" TO "authenticated";



GRANT ALL ON TABLE "public"."corpus_commits" TO "service_role";
GRANT SELECT ON TABLE "public"."corpus_commits" TO "anon";
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE "public"."corpus_commits" TO "authenticated";



GRANT ALL ON TABLE "public"."corpus_documents" TO "service_role";
GRANT SELECT ON TABLE "public"."corpus_documents" TO "anon";
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE "public"."corpus_documents" TO "authenticated";



GRANT ALL ON TABLE "public"."devices" TO "anon";
GRANT ALL ON TABLE "public"."devices" TO "authenticated";
GRANT ALL ON TABLE "public"."devices" TO "service_role";



GRANT ALL ON TABLE "public"."licences" TO "service_role";
GRANT SELECT ON TABLE "public"."licences" TO "anon";
GRANT SELECT,INSERT,UPDATE ON TABLE "public"."licences" TO "authenticated";



GRANT ALL ON TABLE "public"."logs" TO "anon";
GRANT ALL ON TABLE "public"."logs" TO "authenticated";
GRANT ALL ON TABLE "public"."logs" TO "service_role";



GRANT ALL ON TABLE "public"."organizations" TO "anon";
GRANT ALL ON TABLE "public"."organizations" TO "authenticated";
GRANT ALL ON TABLE "public"."organizations" TO "service_role";



GRANT ALL ON TABLE "public"."project_corpora" TO "service_role";
GRANT SELECT ON TABLE "public"."project_corpora" TO "anon";
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE "public"."project_corpora" TO "authenticated";



GRANT ALL ON TABLE "public"."project_licences" TO "service_role";
GRANT SELECT ON TABLE "public"."project_licences" TO "anon";
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE "public"."project_licences" TO "authenticated";



GRANT ALL ON TABLE "public"."project_references" TO "service_role";



GRANT ALL ON TABLE "public"."projects" TO "service_role";
GRANT SELECT ON TABLE "public"."projects" TO "anon";
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE "public"."projects" TO "authenticated";



GRANT ALL ON TABLE "public"."repositories" TO "anon";
GRANT ALL ON TABLE "public"."repositories" TO "authenticated";
GRANT ALL ON TABLE "public"."repositories" TO "service_role";



GRANT ALL ON TABLE "public"."user_directory" TO "service_role";
GRANT SELECT ON TABLE "public"."user_directory" TO "authenticated";



GRANT ALL ON TABLE "public"."users" TO "authenticated";
GRANT ALL ON TABLE "public"."users" TO "service_role";



ALTER DEFAULT PRIVILEGES FOR ROLE "postgres" IN SCHEMA "public" GRANT ALL ON SEQUENCES TO "postgres";
ALTER DEFAULT PRIVILEGES FOR ROLE "postgres" IN SCHEMA "public" GRANT ALL ON SEQUENCES TO "anon";
ALTER DEFAULT PRIVILEGES FOR ROLE "postgres" IN SCHEMA "public" GRANT ALL ON SEQUENCES TO "authenticated";
ALTER DEFAULT PRIVILEGES FOR ROLE "postgres" IN SCHEMA "public" GRANT ALL ON SEQUENCES TO "service_role";






ALTER DEFAULT PRIVILEGES FOR ROLE "postgres" IN SCHEMA "public" GRANT ALL ON FUNCTIONS TO "postgres";
ALTER DEFAULT PRIVILEGES FOR ROLE "postgres" IN SCHEMA "public" GRANT ALL ON FUNCTIONS TO "anon";
ALTER DEFAULT PRIVILEGES FOR ROLE "postgres" IN SCHEMA "public" GRANT ALL ON FUNCTIONS TO "authenticated";
ALTER DEFAULT PRIVILEGES FOR ROLE "postgres" IN SCHEMA "public" GRANT ALL ON FUNCTIONS TO "service_role";






ALTER DEFAULT PRIVILEGES FOR ROLE "postgres" IN SCHEMA "public" GRANT ALL ON TABLES TO "postgres";
ALTER DEFAULT PRIVILEGES FOR ROLE "postgres" IN SCHEMA "public" GRANT ALL ON TABLES TO "anon";
ALTER DEFAULT PRIVILEGES FOR ROLE "postgres" IN SCHEMA "public" GRANT ALL ON TABLES TO "authenticated";
ALTER DEFAULT PRIVILEGES FOR ROLE "postgres" IN SCHEMA "public" GRANT ALL ON TABLES TO "service_role";








SET LOCAL search_path = public, pg_catalog;
CREATE TRIGGER on_auth_user_created AFTER INSERT ON auth.users FOR EACH ROW EXECUTE FUNCTION public.handle_new_user();
CREATE POLICY "corpora_private_objects_anon_guard" ON storage.objects AS RESTRICTIVE FOR ALL TO "anon" USING ((bucket_id <> 'project-corpora'::text)) WITH CHECK ((bucket_id <> 'project-corpora'::text));
CREATE POLICY "corpora_private_objects_delete" ON storage.objects AS PERMISSIVE FOR DELETE TO "authenticated" USING (((bucket_id = 'project-corpora'::text) AND ((owner_id = (corpora_project_actor())::text) OR corpora_project_superadmin())));
CREATE POLICY "corpora_private_objects_insert" ON storage.objects AS PERMISSIVE FOR INSERT TO "authenticated" WITH CHECK (((bucket_id = 'project-corpora'::text) AND ((owner_id = (corpora_project_actor())::text) OR corpora_project_superadmin())));
CREATE POLICY "corpora_private_objects_operation_guard" ON storage.objects AS RESTRICTIVE FOR SELECT TO "authenticated" USING (((bucket_id <> 'project-corpora'::text) OR storage.allow_any_operation(ARRAY['object.upload'::text, 'object.upload_update'::text, 'object.delete'::text, 'object.delete_many'::text, 'object.list'::text, 'object.list_v2'::text])));
CREATE POLICY "corpora_private_objects_read" ON storage.objects AS PERMISSIVE FOR SELECT TO "authenticated" USING (((bucket_id = 'project-corpora'::text) AND corpora_legacy_storage_session_readable() AND ((owner_id = (auth.uid())::text) OR corpora_legacy_superadmin_readable())));
CREATE POLICY "corpora_private_objects_update" ON storage.objects AS PERMISSIVE FOR UPDATE TO "authenticated" USING (((bucket_id = 'project-corpora'::text) AND ((owner_id = (corpora_project_actor())::text) OR corpora_project_superadmin()))) WITH CHECK (((bucket_id = 'project-corpora'::text) AND ((owner_id = (corpora_project_actor())::text) OR corpora_project_superadmin())));
CREATE POLICY "corpora_private_objects_user_delete_guard" ON storage.objects AS RESTRICTIVE FOR DELETE TO "authenticated" USING (((bucket_id <> 'project-corpora'::text) OR ((owner_id = (corpora_project_actor())::text) OR corpora_project_superadmin())));
CREATE POLICY "corpora_private_objects_user_insert_guard" ON storage.objects AS RESTRICTIVE FOR INSERT TO "authenticated" WITH CHECK (((bucket_id <> 'project-corpora'::text) OR ((owner_id = (corpora_project_actor())::text) OR corpora_project_superadmin())));
CREATE POLICY "corpora_private_objects_user_read_guard" ON storage.objects AS RESTRICTIVE FOR SELECT TO "authenticated" USING (((bucket_id <> 'project-corpora'::text) OR (corpora_legacy_storage_session_readable() AND ((owner_id = (auth.uid())::text) OR corpora_legacy_superadmin_readable()))));
CREATE POLICY "corpora_private_objects_user_update_guard" ON storage.objects AS RESTRICTIVE FOR UPDATE TO "authenticated" USING (((bucket_id <> 'project-corpora'::text) OR ((owner_id = (corpora_project_actor())::text) OR corpora_project_superadmin()))) WITH CHECK (((bucket_id <> 'project-corpora'::text) OR ((owner_id = (corpora_project_actor())::text) OR corpora_project_superadmin())));
CREATE POLICY "ingest_bound_objects_delete" ON storage.objects AS RESTRICTIVE FOR DELETE TO PUBLIC USING ((bucket_id <> 'conversion-jobs'::text));
CREATE POLICY "ingest_bound_objects_insert" ON storage.objects AS RESTRICTIVE FOR INSERT TO PUBLIC WITH CHECK (((bucket_id <> 'conversion-jobs'::text) OR ((owner_id = (( SELECT auth.uid() AS uid))::text) AND (name ~ (('^conversion-jobs/'::text || (( SELECT auth.uid() AS uid))::text) || '/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.graph\.json$'::text)))));
CREATE POLICY "ingest_bound_objects_select" ON storage.objects AS RESTRICTIVE FOR SELECT TO PUBLIC USING (((bucket_id <> 'conversion-jobs'::text) OR ((owner_id = (( SELECT auth.uid() AS uid))::text) AND (name ~ (('^conversion-jobs/'::text || (( SELECT auth.uid() AS uid))::text) || '/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.graph\.json$'::text)))));
CREATE POLICY "ingest_bound_objects_update" ON storage.objects AS RESTRICTIVE FOR UPDATE TO PUBLIC USING ((bucket_id <> 'conversion-jobs'::text)) WITH CHECK ((bucket_id <> 'conversion-jobs'::text));
CREATE POLICY "ingest_own_objects_insert" ON storage.objects AS PERMISSIVE FOR INSERT TO "authenticated" WITH CHECK (((bucket_id = 'conversion-jobs'::text) AND (owner_id = (( SELECT auth.uid() AS uid))::text) AND (name ~ (('^conversion-jobs/'::text || (( SELECT auth.uid() AS uid))::text) || '/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.graph\.json$'::text))));
CREATE POLICY "ingest_own_objects_select" ON storage.objects AS PERMISSIVE FOR SELECT TO "authenticated" USING (((bucket_id = 'conversion-jobs'::text) AND (owner_id = (( SELECT auth.uid() AS uid))::text) AND (name ~ (('^conversion-jobs/'::text || (( SELECT auth.uid() AS uid))::text) || '/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.graph\.json$'::text))));
CREATE POLICY "ingest_session_objects_insert" ON storage.objects AS RESTRICTIVE FOR INSERT TO "authenticated" WITH CHECK (((bucket_id <> 'conversion-jobs'::text) OR ( SELECT corpora_ingest_session_active() AS corpora_ingest_session_active)));
CREATE POLICY "ingest_session_objects_select" ON storage.objects AS RESTRICTIVE FOR SELECT TO "authenticated" USING (((bucket_id <> 'conversion-jobs'::text) OR ( SELECT corpora_ingest_session_active() AS corpora_ingest_session_active)));

-- The hosted auto-RLS event trigger is not installed: event-trigger creation
-- needs elevated privileges unavailable to ordinary hosted project owners.
-- Every later migration must explicitly enable RLS on its own tables.
REVOKE ALL ON FUNCTION public.rls_auto_enable() FROM PUBLIC,anon,authenticated;
NOTIFY pgrst,'reload schema';
COMMIT;
