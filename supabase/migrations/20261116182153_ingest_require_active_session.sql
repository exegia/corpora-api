-- This owner-executed predicate reveals only whether the caller's own signed
-- JWT session still exists. It does not expose auth.sessions rows.
create function public.corpora_ingest_session_active()
returns boolean
language sql
stable
security definer
set search_path = ''
as $$
  select exists (
    select 1 from auth.sessions s
    where s.id = case
      when (select auth.jwt()) ->> 'session_id' ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
      then ((select auth.jwt()) ->> 'session_id')::uuid
      else null
    end
    and s.user_id = (select auth.uid())
    and (s.not_after is null or s.not_after > statement_timestamp())
  )
$$;
revoke all on function public.corpora_ingest_session_active() from public, anon;
grant execute on function public.corpora_ingest_session_active() to authenticated;

-- Existing policies still decide ownership. These additional restrictive
-- checks prevent a signed-out JWT from accessing ingestion data directly.
create policy ingest_session_jobs_select on public.conversion_jobs
  as restrictive for select to authenticated
  using ((select public.corpora_ingest_session_active()));
create policy ingest_session_jobs_insert on public.conversion_jobs
  as restrictive for insert to authenticated
  with check ((select public.corpora_ingest_session_active()));
create policy ingest_session_requests_select on public.corpora_ingest_requests
  as restrictive for select to authenticated
  using ((select public.corpora_ingest_session_active()));
create policy ingest_session_requests_insert on public.corpora_ingest_requests
  as restrictive for insert to authenticated
  with check ((select public.corpora_ingest_session_active()));
create policy ingest_session_objects_select on storage.objects
  as restrictive for select to authenticated
  using (bucket_id <> 'conversion-jobs' or (select public.corpora_ingest_session_active()));
create policy ingest_session_objects_insert on storage.objects
  as restrictive for insert to authenticated
  with check (bucket_id <> 'conversion-jobs' or (select public.corpora_ingest_session_active()));
