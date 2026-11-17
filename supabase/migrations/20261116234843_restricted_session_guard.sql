-- Internal session locking for verified API claims. The API role cannot assume
-- the function owner and receives no direct write access to auth.sessions.
-- The API must verify the JWT before providing subject, session and expiry.
CREATE SCHEMA corpora_session_guard;
REVOKE ALL ON SCHEMA corpora_session_guard FROM PUBLIC, anon, authenticated, service_role;
GRANT USAGE ON SCHEMA corpora_session_guard TO service_role;

CREATE FUNCTION corpora_session_guard.lock_verified_session(
    p_owner uuid, p_session uuid, p_token_exp bigint
) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
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

-- The hosted postgres role already has the session privileges and BYPASSRLS;
-- it cannot grant the necessary UPDATE privilege to a separate guard role.
-- The body is fixed SQL and exposes no session updates to the API role.
ALTER FUNCTION corpora_session_guard.lock_verified_session(uuid, uuid, bigint)
    OWNER TO postgres;
REVOKE ALL ON FUNCTION corpora_session_guard.lock_verified_session(uuid, uuid, bigint)
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION corpora_session_guard.lock_verified_session(uuid, uuid, bigint)
    TO service_role;
