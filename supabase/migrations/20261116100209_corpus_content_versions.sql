-- Draft: application code must admit a verified session before inserting a
-- write operation, and must independently verify a retained Hub commit before
-- publishing it. No endpoint currently calls these tables.
-- The first logical corpus version has no earlier published corpus version,
-- but its Hub commit still needs a real Git parent. A trusted provisioner must
-- verify and record a pinned repository baseline before the first claim. This
-- baseline is not a claim about the provider's original creation commit.
CREATE TABLE public.corpora_corpus_provision_ops (
    id uuid PRIMARY KEY,
    corpus_id uuid NOT NULL UNIQUE REFERENCES public.corpora_corpus_metadata(id) ON DELETE RESTRICT,
    actor_id uuid NOT NULL REFERENCES auth.users(id) ON DELETE RESTRICT,
    session_id uuid NOT NULL,
    repo_id text NOT NULL,
    repo_type text NOT NULL CHECK (repo_type IN ('dataset', 'model')),
    state text NOT NULL DEFAULT 'admitted' CHECK (state IN ('admitted', 'executing', 'uncertain', 'completed')),
    worker_claim uuid UNIQUE,
    claimed_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (id, corpus_id, worker_claim),
    CHECK ((state = 'admitted' AND worker_claim IS NULL AND claimed_at IS NULL)
        OR (state <> 'admitted' AND worker_claim IS NOT NULL AND claimed_at IS NOT NULL))
);

CREATE TABLE public.corpora_corpus_bootstrap (
    corpus_id uuid PRIMARY KEY REFERENCES public.corpora_corpus_metadata(id) ON DELETE RESTRICT,
    operation_id uuid NOT NULL UNIQUE,
    worker_claim uuid NOT NULL,
    commit_sha text NOT NULL CHECK (commit_sha ~ '^[0-9a-f]{40}$'),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY (operation_id, corpus_id, worker_claim)
        REFERENCES public.corpora_corpus_provision_ops(id, corpus_id, worker_claim) ON DELETE RESTRICT
);

CREATE TABLE public.corpora_corpus_write_ops (
    id uuid PRIMARY KEY,
    corpus_id uuid NOT NULL REFERENCES public.corpora_corpus_metadata(id) ON DELETE RESTRICT,
    actor_id uuid NOT NULL REFERENCES auth.users(id) ON DELETE RESTRICT,
    session_id uuid NOT NULL,
    expected_sequence bigint NOT NULL CHECK (expected_sequence >= 0),
    expected_parent_sha text CHECK (expected_parent_sha IS NULL OR expected_parent_sha ~ '^[0-9a-f]{40}$'),
    input_sha256 text NOT NULL CHECK (input_sha256 ~ '^[0-9a-f]{64}$'),
    input_format text NOT NULL CHECK (input_format IN ('zip', 'corpus')),
    state text NOT NULL DEFAULT 'admitted' CHECK (state IN ('admitted', 'executing', 'uncertain', 'completed', 'failed')),
    worker_claim uuid UNIQUE,
    claimed_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (id, corpus_id),
    CHECK ((expected_sequence = 0) = (expected_parent_sha IS NULL)),
    CHECK ((state = 'admitted' AND worker_claim IS NULL AND claimed_at IS NULL)
        OR (state <> 'admitted' AND worker_claim IS NOT NULL AND claimed_at IS NOT NULL))
);

-- Until reconciliation confirms the prior outcome, prevent a new save from
-- racing an external write whose result was lost.
CREATE UNIQUE INDEX corpora_corpus_write_ops_one_active
    ON public.corpora_corpus_write_ops (corpus_id) WHERE state IN ('admitted', 'executing', 'uncertain');

CREATE TABLE public.corpora_corpus_versions (
    corpus_id uuid NOT NULL REFERENCES public.corpora_corpus_metadata(id) ON DELETE RESTRICT,
    sequence bigint NOT NULL CHECK (sequence > 0),
    commit_sha text NOT NULL CHECK (commit_sha ~ '^[0-9a-f]{40}$'),
    parent_sha text CHECK (parent_sha IS NULL OR parent_sha ~ '^[0-9a-f]{40}$'),
    operation_id uuid NOT NULL UNIQUE,
    retained_ref text NOT NULL CHECK (retained_ref = 'corpora-op-' || operation_id::text),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (corpus_id, sequence),
    UNIQUE (corpus_id, commit_sha),
    UNIQUE (corpus_id, sequence, commit_sha),
    FOREIGN KEY (operation_id, corpus_id)
        REFERENCES public.corpora_corpus_write_ops(id, corpus_id) ON DELETE RESTRICT,
    CHECK ((sequence = 1) = (parent_sha IS NULL)),
    CHECK (parent_sha IS NULL OR parent_sha <> commit_sha)
);

-- A head can point only to a recorded version for this exact corpus. In the
-- later finalization transaction, version insertion, head advance and operation
-- completion must commit together; initial admission commits separately.
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
    IF NOT FOUND OR request_row.state NOT IN ('executing', 'uncertain')
       OR NEW.sequence <> request_row.expected_sequence + 1
       OR NEW.parent_sha IS DISTINCT FROM request_row.expected_parent_sha
       OR (NEW.sequence = 1 AND NOT EXISTS (
           SELECT 1 FROM public.corpora_corpus_bootstrap b WHERE b.corpus_id = NEW.corpus_id
       )) THEN
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
CREATE TRIGGER corpora_corpus_bootstrap_immutable BEFORE UPDATE OR DELETE
    ON public.corpora_corpus_bootstrap FOR EACH ROW
    EXECUTE FUNCTION public.corpora_corpus_preserve_history();

CREATE FUNCTION public.corpora_corpus_validate_provision()
RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER SET search_path = '' AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.state <> 'admitted' OR NEW.worker_claim IS NOT NULL OR NEW.claimed_at IS NOT NULL
           OR NOT EXISTS (
               SELECT 1 FROM public.corpora_corpus_metadata m
               WHERE m.id = NEW.corpus_id AND m.state = 'provisioning'
                 AND m.hf_repo_id = NEW.repo_id AND m.hf_repo_type = NEW.repo_type
           ) THEN
            RAISE EXCEPTION 'CORPUS_PROVISION_INVALID';
        END IF;
        RETURN NEW;
    END IF;
    IF NEW.id IS DISTINCT FROM OLD.id OR NEW.corpus_id IS DISTINCT FROM OLD.corpus_id
       OR NEW.actor_id IS DISTINCT FROM OLD.actor_id OR NEW.session_id IS DISTINCT FROM OLD.session_id
       OR NEW.repo_id IS DISTINCT FROM OLD.repo_id OR NEW.repo_type IS DISTINCT FROM OLD.repo_type
       OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
        RAISE EXCEPTION 'CORPUS_PROVISION_IDENTITY_IMMUTABLE';
    END IF;
    IF NOT ((OLD.state = 'admitted' AND NEW.state IN ('admitted', 'executing'))
        OR (OLD.state = 'executing' AND NEW.state IN ('executing', 'uncertain', 'completed'))
        OR (OLD.state = 'uncertain' AND NEW.state IN ('uncertain', 'completed'))
        OR (OLD.state = 'completed' AND NEW.state = 'completed')) THEN
        RAISE EXCEPTION 'CORPUS_PROVISION_STATE_INVALID';
    END IF;
    IF OLD.state = 'admitted' AND NEW.state = 'executing' THEN
        IF NEW.worker_claim IS NULL OR NEW.claimed_at IS NOT NULL THEN
            RAISE EXCEPTION 'CORPUS_PROVISION_CLAIM_INVALID';
        END IF;
        NEW.claimed_at := clock_timestamp();
    ELSIF NEW.worker_claim IS DISTINCT FROM OLD.worker_claim
          OR NEW.claimed_at IS DISTINCT FROM OLD.claimed_at THEN
        RAISE EXCEPTION 'CORPUS_PROVISION_CLAIM_IMMUTABLE';
    END IF;
    IF NEW.state = 'completed' AND NOT EXISTS (
        SELECT 1 FROM public.corpora_corpus_bootstrap b
        WHERE b.operation_id = NEW.id AND b.corpus_id = NEW.corpus_id
          AND b.worker_claim = NEW.worker_claim
    ) THEN
        RAISE EXCEPTION 'CORPUS_PROVISION_RESULT_MISSING';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER corpora_corpus_validate_provision BEFORE INSERT OR UPDATE
    ON public.corpora_corpus_provision_ops FOR EACH ROW
    EXECUTE FUNCTION public.corpora_corpus_validate_provision();
CREATE TRIGGER corpora_corpus_provision_ops_no_delete BEFORE DELETE
    ON public.corpora_corpus_provision_ops FOR EACH ROW
    EXECUTE FUNCTION public.corpora_corpus_preserve_history();

CREATE FUNCTION public.corpora_corpus_validate_bootstrap()
RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER SET search_path = '' AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM public.corpora_corpus_provision_ops p
        JOIN public.corpora_corpus_metadata m ON m.id = p.corpus_id
        WHERE p.id = NEW.operation_id AND p.corpus_id = NEW.corpus_id
          AND p.worker_claim = NEW.worker_claim AND p.state IN ('executing', 'uncertain')
          AND m.state = 'provisioning' AND m.hf_repo_id = p.repo_id AND m.hf_repo_type = p.repo_type
          AND NOT EXISTS (SELECT 1 FROM public.corpora_corpus_heads h WHERE h.corpus_id = m.id)
    ) THEN
        RAISE EXCEPTION 'CORPUS_BOOTSTRAP_OPERATION_INVALID';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER corpora_corpus_validate_bootstrap BEFORE INSERT
    ON public.corpora_corpus_bootstrap FOR EACH ROW
    EXECUTE FUNCTION public.corpora_corpus_validate_bootstrap();

CREATE FUNCTION public.corpora_corpus_bootstrap_finalized()
RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER SET search_path = '' AS $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM public.corpora_corpus_provision_ops p
                   WHERE p.id = NEW.operation_id AND p.corpus_id = NEW.corpus_id
                     AND p.worker_claim = NEW.worker_claim AND p.state = 'completed') THEN
        RAISE EXCEPTION 'CORPUS_BOOTSTRAP_NOT_FINALIZED';
    END IF;
    RETURN NULL;
END;
$$;
CREATE CONSTRAINT TRIGGER corpora_corpus_bootstrap_finalized
    AFTER INSERT ON public.corpora_corpus_bootstrap DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION public.corpora_corpus_bootstrap_finalized();

CREATE FUNCTION public.corpora_corpus_validate_operation()
RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER SET search_path = '' AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.state <> 'admitted' OR NEW.worker_claim IS NOT NULL OR NEW.claimed_at IS NOT NULL THEN
            RAISE EXCEPTION 'CORPUS_OPERATION_STATE_INVALID';
        END IF;
        RETURN NEW;
    END IF;
    IF NEW.id IS DISTINCT FROM OLD.id OR NEW.corpus_id IS DISTINCT FROM OLD.corpus_id
       OR NEW.actor_id IS DISTINCT FROM OLD.actor_id OR NEW.session_id IS DISTINCT FROM OLD.session_id
       OR NEW.expected_sequence IS DISTINCT FROM OLD.expected_sequence
       OR NEW.expected_parent_sha IS DISTINCT FROM OLD.expected_parent_sha
       OR NEW.input_sha256 IS DISTINCT FROM OLD.input_sha256
       OR NEW.input_format IS DISTINCT FROM OLD.input_format
       OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
        RAISE EXCEPTION 'CORPUS_OPERATION_IDENTITY_IMMUTABLE';
    END IF;
    IF NOT (
        (OLD.state = 'admitted' AND NEW.state IN ('admitted', 'executing'))
        OR (OLD.state = 'executing' AND NEW.state IN ('executing', 'uncertain', 'completed', 'failed'))
        OR (OLD.state = 'uncertain' AND NEW.state IN ('uncertain', 'completed', 'failed'))
        OR (OLD.state IN ('completed', 'failed') AND NEW.state = OLD.state)
    ) THEN
        RAISE EXCEPTION 'CORPUS_OPERATION_STATE_INVALID';
    END IF;
    IF OLD.state = 'admitted' AND NEW.state = 'executing' THEN
        IF NEW.worker_claim IS NULL OR NEW.claimed_at IS NOT NULL THEN
            RAISE EXCEPTION 'CORPUS_WORKER_CLAIM_INVALID';
        END IF;
        NEW.claimed_at := clock_timestamp();
    ELSIF NEW.worker_claim IS DISTINCT FROM OLD.worker_claim
          OR NEW.claimed_at IS DISTINCT FROM OLD.claimed_at THEN
        RAISE EXCEPTION 'CORPUS_WORKER_CLAIM_IMMUTABLE';
    END IF;
    IF NEW.state = 'completed' AND OLD.state <> 'completed' AND NOT EXISTS (
        SELECT 1 FROM public.corpora_corpus_versions v
        JOIN public.corpora_corpus_heads h ON h.corpus_id = v.corpus_id
            AND h.sequence = v.sequence AND h.commit_sha = v.commit_sha
        WHERE v.operation_id = NEW.id
    ) THEN
        RAISE EXCEPTION 'CORPUS_OPERATION_RESULT_MISSING';
    END IF;
    IF NEW.state = 'failed' AND EXISTS (
        SELECT 1 FROM public.corpora_corpus_versions v WHERE v.operation_id = NEW.id
    ) THEN
        RAISE EXCEPTION 'CORPUS_OPERATION_RESULT_EXISTS';
    END IF;
    NEW.updated_at := clock_timestamp();
    RETURN NEW;
END;
$$;
CREATE TRIGGER corpora_corpus_validate_operation BEFORE INSERT OR UPDATE
    ON public.corpora_corpus_write_ops FOR EACH ROW
    EXECUTE FUNCTION public.corpora_corpus_validate_operation();
CREATE TRIGGER corpora_corpus_write_ops_no_delete BEFORE DELETE
    ON public.corpora_corpus_write_ops FOR EACH ROW
    EXECUTE FUNCTION public.corpora_corpus_preserve_history();

CREATE FUNCTION public.corpora_corpus_validate_finalized_version()
RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER SET search_path = '' AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM public.corpora_corpus_write_ops o
        JOIN public.corpora_corpus_heads h ON h.corpus_id = o.corpus_id
        WHERE o.id = NEW.operation_id AND o.corpus_id = NEW.corpus_id
          AND o.state = 'completed' AND h.sequence >= NEW.sequence
    ) THEN
        RAISE EXCEPTION 'CORPUS_VERSION_NOT_FINALIZED';
    END IF;
    RETURN NULL;
END;
$$;
CREATE CONSTRAINT TRIGGER corpora_corpus_version_finalized
    AFTER INSERT ON public.corpora_corpus_versions DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION public.corpora_corpus_validate_finalized_version();

ALTER TABLE public.corpora_corpus_write_ops ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.corpora_corpus_versions ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.corpora_corpus_heads ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.corpora_corpus_bootstrap ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.corpora_corpus_provision_ops ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.corpora_corpus_provision_ops, public.corpora_corpus_bootstrap, public.corpora_corpus_write_ops, public.corpora_corpus_versions,
    public.corpora_corpus_heads FROM PUBLIC, anon, authenticated, service_role;

-- Deliberately read-only until a reviewed admission/finalization boundary is
-- installed. The raw tables are not a safe public write interface.
GRANT SELECT ON public.corpora_corpus_provision_ops, public.corpora_corpus_bootstrap, public.corpora_corpus_write_ops, public.corpora_corpus_versions,
    public.corpora_corpus_heads TO service_role;
REVOKE ALL ON FUNCTION public.corpora_corpus_validate_version(),
    public.corpora_corpus_validate_head(), public.corpora_corpus_preserve_history(),
    public.corpora_corpus_validate_operation(), public.corpora_corpus_validate_finalized_version(),
    public.corpora_corpus_validate_provision(), public.corpora_corpus_validate_bootstrap(),
    public.corpora_corpus_bootstrap_finalized()
    FROM PUBLIC, anon, authenticated, service_role;
