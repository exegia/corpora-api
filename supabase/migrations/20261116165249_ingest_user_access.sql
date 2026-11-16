-- Ingestion uses the signed-in user's JWT. Storage and metadata ownership are
-- enforced by Postgres even if requests bypass the API.
grant select, insert on public.conversion_jobs to authenticated;
grant select, insert on public.corpora_ingest_requests to authenticated;

create policy ingest_own_jobs_select on public.conversion_jobs
  for select to authenticated using (owner = (select auth.uid())::text);
create policy ingest_own_jobs_insert on public.conversion_jobs
  for insert to authenticated with check (
    owner = (select auth.uid())::text
    and result_key = 'conversion-jobs/' || owner || '/' || id || '.graph.json'
    and id ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
    and output_format = 'corpus' and status = 'succeeded'
  );
-- A future permissive policy must not permit a user to read or insert another
-- user's rows. This table currently has no user-facing hosted policies.
create policy ingest_bound_jobs_select on public.conversion_jobs
  as restrictive for select to public
  using (owner = (select auth.uid())::text);
create policy ingest_bound_jobs_insert on public.conversion_jobs
  as restrictive for insert to public
  with check (
    owner = (select auth.uid())::text
    and result_key = 'conversion-jobs/' || owner || '/' || id || '.graph.json'
    and id ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
    and output_format = 'corpus' and status = 'succeeded'
  );

create policy ingest_own_requests_select on public.corpora_ingest_requests
  for select to authenticated using (owner_id = (select auth.uid()));
create policy ingest_own_requests_insert on public.corpora_ingest_requests
  for insert to authenticated with check (owner_id = (select auth.uid()));
create policy ingest_bound_requests_select on public.corpora_ingest_requests
  as restrictive for select to public using (owner_id = (select auth.uid()));
create policy ingest_bound_requests_insert on public.corpora_ingest_requests
  as restrictive for insert to public with check (owner_id = (select auth.uid()));

create policy ingest_own_objects_select on storage.objects
  for select to authenticated using (
    bucket_id = 'conversion-jobs'
    and owner_id = (select auth.uid())::text
    and name ~ ('^conversion-jobs/' || (select auth.uid())::text || '/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.graph\.json$')
  );
create policy ingest_own_objects_insert on storage.objects
  for insert to authenticated with check (
    bucket_id = 'conversion-jobs'
    and owner_id = (select auth.uid())::text
    and name ~ ('^conversion-jobs/' || (select auth.uid())::text || '/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.graph\.json$')
  );
-- Scope the restrictive guard to this bucket, preserving policies on every
-- other bucket. Include PUBLIC so permissive PUBLIC policies cannot widen it.
create policy ingest_bound_objects_select on storage.objects
  as restrictive for select to public using (
    bucket_id <> 'conversion-jobs' or (
      owner_id = (select auth.uid())::text
      and name ~ ('^conversion-jobs/' || (select auth.uid())::text || '/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.graph\.json$')
    )
  );
create policy ingest_bound_objects_insert on storage.objects
  as restrictive for insert to public with check (
    bucket_id <> 'conversion-jobs' or (
      owner_id = (select auth.uid())::text
      and name ~ ('^conversion-jobs/' || (select auth.uid())::text || '/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.graph\.json$')
    )
  );
create policy ingest_bound_objects_update on storage.objects
  as restrictive for update to public
  using (bucket_id <> 'conversion-jobs') with check (bucket_id <> 'conversion-jobs');
create policy ingest_bound_objects_delete on storage.objects
  as restrictive for delete to public using (bucket_id <> 'conversion-jobs');
