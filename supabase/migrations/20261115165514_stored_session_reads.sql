-- Draft only. These wrappers require the separate revision-read RPCs proposed
-- in PRs #84, #87 and #88. Missing dependencies fail closed on invocation.
-- The verified caller must supply the actual JWT subject, session and expiry.
CREATE FUNCTION public.corpora_stored_lock_session(
    p_owner uuid, p_session uuid, p_token_exp bigint
) RETURNS timestamptz LANGUAGE plpgsql SECURITY INVOKER SET search_path = '' AS $$
DECLARE
    session_expiry timestamptz;
BEGIN
    IF p_token_exp IS NULL OR p_token_exp <= extract(epoch FROM clock_timestamp()) THEN
        RAISE EXCEPTION 'AUTH_SESSION_INVALID';
    END IF;
    SELECT not_after INTO session_expiry FROM auth.sessions
        WHERE id = p_session AND user_id = p_owner FOR SHARE;
    IF NOT FOUND OR (session_expiry IS NOT NULL AND session_expiry <= clock_timestamp()) THEN
        RAISE EXCEPTION 'AUTH_SESSION_INVALID';
    END IF;
    RETURN session_expiry;
END;
$$;

CREATE FUNCTION public.corpora_stored_session_get(
    p_owner uuid, p_session uuid, p_token_exp bigint, p_filename text, p_revision uuid
) RETURNS jsonb LANGUAGE plpgsql SECURITY INVOKER SET search_path = '' AS $$
DECLARE
    session_expiry timestamptz;
    result jsonb;
BEGIN
    session_expiry := public.corpora_stored_lock_session(p_owner, p_session, p_token_exp);
    result := public.corpora_stored_get(p_owner, p_filename, p_revision);
    IF p_token_exp <= extract(epoch FROM clock_timestamp())
       OR (session_expiry IS NOT NULL AND session_expiry <= clock_timestamp()) THEN
        RAISE EXCEPTION 'AUTH_SESSION_INVALID';
    END IF;
    RETURN result;
END;
$$;

CREATE FUNCTION public.corpora_stored_session_history(
    p_owner uuid, p_session uuid, p_token_exp bigint, p_filename text, p_limit integer,
    p_before timestamptz, p_before_revision uuid
) RETURNS jsonb LANGUAGE plpgsql SECURITY INVOKER SET search_path = '' AS $$
DECLARE
    session_expiry timestamptz;
    result jsonb;
BEGIN
    session_expiry := public.corpora_stored_lock_session(p_owner, p_session, p_token_exp);
    result := public.corpora_stored_history(p_owner, p_filename, p_limit, p_before, p_before_revision);
    IF p_token_exp <= extract(epoch FROM clock_timestamp())
       OR (session_expiry IS NOT NULL AND session_expiry <= clock_timestamp()) THEN
        RAISE EXCEPTION 'AUTH_SESSION_INVALID';
    END IF;
    RETURN result;
END;
$$;

CREATE FUNCTION public.corpora_stored_session_facts(
    p_owner uuid, p_session uuid, p_token_exp bigint, p_filename text, p_revisions uuid[]
) RETURNS jsonb LANGUAGE plpgsql SECURITY INVOKER SET search_path = '' AS $$
DECLARE
    session_expiry timestamptz;
    result jsonb;
BEGIN
    session_expiry := public.corpora_stored_lock_session(p_owner, p_session, p_token_exp);
    result := public.corpora_stored_revision_facts(p_owner, p_filename, p_revisions);
    IF p_token_exp <= extract(epoch FROM clock_timestamp())
       OR (session_expiry IS NOT NULL AND session_expiry <= clock_timestamp()) THEN
        RAISE EXCEPTION 'AUTH_SESSION_INVALID';
    END IF;
    RETURN result;
END;
$$;

REVOKE ALL ON FUNCTION public.corpora_stored_lock_session(uuid,uuid,bigint)
    FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.corpora_stored_session_get(uuid,uuid,bigint,text,uuid)
    FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.corpora_stored_session_history(uuid,uuid,bigint,text,integer,timestamptz,uuid)
    FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.corpora_stored_session_facts(uuid,uuid,bigint,text,uuid[])
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.corpora_stored_lock_session(uuid,uuid,bigint) TO service_role;
GRANT EXECUTE ON FUNCTION public.corpora_stored_session_get(uuid,uuid,bigint,text,uuid) TO service_role;
GRANT EXECUTE ON FUNCTION public.corpora_stored_session_history(uuid,uuid,bigint,text,integer,timestamptz,uuid) TO service_role;
GRANT EXECUTE ON FUNCTION public.corpora_stored_session_facts(uuid,uuid,bigint,text,uuid[]) TO service_role;
NOTIFY pgrst, 'reload schema';
