-- Expose an attached document only to someone who can read that document.
-- Deploy this lookup and its Web consumer before removing direct column grants.
CREATE FUNCTION public.corpora_project_document_id(p_project uuid)
RETURNS uuid
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
SET lock_timeout = '3s'
AS $$
DECLARE
    actor uuid := public.corpora_project_actor();
    result uuid;
BEGIN
    SELECT d.id INTO result
    FROM public.projects AS p
    JOIN public.corpus_documents AS d ON d.id = p.corpus_document_id
    WHERE p.id = p_project
      AND (d.owner_id = actor OR public.corpora_project_superadmin());
    RETURN result;
END;
$$;
REVOKE ALL ON FUNCTION public.corpora_project_document_id(uuid) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.corpora_project_document_id(uuid) TO authenticated;
NOTIFY pgrst, 'reload schema';
