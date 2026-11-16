-- Draft migration: apply only after review against the intended Supabase project.
-- The API, not a browser role, is responsible for checking the verified actor.
CREATE TABLE public.corpora_corpus_metadata (
    id uuid PRIMARY KEY,
    owner_id uuid NOT NULL REFERENCES auth.users(id) ON DELETE RESTRICT,
    title text NOT NULL CHECK (length(btrim(title)) BETWEEN 1 AND 512),
    description text CHECK (description IS NULL OR length(description) <= 10000),
    language text CHECK (language IS NULL OR length(language) <= 128),
    properties jsonb NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(properties) = 'object' AND octet_length(properties::text) <= 16384),
    hf_repo_id text NOT NULL CHECK (
        length(hf_repo_id) <= 193 AND
        hf_repo_id ~ '^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$'
    ),
    hf_repo_type text NOT NULL CHECK (hf_repo_type IN ('dataset', 'model')),
    state text NOT NULL DEFAULT 'provisioning' CHECK (state IN ('provisioning', 'ready', 'failed')),
    revision bigint NOT NULL DEFAULT 1 CHECK (revision > 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- A single Hub identifier cannot be claimed by two metadata records, even
-- when a caller varies the spelling's case or asks for a different repo type.
CREATE UNIQUE INDEX corpora_corpus_metadata_hf_repo_id_unique
    ON public.corpora_corpus_metadata (lower(hf_repo_id));
CREATE INDEX corpora_corpus_metadata_owner_id_id
    ON public.corpora_corpus_metadata (owner_id, id);

CREATE FUNCTION public.corpora_corpus_metadata_stamp()
RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER SET search_path = '' AS $$
BEGIN
    IF NEW.id IS DISTINCT FROM OLD.id OR NEW.owner_id IS DISTINCT FROM OLD.owner_id
       OR NEW.hf_repo_id IS DISTINCT FROM OLD.hf_repo_id
       OR NEW.hf_repo_type IS DISTINCT FROM OLD.hf_repo_type
       OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
        RAISE EXCEPTION 'CORPUS_IDENTITY_IMMUTABLE';
    END IF;
    NEW.revision := OLD.revision + 1;
    NEW.updated_at := clock_timestamp();
    RETURN NEW;
END;
$$;
CREATE TRIGGER corpora_corpus_metadata_stamp BEFORE UPDATE
    ON public.corpora_corpus_metadata FOR EACH ROW
    EXECUTE FUNCTION public.corpora_corpus_metadata_stamp();

ALTER TABLE public.corpora_corpus_metadata ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE public.corpora_corpus_metadata FROM PUBLIC, anon, authenticated, service_role;
GRANT SELECT ON public.corpora_corpus_metadata TO service_role;
GRANT INSERT (id, owner_id, title, hf_repo_id, hf_repo_type)
    ON public.corpora_corpus_metadata TO service_role;
GRANT UPDATE (title, description, language, properties)
    ON public.corpora_corpus_metadata TO service_role;
REVOKE ALL ON FUNCTION public.corpora_corpus_metadata_stamp() FROM PUBLIC, anon, authenticated;
