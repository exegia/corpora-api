-- Isolated-test schema only. Never run this file against a deployed project.
CREATE SCHEMA auth;
CREATE FUNCTION auth.uid() RETURNS uuid LANGUAGE sql STABLE AS $$
    SELECT nullif(current_setting('request.jwt.claim.sub',true),'')::uuid;
$$;
GRANT USAGE ON SCHEMA auth TO anon,authenticated,service_role;
CREATE SCHEMA storage;
CREATE TABLE storage.buckets(id text PRIMARY KEY,name text,public boolean DEFAULT false);
CREATE TABLE storage.objects(id uuid PRIMARY KEY,bucket_id text REFERENCES storage.buckets(id));
ALTER TABLE storage.objects ENABLE ROW LEVEL SECURITY;
GRANT USAGE ON SCHEMA storage TO anon,authenticated,service_role;
GRANT ALL ON storage.objects,storage.buckets TO anon,authenticated,service_role;
-- Deliberately broad allow policy: migration's restrictive policy must still win.
CREATE POLICY fixture_storage_allow ON storage.objects FOR ALL TO PUBLIC USING(true) WITH CHECK(true);
CREATE TABLE public.users(id uuid PRIMARY KEY);
CREATE TABLE public.corpus_documents(
    id uuid PRIMARY KEY, name text NOT NULL, source text NOT NULL, path text NOT NULL,
    filename text, job_id text, status text
);
ALTER TABLE public.corpus_documents ENABLE ROW LEVEL SECURITY;
CREATE POLICY fixture_documents_select ON public.corpus_documents FOR SELECT TO PUBLIC USING(true);
CREATE POLICY fixture_documents_insert ON public.corpus_documents FOR INSERT TO PUBLIC WITH CHECK(true);
CREATE POLICY fixture_documents_delete ON public.corpus_documents FOR DELETE TO PUBLIC USING(true);
GRANT ALL ON public.corpus_documents TO anon,authenticated,service_role;
CREATE TABLE public.corpus_commits(
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id uuid NOT NULL REFERENCES public.corpus_documents(id),
    sha text NOT NULL, message text NOT NULL, UNIQUE(document_id,sha)
);
ALTER TABLE public.corpus_commits ENABLE ROW LEVEL SECURITY;
CREATE POLICY fixture_commits_select ON public.corpus_commits FOR SELECT TO PUBLIC USING(true);
CREATE POLICY fixture_commits_insert ON public.corpus_commits FOR INSERT TO PUBLIC WITH CHECK(true);
CREATE POLICY fixture_commits_delete ON public.corpus_commits FOR DELETE TO PUBLIC USING(true);
GRANT ALL ON public.corpus_commits TO anon,authenticated,service_role;
CREATE TABLE public.projects(
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(), name text NOT NULL,
    corpus_document_id uuid REFERENCES public.corpus_documents(id) ON DELETE SET NULL
);
ALTER TABLE public.projects ENABLE ROW LEVEL SECURITY;
CREATE POLICY fixture_projects_access ON public.projects FOR ALL TO PUBLIC USING(true) WITH CHECK(true);
GRANT ALL ON public.projects TO anon,authenticated,service_role;
CREATE TABLE public.corpus_changes(
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id uuid NOT NULL REFERENCES public.corpus_documents(id) ON DELETE CASCADE,
    commit_id uuid REFERENCES public.corpus_commits(id) ON DELETE SET NULL,
    reverts_change_id uuid UNIQUE REFERENCES public.corpus_changes(id) ON DELETE RESTRICT,
    version text NOT NULL,node_id bigint NOT NULL,node_type text,field text NOT NULL,
    previous_value text,new_value text,
    kind text NOT NULL CHECK(kind IN ('annotation','label','boundary','formatting','reference','text')),
    status text NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','applied','failed','reverted')),
    resp text[] NOT NULL CHECK(cardinality(resp)>=2),
    applied_by uuid NOT NULL REFERENCES public.users(id) ON DELETE RESTRICT,
    applied_at timestamptz,suggestion_id text,thread_id text,rule text,rationale text,diff jsonb,scope jsonb,
    content_hash_before text,source text NOT NULL DEFAULT 'ai_panel' CHECK(source IN ('ai_panel','api','import')),
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK(status <> 'applied' OR applied_at IS NOT NULL),
    CHECK(previous_value IS DISTINCT FROM new_value)
);
ALTER TABLE public.corpus_changes ENABLE ROW LEVEL SECURITY;
CREATE POLICY corpus_changes_select ON public.corpus_changes FOR SELECT TO authenticated USING(true);
GRANT ALL ON public.corpus_changes TO anon,authenticated,service_role;
GRANT SELECT, REFERENCES ON public.users,public.corpus_documents TO service_role;
