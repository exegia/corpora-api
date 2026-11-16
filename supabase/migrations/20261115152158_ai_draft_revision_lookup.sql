-- Owner- and document-bound lookup for revisions produced by the private AI
-- draft journal. This does not resolve arbitrary imported archive history.
CREATE INDEX corpus_ai_operations_applied_revision_lookup
    ON public.corpus_ai_operations(owner, document_id) WHERE applied_at IS NOT NULL;

CREATE FUNCTION public.corpora_ai_revision_lookup(
    p_owner uuid, p_document uuid, p_revision uuid
) RETURNS jsonb LANGUAGE plpgsql SECURITY INVOKER SET search_path = '' AS $$
DECLARE
    d public.corpus_ai_drafts%ROWTYPE;
    o public.corpus_ai_operations%ROWTYPE;
    evidence jsonb;
    found_fact jsonb := NULL;
    fact jsonb;
    operations uuid[] := ARRAY[]::uuid[];
BEGIN
    IF p_owner IS NULL OR p_document IS NULL OR p_revision IS NULL THEN
        RAISE EXCEPTION 'AI_NOT_FOUND';
    END IF;
    SELECT * INTO d FROM public.corpus_ai_drafts
        WHERE owner=p_owner AND document_id=p_document;
    IF NOT FOUND OR d.corpus IS DISTINCT FROM 'draft:' || p_document::text THEN
        RAISE EXCEPTION 'AI_NOT_FOUND';
    END IF;

    IF d.revision = p_revision THEN
        found_fact := jsonb_build_object('revision',d.revision,'digest',d.digest,'version',d.version);
    END IF;
    FOR o IN SELECT * FROM public.corpus_ai_operations
        WHERE owner=p_owner AND document_id=p_document AND applied_at IS NOT NULL
            AND (intent->'plan'->'before'->>'revision'=p_revision::text
                OR intent->'plan'->'after'->>'revision'=p_revision::text)
    LOOP
        -- A pending intent is never evidence. Applied operations require the
        -- matching publication receipt and the expected owner/document scope.
        IF o.receipt IS NULL OR o.receipt->>'proof' IS DISTINCT FROM o.proof
            OR (o.receipt->>'applied_at')::timestamptz IS DISTINCT FROM o.applied_at
            OR o.intent->>'id' IS DISTINCT FROM o.id::text
            OR o.intent->>'owner' IS DISTINCT FROM p_owner::text
            OR o.intent->'suggestion'->'scope'->>'corpus' IS DISTINCT FROM d.corpus
            THEN RAISE EXCEPTION 'AI_CONFLICT'; END IF;
        operations := array_append(operations,o.id);
        -- A revision can be the result of one applied operation and the
        -- input of one later operation. More claims indicate corrupted state.
        IF cardinality(operations) > 2 THEN RAISE EXCEPTION 'AI_CONFLICT'; END IF;
        FOR evidence IN SELECT value FROM jsonb_array_elements(
            jsonb_build_array(o.intent->'plan'->'before',o.intent->'plan'->'after'))
        LOOP
            IF evidence->>'revision' = p_revision::text THEN
                IF coalesce(evidence->>'digest','') !~ '^sha256:[0-9a-f]{64}$'
                    OR coalesce(evidence->>'version','') = ''
                    THEN RAISE EXCEPTION 'AI_CONFLICT'; END IF;
                fact := jsonb_build_object('revision',p_revision,'digest',evidence->>'digest',
                    'version',evidence->>'version');
                IF found_fact IS NOT NULL AND found_fact IS DISTINCT FROM fact THEN
                    RAISE EXCEPTION 'AI_CONFLICT';
                END IF;
                found_fact := fact;
            END IF;
        END LOOP;
    END LOOP;
    IF found_fact IS NULL THEN RAISE EXCEPTION 'AI_NOT_FOUND'; END IF;
    IF coalesce(found_fact->>'digest','') !~ '^sha256:[0-9a-f]{64}$'
        OR coalesce(found_fact->>'version','') = '' THEN RAISE EXCEPTION 'AI_CONFLICT'; END IF;
    RETURN found_fact || jsonb_build_object('operations',to_jsonb(operations));
END;
$$;

REVOKE ALL ON FUNCTION public.corpora_ai_revision_lookup(uuid,uuid,uuid)
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.corpora_ai_revision_lookup(uuid,uuid,uuid) TO service_role;
NOTIFY pgrst, 'reload schema';
