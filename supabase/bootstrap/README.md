# Fresh Supabase development database

This schema-only baseline represents the hosted application schema through migration
`20261118100059`. The earliest hosted migrations are missing from the repositories,
so the existing migration histories cannot reconstruct a fresh database. Do not replay
migrations at or before that version on top of this baseline. Later API migrations are
copied in timestamp order. The baseline refuses a database with application objects,
users, or storage buckets; never use it to replace an existing project.

The baseline contains application schema, grants, policies, the Auth user trigger, and
18 reviewed Storage policies. It contains no user accounts, superadmins, corpus data,
or credentials. The hosted automatic RLS event trigger is not installed: every future
migration must explicitly enable RLS on its tables. Client execution of its event
function is revoked. This setup has been tested locally, not on a fresh hosted project.

## Create a disposable local stack

Use a separate empty directory. With a Supabase CLI that supports the native stack:

```sh
mkdir -p /tmp/corpora-fresh
supabase init --workdir /tmp/corpora-fresh
uv run python scripts/prepare_fresh_supabase.py --workdir /tmp/corpora-fresh --native
supabase start --workdir /tmp/corpora-fresh
uv run --with 'psycopg[binary]' python scripts/initialize_fresh_supabase.py --workdir /tmp/corpora-fresh
CORPORA_TEST_FRESH_BOOTSTRAP=1 CORPORA_TEST_FRESH_WORKDIR=/tmp/corpora-fresh uv run --with 'psycopg[binary]' python -m pytest tests/api/test_fresh_bootstrap_supabase.py -q
```

Run the integration test before creating users: it requires an empty Auth directory.
The initializer creates the private buckets without assigning a superadmin. To assign
one explicitly, create and confirm a local Auth user and rerun the initializer with
`--superadmin-user USER_UUID`. The licence catalog and user directory are unseeded;
features that need their records require explicit local test data.

When newer migrations are added, run the preparer again with `--sync`, then apply
those migrations with the Supabase CLI. Sync checks every existing migration byte for
byte, refuses unknown files and out-of-order additions, and never overwrites them.

To remove only a disposable stack and its local data, run `supabase stop --workdir
/tmp/corpora-fresh` followed by `supabase stack destroy --workdir /tmp/corpora-fresh
--yes`. Do not use these cleanup commands on a project whose data you need.

## AI working drafts

The baseline mirrors the hosted schema of 2026-11-18, which has no AI draft
registry. Migration `20261120090000_working_draft_registry.sql`, applied by the
ordered sync after the baseline, adds the server-only `public.corpus_ai_drafts`
table, the `corpora_ai_storage(text,uuid,jsonb)` RPC and a restrictive Storage
policy that keeps browser roles out of the `corpus-ai-drafts` bucket. The
initializer creates that bucket as private through the Storage API. The RPC
reports no draft capability, and the draft API returns 503, until the bucket
exists and is private.

The historical `20261115042900_legacy_ai_draft_registry.sql` predates the
baseline and is never copied. Do not replay it: it changes existing policies and
writes directly to Supabase-managed Storage tables. The new migration refuses to
run if any draft registry is already present.

To verify the draft lifecycle on a fresh stack, after initialization:

```sh
CORPORA_TEST_DRAFT_REGISTRY=1 CORPORA_TEST_DRAFT_REGISTRY_WORKDIR=/tmp/corpora-fresh uv run --with 'psycopg[binary]' python -m pytest tests/api/test_draft_registry_supabase.py -q
```
