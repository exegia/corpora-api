-- Managed corpus associations are distinct from the legacy project_corpora table.
CREATE TABLE public.corpora_project_managed_corpora (
    project_id uuid NOT NULL REFERENCES public.projects(id) ON DELETE RESTRICT,
    corpus_id uuid NOT NULL REFERENCES public.corpora_corpus_metadata(id) ON DELETE RESTRICT,
    linked_by uuid NOT NULL,
    linked_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (project_id, corpus_id)
);
ALTER TABLE public.corpora_project_managed_corpora ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.corpora_project_managed_corpora FROM PUBLIC, anon, authenticated, service_role;
GRANT SELECT ON public.corpora_project_managed_corpora TO service_role;

CREATE FUNCTION corpora_session_guard.project_corpus_set(
    p_actor uuid, p_session uuid, p_token_exp bigint, p_project uuid, p_corpus uuid, p_present boolean
) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path='' SET lock_timeout='3s' AS $$
DECLARE access jsonb;
BEGIN
    IF p_corpus IS NULL OR p_present IS NULL THEN RAISE EXCEPTION 'PROJECT_CORPUS_INPUT_INVALID'; END IF;
    IF NOT corpora_session_guard.project_link_access(p_actor,p_session,p_token_exp,p_project,true) THEN
        RETURN jsonb_build_object('status','not_found');
    END IF;
    access := corpora_session_guard.read_access(p_actor,p_session,p_token_exp,p_corpus);
    IF access->>'status' IS DISTINCT FROM 'ok' OR access->'head' IS NULL
       OR access->'head' = 'null'::jsonb OR access#>>'{row,state}' IS DISTINCT FROM 'ready' THEN
        RETURN jsonb_build_object('status','not_found');
    END IF;
    IF p_present THEN
        INSERT INTO public.corpora_project_managed_corpora(project_id,corpus_id,linked_by)
            VALUES(p_project,p_corpus,p_actor) ON CONFLICT DO NOTHING;
    ELSE
        DELETE FROM public.corpora_project_managed_corpora
            WHERE project_id=p_project AND corpus_id=p_corpus;
    END IF;
    PERFORM corpora_session_guard.lock_verified_session(p_actor,p_session,p_token_exp);
    RETURN jsonb_build_object('status','ok','project_id',p_project,'corpus_id',p_corpus,'linked',p_present);
END;
$$;

-- A member sees only linked corpora they can independently access. Only a
-- project manager may enumerate the wider set of available corpus choices.
CREATE FUNCTION corpora_session_guard.project_corpora_list(
    p_actor uuid, p_session uuid, p_token_exp bigint, p_project uuid,
    p_linked_only boolean, p_after uuid, p_limit integer
) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path='' SET lock_timeout='3s' AS $$
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
REVOKE ALL ON FUNCTION corpora_session_guard.project_corpus_set(uuid,uuid,bigint,uuid,uuid,boolean),
    corpora_session_guard.project_corpora_list(uuid,uuid,bigint,uuid,boolean,uuid,integer)
    FROM PUBLIC,anon,authenticated,service_role;

CREATE FUNCTION public.corpora_set_project_corpus(
    p_actor uuid, p_session uuid, p_token_exp bigint, p_project uuid, p_corpus uuid, p_present boolean
) RETURNS jsonb LANGUAGE sql SECURITY INVOKER SET search_path='' AS $$
    SELECT corpora_session_guard.project_corpus_set(p_actor,p_session,p_token_exp,p_project,p_corpus,p_present);
$$;
CREATE FUNCTION public.corpora_list_project_corpora(
    p_actor uuid, p_session uuid, p_token_exp bigint, p_project uuid, p_linked_only boolean, p_after uuid, p_limit integer
) RETURNS jsonb LANGUAGE sql SECURITY INVOKER SET search_path='' AS $$
    SELECT corpora_session_guard.project_corpora_list(p_actor,p_session,p_token_exp,p_project,p_linked_only,p_after,p_limit);
$$;
REVOKE ALL ON FUNCTION public.corpora_set_project_corpus(uuid,uuid,bigint,uuid,uuid,boolean),
    public.corpora_list_project_corpora(uuid,uuid,bigint,uuid,boolean,uuid,integer) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION corpora_session_guard.project_corpus_set(uuid,uuid,bigint,uuid,uuid,boolean),
    corpora_session_guard.project_corpora_list(uuid,uuid,bigint,uuid,boolean,uuid,integer),
    public.corpora_set_project_corpus(uuid,uuid,bigint,uuid,uuid,boolean),
    public.corpora_list_project_corpora(uuid,uuid,bigint,uuid,boolean,uuid,integer) TO service_role;
NOTIFY pgrst,'reload schema';
