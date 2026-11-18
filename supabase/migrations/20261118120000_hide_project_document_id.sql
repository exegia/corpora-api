-- Apply only after Web no longer embeds projects.corpus_documents.
-- Public project fields remain readable; the attachment ID uses the
-- session-checked corpora_project_document_id RPC instead.
BEGIN;
DO $$
BEGIN
    IF NOT pg_catalog.has_table_privilege('anon', 'public.projects', 'SELECT')
       OR NOT pg_catalog.has_table_privilege('authenticated', 'public.projects', 'SELECT') THEN
        RAISE EXCEPTION 'PROJECT_SELECT_GRANTS_CHANGED';
    END IF;
END;
$$;
REVOKE SELECT ON public.projects FROM anon, authenticated;
REVOKE SELECT (corpus_document_id) ON public.projects FROM anon, authenticated;
GRANT SELECT (id, name, description, owner_id, created_at, updated_at, status,
              type, language, category, organization_id, user_id)
    ON public.projects TO anon, authenticated;
DO $$
BEGIN
    IF pg_catalog.has_column_privilege('anon', 'public.projects', 'corpus_document_id', 'SELECT')
       OR pg_catalog.has_column_privilege('authenticated', 'public.projects', 'corpus_document_id', 'SELECT') THEN
        RAISE EXCEPTION 'PROJECT_DOCUMENT_ID_STILL_READABLE';
    END IF;
END;
$$;

-- PostgREST needs these column grants to render public project relationships.
-- The restrictive policy guarantees that anonymous directory reads return no rows,
-- even if a future permissive policy is added.
GRANT SELECT (id, name, username) ON public.user_directory TO anon;
CREATE POLICY corpora_directory_anonymous_hidden ON public.user_directory
    AS RESTRICTIVE FOR SELECT TO anon USING (false);

-- Avoid using a project attachment update to probe another user's documents.
-- Existing attachments can be left in place or detached without reassignment.
CREATE FUNCTION public.corpora_check_project_document_attachment()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = '' SET lock_timeout = '3s' AS $$
DECLARE actor uuid;
BEGIN
    IF TG_OP = 'UPDATE' AND NEW.corpus_document_id IS NOT DISTINCT FROM OLD.corpus_document_id THEN
        RETURN NEW;
    END IF;
    IF NEW.corpus_document_id IS NULL OR auth.role() = 'service_role' OR session_user = 'postgres' THEN
        RETURN NEW;
    END IF;
    actor := public.corpora_project_actor();
    IF NOT EXISTS (
        SELECT 1 FROM public.corpus_documents d
        WHERE d.id = NEW.corpus_document_id
          AND (d.owner_id = actor OR public.corpora_project_superadmin())
    ) THEN
        RAISE EXCEPTION 'DOCUMENT_UNAVAILABLE';
    END IF;
    RETURN NEW;
END;
$$;
REVOKE ALL ON FUNCTION public.corpora_check_project_document_attachment() FROM PUBLIC, anon, authenticated;
CREATE TRIGGER corpora_check_project_document_attachment
    BEFORE INSERT OR UPDATE OF corpus_document_id ON public.projects
    FOR EACH ROW EXECUTE FUNCTION public.corpora_check_project_document_attachment();
NOTIFY pgrst, 'reload schema';
COMMIT;
