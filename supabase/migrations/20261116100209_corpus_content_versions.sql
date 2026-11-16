-- Draft: application code must admit a verified session before inserting a
-- write operation, and must independently verify a retained Hub commit before
-- publishing it. No endpoint currently calls these tables.
CREATE TABLE public.corpora_corpus_write_ops (
    id uuid PRIMARY KEY,
    corpus_id uuid NOT NULL REFERENCES public.corpora_corpus_metadata(id) ON DELETE RESTRICT,
    actor_id uuid NOT NULL REFERENCES auth.users(id) ON DELETE RESTRICT,
    session_id uuid NOT NULL,
    expected_sequence bigint NOT NULL CHECK (expected_sequence >= 0),
    expected_parent_sha text CHECK (expected_parent_sha IS NULL OR expected_parent_sha ~ '^[0-9a-f]{40}$'),
    input_sha256 text NOT NULL CHECK (input_sha256 ~ '^[0-9a-f]{64}$'),
    state text NOT NULL DEFAULT 'admitted' CHECK (state IN ('admitted', 'uncertain', 'completed', 'failed')),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (id, corpus_id),
    CHECK ((expected_sequence = 0) = (expected_parent_sha IS NULL))
);

-- Until reconciliation confirms the prior outcome, prevent a new save from
-- racing an external write whose result was lost.
CREATE UNIQUE INDEX corpora_corpus_write_ops_one_active
    ON public.corpora_corpus_write_ops (corpus_id) WHERE state IN ('admitted', 'uncertain');

CREATE TABLE public.corpora_corpus_versions (
    corpus_id uuid NOT NULL REFERENCES public.corpora_corpus_metadata(id) ON DELETE RESTRICT,
    sequence bigint NOT NULL CHECK (sequence > 0),
    commit_sha text NOT NULL CHECK (commit_sha ~ '^[0-9a-f]{40}$'),
    parent_sha text CHECK (parent_sha IS NULL OR parent_sha ~ '^[0-9a-f]{40}$'),
    operation_id uuid NOT NULL UNIQUE,
    retained_ref text NOT NULL CHECK (retained_ref = 'corpora-op-' || operation_id::text),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (corpus_id, sequence),
    UNIQUE (corpus_id, sequence, commit_sha),
    FOREIGN KEY (operation_id, corpus_id)
        REFERENCES public.corpora_corpus_write_ops(id, corpus_id) ON DELETE RESTRICT,
    CHECK ((sequence = 1) = (parent_sha IS NULL))
);

-- A head can point only to a recorded version for this exact corpus. Admission
-- and head advance must occur in the same database transaction.
CREATE TABLE public.corpora_corpus_heads (
    corpus_id uuid PRIMARY KEY REFERENCES public.corpora_corpus_metadata(id) ON DELETE RESTRICT,
    sequence bigint NOT NULL CHECK (sequence > 0),
    commit_sha text NOT NULL,
    FOREIGN KEY (corpus_id, sequence, commit_sha)
        REFERENCES public.corpora_corpus_versions(corpus_id, sequence, commit_sha) ON DELETE RESTRICT
);

CREATE FUNCTION public.corpora_corpus_validate_version()
RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER SET search_path = '' AS $$
DECLARE
    request_row public.corpora_corpus_write_ops%ROWTYPE;
BEGIN
    SELECT * INTO request_row FROM public.corpora_corpus_write_ops
        WHERE id = NEW.operation_id AND corpus_id = NEW.corpus_id;
    IF NOT FOUND OR request_row.state NOT IN ('admitted', 'uncertain')
       OR NEW.sequence <> request_row.expected_sequence + 1
       OR NEW.parent_sha IS DISTINCT FROM request_row.expected_parent_sha THEN
        RAISE EXCEPTION 'CORPUS_VERSION_OPERATION_INVALID';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER corpora_corpus_validate_version BEFORE INSERT
    ON public.corpora_corpus_versions FOR EACH ROW
    EXECUTE FUNCTION public.corpora_corpus_validate_version();

CREATE FUNCTION public.corpora_corpus_validate_head()
RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER SET search_path = '' AS $$
DECLARE
    actual_parent text;
BEGIN
    SELECT parent_sha INTO actual_parent FROM public.corpora_corpus_versions
        WHERE corpus_id = NEW.corpus_id AND sequence = NEW.sequence AND commit_sha = NEW.commit_sha;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'CORPUS_HEAD_VERSION_INVALID';
    END IF;
    IF TG_OP = 'INSERT' THEN
        IF NEW.sequence <> 1 OR actual_parent IS NOT NULL THEN
            RAISE EXCEPTION 'CORPUS_HEAD_VERSION_INVALID';
        END IF;
    ELSIF NEW.corpus_id IS DISTINCT FROM OLD.corpus_id OR NEW.sequence <> OLD.sequence + 1
          OR actual_parent IS DISTINCT FROM OLD.commit_sha THEN
        RAISE EXCEPTION 'CORPUS_HEAD_VERSION_INVALID';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER corpora_corpus_validate_head BEFORE INSERT OR UPDATE
    ON public.corpora_corpus_heads FOR EACH ROW
    EXECUTE FUNCTION public.corpora_corpus_validate_head();

CREATE FUNCTION public.corpora_corpus_preserve_history()
RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER SET search_path = '' AS $$
BEGIN
    RAISE EXCEPTION 'CORPUS_HISTORY_IMMUTABLE';
END;
$$;
CREATE TRIGGER corpora_corpus_versions_immutable BEFORE UPDATE OR DELETE
    ON public.corpora_corpus_versions FOR EACH ROW
    EXECUTE FUNCTION public.corpora_corpus_preserve_history();
CREATE TRIGGER corpora_corpus_heads_no_delete BEFORE DELETE
    ON public.corpora_corpus_heads FOR EACH ROW
    EXECUTE FUNCTION public.corpora_corpus_preserve_history();

ALTER TABLE public.corpora_corpus_write_ops ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.corpora_corpus_versions ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.corpora_corpus_heads ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.corpora_corpus_write_ops, public.corpora_corpus_versions,
    public.corpora_corpus_heads FROM PUBLIC, anon, authenticated, service_role;

-- Deliberately read-only until a reviewed admission/finalization boundary is
-- installed. The raw tables are not a safe public write interface.
GRANT SELECT ON public.corpora_corpus_write_ops, public.corpora_corpus_versions,
    public.corpora_corpus_heads TO service_role;
REVOKE ALL ON FUNCTION public.corpora_corpus_validate_version(),
    public.corpora_corpus_validate_head(), public.corpora_corpus_preserve_history()
    FROM PUBLIC, anon, authenticated, service_role;
