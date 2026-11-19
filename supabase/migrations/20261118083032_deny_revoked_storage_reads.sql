BEGIN;
SET LOCAL lock_timeout='3s';
LOCK TABLE storage.objects IN ACCESS EXCLUSIVE MODE;

-- Storage maps raised RLS exceptions to 500. A revoked session must be a
-- normal access denial; unexpected database errors still propagate.
CREATE FUNCTION public.corpora_legacy_storage_session_readable() RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path='' AS $$
BEGIN
    RETURN public.corpora_directory_reader();
EXCEPTION WHEN raise_exception THEN
    IF SQLERRM='AUTH_SESSION_INVALID' THEN RETURN false; END IF;
    RAISE;
END;
$$;
REVOKE ALL ON FUNCTION public.corpora_legacy_storage_session_readable() FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION public.corpora_legacy_storage_session_readable() TO authenticated;

CREATE OR REPLACE FUNCTION public.corpora_legacy_superadmin_readable() RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path='' AS $$
BEGIN
    IF NOT public.corpora_legacy_storage_session_readable() THEN RETURN false; END IF;
    RETURN EXISTS(SELECT 1 FROM corpora_session_guard.superadmins WHERE user_id=auth.uid());
END;
$$;

DROP POLICY corpora_private_objects_read ON storage.objects;
CREATE POLICY corpora_private_objects_read ON storage.objects FOR SELECT TO authenticated
    USING (bucket_id='project-corpora' AND public.corpora_legacy_storage_session_readable() AND
        (owner_id=auth.uid()::text OR public.corpora_legacy_superadmin_readable()));
DROP POLICY corpora_private_objects_user_read_guard ON storage.objects;
CREATE POLICY corpora_private_objects_user_read_guard ON storage.objects AS RESTRICTIVE FOR SELECT TO authenticated
    USING (bucket_id<>'project-corpora' OR (public.corpora_legacy_storage_session_readable() AND
        (owner_id=auth.uid()::text OR public.corpora_legacy_superadmin_readable())));
NOTIFY pgrst,'reload schema';
COMMIT;
