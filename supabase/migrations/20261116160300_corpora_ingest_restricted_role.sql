-- Draft for review and disposable local testing only. A trusted issuer must
-- supply a short-lived JWT with role=corpora_ingest; do not put the project JWT
-- signing key in the API. This shared backend role does not enforce per-user
-- ownership: the API must authenticate each request and enforce its owner.
create role corpora_ingest nologin noinherit nobypassrls;
grant corpora_ingest to authenticator;
grant usage on schema public, storage to corpora_ingest;

grant select, insert on public.conversion_jobs to corpora_ingest;
grant select, insert on public.corpora_ingest_requests to corpora_ingest;
grant select, insert on storage.objects to corpora_ingest;

create policy corpora_ingest_select_jobs on public.conversion_jobs
  for select to corpora_ingest
  using (result_key = 'conversion-jobs/' || id || '.graph.json');
create policy corpora_ingest_insert_jobs on public.conversion_jobs
  for insert to corpora_ingest
  with check (
    result_key = 'conversion-jobs/' || id || '.graph.json'
    and id ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
    and owner ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
    and output_format = 'corpus'
    and status = 'succeeded'
  );
-- Restrictive policies also constrain permissive PUBLIC policies that may
-- already exist in a hosted project.
create policy corpora_ingest_bound_jobs_select on public.conversion_jobs
  as restrictive for select to corpora_ingest
  using (result_key = 'conversion-jobs/' || id || '.graph.json');
create policy corpora_ingest_bound_jobs_insert on public.conversion_jobs
  as restrictive for insert to corpora_ingest
  with check (
    result_key = 'conversion-jobs/' || id || '.graph.json'
    and id ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
    and owner ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
    and output_format = 'corpus'
    and status = 'succeeded'
  );
create policy corpora_ingest_select_requests on public.corpora_ingest_requests
  for select to corpora_ingest using (true);
create policy corpora_ingest_insert_requests on public.corpora_ingest_requests
  for insert to corpora_ingest with check (true);

create policy corpora_ingest_select_objects on storage.objects
  for select to corpora_ingest
  using (
    bucket_id = 'conversion-jobs'
    and name ~ '^conversion-jobs/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.graph\.json$'
  );
create policy corpora_ingest_insert_objects on storage.objects
  for insert to corpora_ingest
  with check (
    bucket_id = 'conversion-jobs'
    and name ~ '^conversion-jobs/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.graph\.json$'
  );

create policy corpora_ingest_bound_objects_select on storage.objects
  as restrictive for select to corpora_ingest
  using (
    bucket_id = 'conversion-jobs'
    and name ~ '^conversion-jobs/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.graph\.json$'
  );
create policy corpora_ingest_bound_objects_insert on storage.objects
  as restrictive for insert to corpora_ingest
  with check (
    bucket_id = 'conversion-jobs'
    and name ~ '^conversion-jobs/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.graph\.json$'
  );

-- PUBLIC grants are inherited by every role regardless of NOINHERIT. Abort
-- rather than installing a role that can mutate these tables unexpectedly.
do $$
declare
  relation_name text;
  forbidden_privilege text;
  exposed_schema record;
  exposed_relation record;
  exposed_sequence record;
  exposed_function record;
begin
  if has_database_privilege('corpora_ingest', current_database(), 'CREATE') then
    raise exception 'corpora_ingest has unexpected database CREATE privilege';
  end if;
  for exposed_schema in
    select n.oid, n.nspname
    from pg_namespace n
    where n.nspname not in ('pg_catalog', 'information_schema')
      and left(n.nspname, 3) <> 'pg_'
  loop
    if has_schema_privilege('corpora_ingest', exposed_schema.oid, 'CREATE') then
      raise exception 'corpora_ingest has unexpected CREATE privilege on schema %',
        exposed_schema.nspname;
    end if;
  end loop;
  foreach relation_name in array array[
    'public.conversion_jobs', 'public.corpora_ingest_requests', 'storage.objects'
  ] loop
    if not coalesce((select relrowsecurity from pg_class where oid = to_regclass(relation_name)), false) then
      raise exception 'row-level security is not enabled on %', relation_name;
    end if;
    foreach forbidden_privilege in array array['UPDATE', 'DELETE', 'TRUNCATE', 'REFERENCES', 'TRIGGER'] loop
      if has_table_privilege('corpora_ingest', relation_name, forbidden_privilege) then
        raise exception 'corpora_ingest has unexpected % privilege on %',
          forbidden_privilege, relation_name;
      end if;
    end loop;
    foreach forbidden_privilege in array array['UPDATE', 'REFERENCES'] loop
      if has_any_column_privilege('corpora_ingest', relation_name, forbidden_privilege) then
        raise exception 'corpora_ingest has unexpected column % privilege on %',
          forbidden_privilege, relation_name;
      end if;
    end loop;
    if current_setting('server_version_num')::integer >= 170000 then
      if has_table_privilege('corpora_ingest', relation_name, 'MAINTAIN') then
        raise exception 'corpora_ingest has unexpected MAINTAIN privilege on %', relation_name;
      end if;
    end if;
  end loop;
  -- PUBLIC applies even to NOINHERIT roles. Check all accessible non-system
  -- schemas, including ones that PostgREST does not currently expose.
  for exposed_relation in
    select c.oid, n.nspname, c.relname
    from pg_class c join pg_namespace n on n.oid = c.relnamespace
    where n.nspname not in ('pg_catalog', 'information_schema')
      and left(n.nspname, 3) <> 'pg_'
      and has_schema_privilege('corpora_ingest', n.oid, 'USAGE')
      and c.relkind in ('r', 'p', 'v', 'm', 'f')
      and not (n.nspname = 'public' and c.relname in ('conversion_jobs', 'corpora_ingest_requests'))
      and not (n.nspname = 'storage' and c.relname = 'objects')
  loop
    foreach forbidden_privilege in array array['SELECT', 'INSERT', 'UPDATE', 'DELETE', 'TRUNCATE', 'REFERENCES', 'TRIGGER'] loop
      if has_table_privilege('corpora_ingest', exposed_relation.oid, forbidden_privilege) then
        raise exception 'corpora_ingest has unexpected % privilege on %.%',
          forbidden_privilege, exposed_relation.nspname, exposed_relation.relname;
      end if;
    end loop;
    foreach forbidden_privilege in array array['SELECT', 'INSERT', 'UPDATE', 'REFERENCES'] loop
      if has_any_column_privilege('corpora_ingest', exposed_relation.oid, forbidden_privilege) then
        raise exception 'corpora_ingest has unexpected column % privilege on %.%',
          forbidden_privilege, exposed_relation.nspname, exposed_relation.relname;
      end if;
    end loop;
    if current_setting('server_version_num')::integer >= 170000 then
      if has_table_privilege('corpora_ingest', exposed_relation.oid, 'MAINTAIN') then
        raise exception 'corpora_ingest has unexpected MAINTAIN privilege on %.%',
          exposed_relation.nspname, exposed_relation.relname;
      end if;
    end if;
  end loop;
  for exposed_sequence in
    select c.oid, n.nspname, c.relname
    from pg_class c join pg_namespace n on n.oid = c.relnamespace
    where n.nspname not in ('pg_catalog', 'information_schema')
      and left(n.nspname, 3) <> 'pg_'
      and has_schema_privilege('corpora_ingest', n.oid, 'USAGE')
      and c.relkind = 'S'
  loop
    foreach forbidden_privilege in array array['USAGE', 'SELECT', 'UPDATE'] loop
      if has_sequence_privilege('corpora_ingest', exposed_sequence.oid, forbidden_privilege) then
        raise exception 'corpora_ingest has unexpected % privilege on %.%',
          forbidden_privilege, exposed_sequence.nspname, exposed_sequence.relname;
      end if;
    end loop;
  end loop;
  for exposed_function in
    select p.oid, n.nspname, p.proname
    from pg_proc p join pg_namespace n on n.oid = p.pronamespace
    where n.nspname not in ('pg_catalog', 'information_schema')
      and left(n.nspname, 3) <> 'pg_'
      and has_schema_privilege('corpora_ingest', n.oid, 'USAGE')
      and p.prosecdef
      -- Event triggers require superuser creation. Ordinary trigger functions
      -- remain in scope: PUBLIC TEMP privileges can allow a caller to attach
      -- one to a temporary table it owns.
      and p.prorettype <> 'pg_catalog.event_trigger'::regtype
  loop
    if has_function_privilege('corpora_ingest', exposed_function.oid, 'EXECUTE') then
      raise exception 'corpora_ingest can execute unexpected security-definer function %.%',
        exposed_function.nspname, exposed_function.proname;
    end if;
  end loop;
end $$;
