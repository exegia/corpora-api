-- Membership is a private authority, not a writable project metadata field.
CREATE TABLE public.corpora_project_members (
    project_id uuid NOT NULL REFERENCES public.projects(id) ON DELETE CASCADE,
    user_id uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    added_by uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY(project_id,user_id)
);
ALTER TABLE public.corpora_project_members ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.corpora_project_members FROM PUBLIC,anon,authenticated,service_role;
GRANT SELECT ON public.corpora_project_members TO service_role;

-- The API verifies the JWT before passing session claims. Project row locks
-- serialize owner transfers, member changes and future reference operations.
CREATE FUNCTION corpora_session_guard.project_member_set(
    p_actor uuid,p_session uuid,p_token_exp bigint,p_project uuid,p_member uuid,p_present boolean
) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path='' SET lock_timeout='3s' AS $$
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
-- Email lookup happens only after owner/superadmin authorization; it does not
-- invite users or reveal account existence to unrelated callers.
CREATE FUNCTION corpora_session_guard.project_member_add_email(
    p_actor uuid,p_session uuid,p_token_exp bigint,p_project uuid,p_email text
) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path='' SET lock_timeout='3s' AS $$
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
CREATE FUNCTION corpora_session_guard.project_members_list(
    p_actor uuid,p_session uuid,p_token_exp bigint,p_project uuid,p_after uuid,p_limit integer
) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path='' SET lock_timeout='3s' AS $$
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
CREATE FUNCTION public.corpora_set_project_member(
    p_actor uuid,p_session uuid,p_token_exp bigint,p_project uuid,p_member uuid,p_present boolean
) RETURNS jsonb LANGUAGE sql SECURITY INVOKER SET search_path='' AS $$
    SELECT corpora_session_guard.project_member_set(p_actor,p_session,p_token_exp,p_project,p_member,p_present);
$$;
CREATE FUNCTION public.corpora_list_project_members(
    p_actor uuid,p_session uuid,p_token_exp bigint,p_project uuid,p_after uuid,p_limit integer
) RETURNS jsonb LANGUAGE sql SECURITY INVOKER SET search_path='' AS $$
    SELECT corpora_session_guard.project_members_list(p_actor,p_session,p_token_exp,p_project,p_after,p_limit);
$$;
CREATE FUNCTION public.corpora_add_project_member_by_email(
    p_actor uuid,p_session uuid,p_token_exp bigint,p_project uuid,p_email text
) RETURNS jsonb LANGUAGE sql SECURITY INVOKER SET search_path='' AS $$
    SELECT corpora_session_guard.project_member_add_email(p_actor,p_session,p_token_exp,p_project,p_email);
$$;
REVOKE ALL ON FUNCTION corpora_session_guard.project_member_set(uuid,uuid,bigint,uuid,uuid,boolean)
    FROM PUBLIC,anon,authenticated;
REVOKE ALL ON FUNCTION corpora_session_guard.project_members_list(uuid,uuid,bigint,uuid,uuid,integer)
    FROM PUBLIC,anon,authenticated;
REVOKE ALL ON FUNCTION public.corpora_set_project_member(uuid,uuid,bigint,uuid,uuid,boolean)
    FROM PUBLIC,anon,authenticated;
REVOKE ALL ON FUNCTION public.corpora_list_project_members(uuid,uuid,bigint,uuid,uuid,integer)
    FROM PUBLIC,anon,authenticated;
REVOKE ALL ON FUNCTION corpora_session_guard.project_member_add_email(uuid,uuid,bigint,uuid,text)
    FROM PUBLIC,anon,authenticated;
REVOKE ALL ON FUNCTION public.corpora_add_project_member_by_email(uuid,uuid,bigint,uuid,text)
    FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION corpora_session_guard.project_member_set(uuid,uuid,bigint,uuid,uuid,boolean) TO service_role;
GRANT EXECUTE ON FUNCTION corpora_session_guard.project_members_list(uuid,uuid,bigint,uuid,uuid,integer) TO service_role;
GRANT EXECUTE ON FUNCTION public.corpora_set_project_member(uuid,uuid,bigint,uuid,uuid,boolean) TO service_role;
GRANT EXECUTE ON FUNCTION public.corpora_list_project_members(uuid,uuid,bigint,uuid,uuid,integer) TO service_role;
GRANT EXECUTE ON FUNCTION corpora_session_guard.project_member_add_email(uuid,uuid,bigint,uuid,text) TO service_role;
GRANT EXECUTE ON FUNCTION public.corpora_add_project_member_by_email(uuid,uuid,bigint,uuid,text) TO service_role;
NOTIFY pgrst,'reload schema';
