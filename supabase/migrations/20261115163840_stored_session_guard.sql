-- Draft only: this does not grant access to auth.sessions and is not wired to
-- an HTTP route. The caller must pass claims from a verified access token.
CREATE OR REPLACE FUNCTION public.corpora_stored_session_publish(
    p_owner uuid, p_session uuid, p_token_exp bigint, p_filename text,
    p_expected uuid, p_revision uuid, p_digest text, p_version text
) RETURNS void LANGUAGE plpgsql SECURITY INVOKER SET search_path = '' AS $$
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
    PERFORM public.corpora_stored_publish(
        p_owner, p_filename, p_expected, p_revision, p_digest, p_version
    );
    -- Recheck wall-clock expiry before returning; the session row remains
    -- locked until commit so deletion or expiry changes cannot pass the write.
    IF p_token_exp <= extract(epoch FROM clock_timestamp())
       OR (session_expiry IS NOT NULL AND session_expiry <= clock_timestamp()) THEN
        RAISE EXCEPTION 'AUTH_SESSION_INVALID';
    END IF;
END;
$$;

REVOKE ALL ON FUNCTION public.corpora_stored_session_publish(
    uuid,uuid,bigint,text,uuid,uuid,text,text
) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.corpora_stored_session_publish(
    uuid,uuid,bigint,text,uuid,uuid,text,text
) TO service_role;

CREATE OR REPLACE FUNCTION public.corpora_stored_session_create(
    p_owner uuid, p_session uuid, p_token_exp bigint, p_filename text,
    p_revision uuid, p_digest text, p_version text
) RETURNS void LANGUAGE plpgsql SECURITY INVOKER SET search_path = '' AS $$
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
    PERFORM public.corpora_stored_create(p_owner, p_filename, p_revision, p_digest, p_version);
    IF p_token_exp <= extract(epoch FROM clock_timestamp())
       OR (session_expiry IS NOT NULL AND session_expiry <= clock_timestamp()) THEN
        RAISE EXCEPTION 'AUTH_SESSION_INVALID';
    END IF;
END;
$$;

REVOKE ALL ON FUNCTION public.corpora_stored_session_create(
    uuid,uuid,bigint,text,uuid,text,text
) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.corpora_stored_session_create(
    uuid,uuid,bigint,text,uuid,text,text
) TO service_role;

-- Requires the separate draft legacy import RPC from PR #86. Until that
-- dependency exists, invocation fails closed; no caller is wired here.
CREATE OR REPLACE FUNCTION public.corpora_stored_session_import(
    p_owner uuid, p_session uuid, p_token_exp bigint, p_bucket text, p_filename text,
    p_source_id uuid, p_source_version text, p_revision uuid, p_digest text, p_version text
) RETURNS void LANGUAGE plpgsql SECURITY INVOKER SET search_path = '' AS $$
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
    PERFORM public.corpora_stored_import(
        p_owner, p_bucket, p_filename, p_source_id, p_source_version,
        p_revision, p_digest, p_version
    );
    IF p_token_exp <= extract(epoch FROM clock_timestamp())
       OR (session_expiry IS NOT NULL AND session_expiry <= clock_timestamp()) THEN
        RAISE EXCEPTION 'AUTH_SESSION_INVALID';
    END IF;
END;
$$;

REVOKE ALL ON FUNCTION public.corpora_stored_session_import(
    uuid,uuid,bigint,text,text,uuid,text,uuid,text,text
) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.corpora_stored_session_import(
    uuid,uuid,bigint,text,text,uuid,text,uuid,text,text
) TO service_role;
NOTIFY pgrst, 'reload schema';
