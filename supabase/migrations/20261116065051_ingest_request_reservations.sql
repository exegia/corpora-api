-- The API reserves an owner-scoped request identity before starting conversion.
-- Only the trusted API service may access this bookkeeping table.
create table public.corpora_ingest_requests (
    owner_id uuid not null,
    request_key_hash text not null check (request_key_hash ~ '^[0-9a-f]{64}$'),
    input_hash text not null check (input_hash ~ '^[0-9a-f]{64}$'),
    job_id uuid not null unique,
    created_at timestamptz not null default now(),
    primary key (owner_id, request_key_hash)
);

alter table public.corpora_ingest_requests enable row level security;
revoke all on public.corpora_ingest_requests from public, anon, authenticated, service_role;
grant select, insert on public.corpora_ingest_requests to service_role;
