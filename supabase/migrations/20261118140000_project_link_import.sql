-- A bulk import is atomic; a preview is never treated as authorization to write.
CREATE FUNCTION corpora_session_guard.project_link_import(
    p_actor uuid,p_session uuid,p_token_exp bigint,p_project uuid,p_reason text,p_items jsonb
) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path='' SET lock_timeout='3s' AS $$
DECLARE plan jsonb; checked jsonb; planned jsonb; candidate jsonb; result jsonb;
    output jsonb := '[]'::jsonb; saved jsonb;
BEGIN
    IF p_reason IS NULL OR length(p_reason) NOT BETWEEN 1 AND 2000 OR btrim(p_reason)='' THEN
        RAISE EXCEPTION 'PROJECT_LINK_IMPORT_INVALID';
    END IF;
    plan := corpora_session_guard.project_link_import_preview(
        p_actor,p_session,p_token_exp,p_project,p_items);
    IF plan->>'status' IS DISTINCT FROM 'ok' THEN RETURN plan; END IF;
    IF EXISTS (SELECT 1 FROM pg_catalog.jsonb_array_elements(plan->'items') x
               WHERE x->>'outcome'='conflict') THEN
        RETURN pg_catalog.jsonb_build_object('status','conflict');
    END IF;
    BEGIN
        FOR planned IN SELECT value FROM pg_catalog.jsonb_array_elements(plan->'items') LOOP
            SELECT value INTO candidate FROM pg_catalog.jsonb_array_elements(p_items)
                WHERE value->>'id'=planned->>'reference_id';
            IF candidate IS NULL OR candidate->'value' IS NULL
               OR candidate->'value'->>'id' IS DISTINCT FROM candidate->>'id'
               OR candidate->'value'->'source' IS DISTINCT FROM candidate->'source'
               OR candidate->'value'->'target' IS DISTINCT FROM candidate->'target'
               OR candidate->'value'->>'relationship' IS DISTINCT FROM candidate->>'relationship'
               OR candidate#>>'{value,provenance,agent_id}' IS DISTINCT FROM p_actor::text
               OR candidate#>>'{value,provenance,origin}' IS DISTINCT FROM 'manual'
               OR candidate#>>'{value,provenance,method}' IS DISTINCT FROM 'project-import' THEN
                RAISE EXCEPTION 'PROJECT_LINK_IMPORT_INVALID';
            END IF;
            IF planned->>'outcome'='unchanged' THEN
                output := output || pg_catalog.jsonb_build_array(pg_catalog.jsonb_build_object(
                    'reference_id',planned->>'reference_id','outcome','unchanged',
                    'version',(planned->>'current_version')::bigint));
            ELSIF planned->>'outcome'='new' THEN
                saved := corpora_session_guard.project_link_save(
                    p_actor,p_session,p_token_exp,p_project,(candidate->>'id')::uuid,
                    NULL,p_reason,candidate->'value');
                IF saved->>'status' IS DISTINCT FROM 'ok'
                   OR (saved->>'version')::bigint IS DISTINCT FROM 1 THEN
                    RAISE EXCEPTION USING ERRCODE='P1001', MESSAGE='PROJECT_LINK_IMPORT_RECHECK';
                END IF;
                output := output || pg_catalog.jsonb_build_array(pg_catalog.jsonb_build_object(
                    'reference_id',planned->>'reference_id','outcome','imported','version',1));
            ELSE
                RAISE EXCEPTION 'PROJECT_LINK_IMPORT_INVALID';
            END IF;
        END LOOP;
    EXCEPTION WHEN SQLSTATE 'P1001' THEN
        -- All saves in this block roll back. Reauthorize a raced-in head before
        -- returning a conflict so private corpus existence is never disclosed.
        checked := corpora_session_guard.project_link_import_preview(
            p_actor,p_session,p_token_exp,p_project,p_items);
        IF checked->>'status' IS DISTINCT FROM 'ok' THEN RETURN checked; END IF;
        RETURN pg_catalog.jsonb_build_object('status','conflict');
    END;
    PERFORM corpora_session_guard.lock_verified_session(p_actor,p_session,p_token_exp);
    RETURN pg_catalog.jsonb_build_object('status','ok','items',output);
END;
$$;
CREATE FUNCTION public.corpora_import_project_links(
    p_actor uuid,p_session uuid,p_token_exp bigint,p_project uuid,p_reason text,p_items jsonb
) RETURNS jsonb LANGUAGE sql SECURITY INVOKER SET search_path='' AS $$
    SELECT corpora_session_guard.project_link_import(p_actor,p_session,p_token_exp,p_project,p_reason,p_items);
$$;
REVOKE ALL ON FUNCTION corpora_session_guard.project_link_import(uuid,uuid,bigint,uuid,text,jsonb)
    FROM PUBLIC,anon,authenticated;
REVOKE ALL ON FUNCTION public.corpora_import_project_links(uuid,uuid,bigint,uuid,text,jsonb)
    FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION corpora_session_guard.project_link_import(uuid,uuid,bigint,uuid,text,jsonb) TO service_role;
GRANT EXECUTE ON FUNCTION public.corpora_import_project_links(uuid,uuid,bigint,uuid,text,jsonb) TO service_role;
NOTIFY pgrst,'reload schema';
