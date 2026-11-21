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
The hosted procedures below are documented but not yet verified.

## Create a disposable local stack

Use a separate empty directory. With a Supabase CLI that supports the native stack:

```sh
mkdir -p /tmp/corpora-fresh
supabase init --workdir /tmp/corpora-fresh
uv run python scripts/prepare_fresh_supabase.py --workdir /tmp/corpora-fresh --native
supabase start --workdir /tmp/corpora-fresh
uv run --with 'psycopg[binary]' python scripts/initialize_fresh_supabase.py --workdir /tmp/corpora-fresh
CORPORA_TEST_FRESH_BOOTSTRAP=1 CORPORA_TEST_FRESH_WORKDIR=/tmp/corpora-fresh uv run --with 'psycopg[binary]' python -m pytest tests/api/test_fresh_bootstrap_supabase.py -q -rs
```

Use `python -m pytest`: a bare `pytest` resolves to the project environment, cannot
import the temporary `psycopg`, and silently skips both tests. Check that the output
reports `2 passed`. Run the tests before creating users: the first requires an empty
Auth directory. Both create temporary users and records and remove them afterwards.

The second test checks the authorization that corpora-web relies on, for a
superadmin, an ordinary user, an anonymous caller (publishable key only, which is
what the Web sends after sign-out), and a revoked session token. It covers the
`corpora_project_superadmin` role check, licence catalogue reads, creation, edits
and text saves, and project licence attach, agree and detach. It does not run the
Web application itself. Project review and publication are restricted only in the
Web: the database lets a project owner set any `status`, as the hosted project does.
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

## Fresh hosted project (not yet verified)

This path has not been run against a hosted project. Use it only on a new, empty,
disposable project created for the purpose, never on the existing hosted project.
The initializer refuses non-local stacks, so the hosted steps below are manual.

1. Create the project and record its reference. Do not seed it or create users.
2. Prepare a new workdir as above, without `--native`, then link it:
   `supabase link --workdir DIR --project-ref REF`.
3. Confirm that the target has no migration history and no application objects:
   `supabase migration list --workdir DIR --linked` must show no remote versions.
4. Apply the prepared baseline and later migrations: `supabase db push --workdir DIR`.
   The baseline aborts with `FRESH_BOOTSTRAP_REQUIRES_EMPTY_PROJECT` if the project
   is not empty; investigate instead of retrying.
5. Create the private `conversion-jobs` and `project-corpora` buckets in the dashboard
   or with the Storage API, and confirm both report `public: false`.
6. Configure Auth (site URL, redirect URLs, providers) for the Web deployment that
   will use the project. `db push` does not change Auth settings.
7. Create and confirm the first administrator in Auth, then, in the SQL editor, run
   `INSERT INTO corpora_session_guard.superadmins(user_id) VALUES ('USER_UUID');`
   No other account receives a role.
8. Check whether the platform installed an automatic RLS event trigger, and compare
   effective table, column and function grants with a fresh local stack.
9. Repeat the checks of the integration tests by hand or by pointing an adapted copy
   at the project, then delete the project when the evidence is recorded.

## Upgrade the existing hosted database

Never apply the baseline, the preparer output, or `db push --include-all` to the
existing hosted database. The repository contains the legacy AI draft registry
migration (`20261115042900`), which the hosted history deliberately lacks and which
`--include-all` would apply.

1. Read the hosted history: `supabase migration list --linked` from a linked
   workdir, or a read-only query of `supabase_migrations.schema_migrations`.
2. List every file in `supabase/migrations` whose version is missing from that
   history. Exclude `20261115042900_legacy_ai_draft_registry.sql`.
3. Review each missing migration in timestamp order. Read what it changes on the
   current hosted schema, and run it on a fresh local stack first.
4. Apply one reviewed migration at a time, unchanged, recording the same version
   and name in the hosted history (for example with the dashboard migration tool
   or `supabase migration repair --status applied VERSION` after a transactional
   apply). Stop at the first failure.
5. Confirm the new history and check the changed grants and policies with
   read-only queries. Do not seed users, roles, or data during an upgrade.

## AI draft limitation

The current hosted schema does not contain the legacy AI draft registry. This
baseline mirrors that schema and does not provision `corpora_ai_storage` or the
`corpus-ai-drafts` bucket. Existing draft API routes that depend on that registry
will return 503 until the active AI migration work supplies and verifies its
persistence. Do not replay the historical legacy registry migration blindly: it
changes existing policies and writes directly to Supabase-managed Storage tables.
