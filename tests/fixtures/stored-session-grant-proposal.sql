-- Offline proposal for security review. Do not apply automatically or deploy.
-- SELECT FOR SHARE requires UPDATE on at least one column; the UPDATE grant
-- below confers real authority to change session expiry, not just a read lock.
GRANT SELECT (id, user_id, not_after) ON auth.sessions TO service_role;
GRANT UPDATE (not_after) ON auth.sessions TO service_role;
