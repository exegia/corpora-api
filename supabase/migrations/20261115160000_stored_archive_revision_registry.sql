-- Draft primitive only: do not apply until all archive readers, writers, imports,
-- and deletes are coordinated with this registry and immutable object storage.
CREATE TABLE public.corpora_stored_revisions (
    owner uuid NOT NULL,
    filename text NOT NULL CHECK (
        filename <> '' AND filename NOT IN ('.', '..')
        AND position('/' IN filename) = 0 AND position(chr(92) IN filename) = 0
        AND position(chr(10) IN filename) = 0 AND position(chr(13) IN filename) = 0
        AND (right(filename, 7) = '.corpus' OR right(filename, 5) = '.cusx')
    ),
    revision uuid NOT NULL,
    digest text NOT NULL CHECK (digest ~ '^sha256:[0-9a-f]{64}$'),
    version text NOT NULL CHECK (length(version) BETWEEN 1 AND 128),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (owner, filename, revision)
);

CREATE TABLE public.corpora_stored_heads (
    owner uuid NOT NULL,
    filename text NOT NULL,
    revision uuid NOT NULL,
    PRIMARY KEY (owner, filename),
    FOREIGN KEY (owner, filename, revision)
        REFERENCES public.corpora_stored_revisions(owner, filename, revision)
);

ALTER TABLE public.corpora_stored_revisions ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.corpora_stored_heads ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.corpora_stored_revisions, public.corpora_stored_heads
    FROM PUBLIC, anon, authenticated;
GRANT SELECT, INSERT ON public.corpora_stored_revisions TO service_role;
GRANT SELECT, INSERT, UPDATE ON public.corpora_stored_heads TO service_role;

-- p_owner is supplied by the trusted server after authentication. These
-- functions do not themselves validate a user's token or object bytes.
CREATE FUNCTION public.corpora_stored_create(
    p_owner uuid, p_filename text, p_revision uuid, p_digest text, p_version text
) RETURNS void LANGUAGE plpgsql SECURITY INVOKER SET search_path = '' AS $$
BEGIN
    INSERT INTO public.corpora_stored_revisions(owner, filename, revision, digest, version)
    VALUES (p_owner, p_filename, p_revision, p_digest, p_version);
    INSERT INTO public.corpora_stored_heads(owner, filename, revision)
    VALUES (p_owner, p_filename, p_revision);
END;
$$;

CREATE FUNCTION public.corpora_stored_publish(
    p_owner uuid, p_filename text, p_expected uuid,
    p_revision uuid, p_digest text, p_version text
) RETURNS void LANGUAGE plpgsql SECURITY INVOKER SET search_path = '' AS $$
DECLARE
    current_revision uuid;
BEGIN
    SELECT revision INTO current_revision FROM public.corpora_stored_heads
        WHERE owner = p_owner AND filename = p_filename FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'ARCHIVE_NOT_FOUND';
    END IF;
    IF p_expected IS NULL OR current_revision <> p_expected THEN
        RAISE EXCEPTION 'ARCHIVE_CONFLICT';
    END IF;
    INSERT INTO public.corpora_stored_revisions(owner, filename, revision, digest, version)
    VALUES (p_owner, p_filename, p_revision, p_digest, p_version);
    UPDATE public.corpora_stored_heads SET revision = p_revision
        WHERE owner = p_owner AND filename = p_filename;
END;
$$;

REVOKE ALL ON FUNCTION public.corpora_stored_create(uuid,text,uuid,text,text)
    FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.corpora_stored_publish(uuid,text,uuid,uuid,text,text)
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.corpora_stored_create(uuid,text,uuid,text,text)
    TO service_role;
GRANT EXECUTE ON FUNCTION public.corpora_stored_publish(uuid,text,uuid,uuid,text,text)
    TO service_role;

NOTIFY pgrst, 'reload schema';
