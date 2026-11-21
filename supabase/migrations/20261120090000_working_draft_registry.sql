-- Durable, server-only registry for owner-bound AI working drafts.
--
-- Replaces the unapplied legacy registry (20261115042900) for the four draft
-- lifecycle operations only: capabilities, provision, drafts and resolve. It
-- adds new objects and one restrictive Storage policy for the new private
-- bucket. It does not alter existing tables, grants or policies, and it does
-- not write to Supabase-managed Storage tables: create the private
-- `corpus-ai-drafts` bucket through the Storage API (see supabase/bootstrap).
-- CREATE (not CREATE OR REPLACE) fails closed if any legacy registry exists.
BEGIN;
SET LOCAL lock_timeout = '3s';

DO $$
BEGIN
    IF to_regclass('public.corpus_ai_drafts') IS NOT NULL
       OR to_regprocedure('public.corpora_ai_storage(text,uuid,jsonb)') IS NOT NULL THEN
        RAISE EXCEPTION 'WORKING_DRAFT_REGISTRY_ALREADY_PRESENT';
    END IF;
    IF EXISTS (SELECT 1 FROM storage.buckets WHERE id = 'corpus-ai-drafts' AND public IS NOT FALSE) THEN
        RAISE EXCEPTION 'WORKING_DRAFT_BUCKET_MUST_BE_PRIVATE';
    END IF;
END;
$$;

-- One immutable archive pointer per owner and source conversion job. The
-- document ID is derived by the API from (owner, job) so retries are stable.
-- source_job_id and source_digest record provenance; the conversion job row is
-- not referenced by a foreign key so existing job retention stays unchanged.
CREATE TABLE public.corpus_ai_drafts (
    document_id uuid PRIMARY KEY,
    owner uuid NOT NULL REFERENCES auth.users(id) ON DELETE RESTRICT,
    corpus text NOT NULL,
    bucket text NOT NULL CHECK (bucket = 'corpus-ai-drafts'),
    object_key text NOT NULL,
    revision uuid NOT NULL,
    digest text NOT NULL CHECK (digest ~ '^sha256:[0-9a-f]{64}$'),
    version text NOT NULL CHECK (version ~ '^v?[0-9]+\.[0-9]+$'),
    state text NOT NULL DEFAULT 'draft' CHECK (state IN ('draft', 'locked', 'published')),
    name text NOT NULL CHECK (length(name) BETWEEN 1 AND 512),
    source_job_id text NOT NULL
        CHECK (source_job_id ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'),
    source_digest text NOT NULL CHECK (source_digest ~ '^sha256:[0-9a-f]{64}$'),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (owner, corpus),
    UNIQUE (owner, source_job_id),
    CHECK (corpus = 'draft:' || document_id::text),
    CHECK (object_key = owner::text || '/ai/' || document_id::text || '/' || revision::text || '.corpus')
);
CREATE INDEX corpus_ai_drafts_owner_created
    ON public.corpus_ai_drafts(owner, created_at, document_id);
ALTER TABLE public.corpus_ai_drafts ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.corpus_ai_drafts FROM PUBLIC, anon, authenticated, service_role;
GRANT SELECT, INSERT ON public.corpus_ai_drafts TO service_role;
COMMENT ON TABLE public.corpus_ai_drafts IS
    'Server-only owner-bound working draft pointers. Browser roles have no access.';

-- Browser roles never read or write draft archives directly; only the API's
-- service role (which bypasses RLS) stages and downloads them.
CREATE POLICY corpora_ai_draft_objects_private ON storage.objects
AS RESTRICTIVE FOR ALL TO PUBLIC
USING (bucket_id <> 'corpus-ai-drafts')
WITH CHECK (bucket_id <> 'corpus-ai-drafts');

-- p_owner is the verified caller supplied by the trusted API after token and
-- session checks. Every lookup is scoped to it, so a foreign draft and an
-- unknown draft both resolve to NULL.
CREATE FUNCTION public.corpora_ai_storage(p_action text, p_owner uuid, p_payload jsonb)
RETURNS jsonb LANGUAGE plpgsql SECURITY INVOKER SET search_path = '' SET lock_timeout = '3s' AS $$
DECLARE
    d public.corpus_ai_drafts%ROWTYPE;
    v_id uuid;
    v_limit integer;
    v_offset integer;
    result jsonb;
    uuid_pattern constant text := '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$';
BEGIN
    IF p_owner IS NULL THEN RAISE EXCEPTION 'AI_NOT_FOUND'; END IF;
    IF p_payload IS NULL OR pg_catalog.jsonb_typeof(p_payload) <> 'object' THEN
        RAISE EXCEPTION 'AI_INVALID';
    END IF;
    IF p_action = 'capabilities' THEN
        -- Fail closed until the private archive bucket has been provisioned.
        IF NOT EXISTS (SELECT 1 FROM storage.buckets
                WHERE id = 'corpus-ai-drafts' AND public IS FALSE) THEN
            RAISE EXCEPTION 'AI_UNAVAILABLE';
        END IF;
        RETURN pg_catalog.jsonb_build_object('draft_api', 1);
    ELSIF p_action = 'provision' THEN
        -- Called only after the API verified source-job ownership, validated
        -- the archive, staged it without upsert and re-read its digest.
        IF COALESCE(p_payload->>'document_id', '') !~ uuid_pattern
           OR COALESCE(p_payload->>'revision', '') !~ uuid_pattern
           OR COALESCE(p_payload->>'job_id', '') !~ uuid_pattern
           OR p_payload->>'name' IS NULL
           OR p_payload->>'digest' IS NULL OR p_payload->>'version' IS NULL THEN
            RAISE EXCEPTION 'AI_INVALID';
        END IF;
        v_id := (p_payload->>'document_id')::uuid;
        IF p_payload->>'corpus' IS DISTINCT FROM 'draft:' || v_id::text
           OR p_payload->>'bucket' IS DISTINCT FROM 'corpus-ai-drafts'
           OR p_payload->>'object_key' IS DISTINCT FROM
               p_owner::text || '/ai/' || v_id::text || '/' || (p_payload->>'revision') || '.corpus' THEN
            RAISE EXCEPTION 'AI_INVALID';
        END IF;
        PERFORM pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(v_id::text, 0));
        SELECT * INTO d FROM public.corpus_ai_drafts WHERE document_id = v_id;
        IF FOUND THEN
            -- An existing binding always wins a retry for the same owner/job.
            IF d.owner <> p_owner OR d.source_job_id <> p_payload->>'job_id' THEN
                RAISE EXCEPTION 'AI_CONFLICT';
            END IF;
        ELSE
            -- Provenance: the source job must still belong to this owner and
            -- have succeeded at the moment the pointer becomes visible.
            IF NOT EXISTS (SELECT 1 FROM public.conversion_jobs j
                    WHERE j.id = p_payload->>'job_id' AND j.owner = p_owner::text
                      AND j.status = 'succeeded') THEN
                RAISE EXCEPTION 'AI_NOT_FOUND';
            END IF;
            INSERT INTO public.corpus_ai_drafts(
                document_id, owner, corpus, bucket, object_key, revision, digest, version,
                name, source_job_id, source_digest)
            VALUES (v_id, p_owner, p_payload->>'corpus', p_payload->>'bucket',
                p_payload->>'object_key', (p_payload->>'revision')::uuid, p_payload->>'digest',
                p_payload->>'version', pg_catalog.left(p_payload->>'name', 512),
                p_payload->>'job_id', p_payload->>'digest')
            RETURNING * INTO d;
        END IF;
    ELSIF p_action = 'resolve' THEN
        SELECT * INTO d FROM public.corpus_ai_drafts
            WHERE owner = p_owner AND corpus = p_payload->>'corpus';
        IF NOT FOUND THEN RETURN NULL; END IF;
    ELSIF p_action = 'drafts' THEN
        IF pg_catalog.jsonb_typeof(p_payload->'limit') IS DISTINCT FROM 'number'
           OR pg_catalog.jsonb_typeof(p_payload->'offset') IS DISTINCT FROM 'number' THEN
            RAISE EXCEPTION 'AI_INVALID';
        END IF;
        v_limit := (p_payload->>'limit')::integer;
        v_offset := (p_payload->>'offset')::integer;
        IF v_limit NOT BETWEEN 1 AND 101 OR v_offset < 0 THEN RAISE EXCEPTION 'AI_INVALID'; END IF;
        SELECT COALESCE(pg_catalog.jsonb_agg(pg_catalog.jsonb_build_object(
                'document_id', q.document_id, 'owner', q.owner, 'corpus', q.corpus,
                'bucket', q.bucket, 'object_key', q.object_key, 'revision', q.revision,
                'digest', q.digest, 'version', q.version, 'state', q.state)
                ORDER BY q.created_at, q.document_id), '[]'::jsonb)
            INTO result
            FROM (SELECT * FROM public.corpus_ai_drafts WHERE owner = p_owner
                ORDER BY created_at, document_id LIMIT v_limit OFFSET v_offset) q;
        RETURN result;
    ELSE
        RAISE EXCEPTION 'AI_INVALID';
    END IF;
    -- The API accepts exactly these fields; provenance columns stay server-side.
    RETURN pg_catalog.jsonb_build_object(
        'document_id', d.document_id, 'owner', d.owner, 'corpus', d.corpus,
        'bucket', d.bucket, 'object_key', d.object_key, 'revision', d.revision,
        'digest', d.digest, 'version', d.version, 'state', d.state);
END;
$$;
REVOKE ALL ON FUNCTION public.corpora_ai_storage(text,uuid,jsonb) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.corpora_ai_storage(text,uuid,jsonb) TO service_role;
COMMENT ON FUNCTION public.corpora_ai_storage(text,uuid,jsonb) IS
    'Server-only working draft registry: capabilities, provision, drafts, resolve.';
NOTIFY pgrst, 'reload schema';
COMMIT;
