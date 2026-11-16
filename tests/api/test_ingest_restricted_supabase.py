"""Exercise the restricted ingestion role against a disposable local stack."""

import json
import os
import time
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

import httpx
import pytest

from corpora_api.ingest import build_plain_graph
from corpora_api.ingest_requests import input_fingerprint, reserve_request
from corpora_api.ingest_store import IngestNotFound, IngestStore

pytestmark = pytest.mark.skipif(
    os.getenv("CORPORA_TEST_DISPOSABLE_SUPABASE") != "1",
    reason="requires disposable local Supabase",
)


def require_disposable_stack():
    """Match both endpoints to the receipt for the explicitly named test stack."""
    manifest = json.loads(Path(os.environ["CORPORA_TEST_STACK_MANIFEST"]).read_text())
    if manifest.get("state") != "started" or not manifest.get("name", "").startswith(
        "corpora-ingest-test-"
    ):
        raise RuntimeError("A dedicated ingestion test stack is required")
    stack = json.loads((Path(manifest["directory"]) / "start.json").read_text())
    if stack.get("runtime") != "native" or any(
        stack["env"].get(key) != os.environ[key] for key in ("API_URL", "DB_URL")
    ):
        raise RuntimeError("Connection settings do not match the disposable stack receipt")


def test_restricted_role_limits_jobs_and_storage(monkeypatch):
    psycopg = pytest.importorskip("psycopg")
    sql = pytest.importorskip("psycopg.sql")
    jwt = pytest.importorskip("jwt")
    require_disposable_stack()
    url, dsn = os.environ["API_URL"].rstrip("/"), os.environ["DB_URL"]
    assert urlparse(url).hostname in {"localhost", "127.0.0.1", "::1"}
    assert urlparse(dsn).hostname in {"localhost", "127.0.0.1", "::1"}
    api_key, service = os.environ["ANON_KEY"], os.environ["SERVICE_ROLE_KEY"]
    secret = os.environ["CORPORA_TEST_LOCAL_JWT_SECRET"]
    token = jwt.encode(
        {"role": "corpora_ingest", "exp": int(time.time()) + 300}, secret, algorithm="HS256"
    )
    service_headers = {"apikey": service, "Authorization": "Bearer " + service}
    scoped_headers = {"apikey": api_key, "Authorization": "Bearer " + token}
    monkeypatch.setenv("SUPABASE_URL", url)
    monkeypatch.setenv("SUPABASE_INGEST_API_KEY", api_key)
    monkeypatch.setenv("SUPABASE_INGEST_BEARER_TOKEN", token)
    monkeypatch.setenv("SUPABASE_JOBS_BUCKET", "conversion-jobs")
    root = Path(__file__).resolve().parents[2]
    jobs_sql = Path(os.environ["CORPORA_TEST_JOBS_SCHEMA"]).read_text()
    reservations = next((root / "supabase/migrations").glob("*ingest_request_reservations.sql"))
    restricted = next((root / "supabase/migrations").glob("*corpora_ingest_restricted_role.sql"))
    owner, other, job = str(uuid4()), str(uuid4()), str(uuid4())
    other_bucket = "ingest-denied-" + uuid4().hex
    attempted_buckets = []
    confirmed_buckets = []
    uncertain_bucket_write = False
    uploaded = []
    created_test_policies = []
    role_policies_installed = False
    with httpx.Client(timeout=20) as client, psycopg.connect(dsn, autocommit=True) as db:
        assert db.execute("select to_regclass('public.conversion_jobs') is null").fetchone()[0]
        assert db.execute(
            "select to_regclass('public.corpora_ingest_requests') is null"
        ).fetchone()[0]
        assert not db.execute(
            "select exists(select 1 from pg_roles where rolname='corpora_ingest')"
        ).fetchone()[0]
        role_policies = [
            "corpora_ingest_select_objects",
            "corpora_ingest_insert_objects",
            "corpora_ingest_bound_objects_select",
            "corpora_ingest_bound_objects_insert",
        ]
        test_policies = ["corpora_test_public_read", "corpora_test_public_insert"]
        existing_policies = db.execute(
            "select policyname from pg_policies "
            "where schemaname='storage' and tablename='objects' and policyname = any(%s)",
            (role_policies + test_policies,),
        ).fetchall()
        assert not existing_policies
        for bucket in ("conversion-jobs", other_bucket):
            assert not db.execute(
                "select exists(select 1 from storage.buckets where id=%s)", (bucket,)
            ).fetchone()[0]
        try:
            db.execute(jobs_sql)
            db.execute(reservations.read_text())
            db.execute(restricted.read_text())
            role_policies_installed = True
            db.execute("notify pgrst, 'reload schema'")
            for bucket in ("conversion-jobs", other_bucket):
                attempted_buckets.append(bucket)
                try:
                    response = client.post(
                        f"{url}/storage/v1/bucket",
                        headers=service_headers,
                        json={"id": bucket, "name": bucket, "public": False},
                    )
                    response.raise_for_status()
                except httpx.HTTPError:
                    uncertain_bucket_write = True
                    raise
                confirmed_buckets.append(bucket)
            # Model permissive PUBLIC policies already present on the hosted
            # project; a role-specific restrictive policy must still apply.
            predicate = sql.SQL("bucket_id in ('conversion-jobs', {})").format(
                sql.Literal(other_bucket)
            )
            db.execute(
                sql.SQL(
                    "create policy corpora_test_public_read on storage.objects "
                    "for select to public using ({})"
                ).format(predicate)
            )
            created_test_policies.append("corpora_test_public_read")
            db.execute(
                sql.SQL(
                    "create policy corpora_test_public_insert on storage.objects "
                    "for insert to public with check ({})"
                ).format(predicate)
            )
            created_test_policies.append("corpora_test_public_insert")
            db.execute(
                "create policy corpora_test_public_other_jobs on public.conversion_jobs "
                "for select to public using (result_key like 'other/%')"
            )
            for table in (
                "public.conversion_jobs",
                "public.corpora_ingest_requests",
                "storage.objects",
            ):
                for privilege in ("UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"):
                    assert not db.execute(
                        "select has_table_privilege('corpora_ingest', %s, %s)", (table, privilege)
                    ).fetchone()[0]
            assert not db.execute(
                "select has_table_privilege('corpora_ingest', 'storage.buckets', 'SELECT')"
            ).fetchone()[0]
            roles = db.execute(
                "select rolsuper, rolcreaterole, rolcreatedb, rolcanlogin, "
                "rolbypassrls, rolinherit from pg_roles where rolname='corpora_ingest'"
            ).fetchone()
            assert not any(roles)
            deadline = time.monotonic() + 10
            while True:
                probe = client.get(
                    f"{url}/rest/v1/corpora_ingest_requests",
                    params={"select": "job_id", "limit": "0"},
                    headers=scoped_headers,
                )
                if probe.status_code != 404 or time.monotonic() >= deadline:
                    break
                time.sleep(0.1)
            probe_status = probe.status_code
            probe_code = probe.json().get("code") if probe_status != 200 else None
            assert probe_status == 200, f"reservation probe: {probe_status} ({probe_code})"
            store = IngestStore()
            graph = json.dumps(build_plain_graph("sample.txt", b"local sample").to_wire()).encode()
            fingerprint = input_fingerprint(
                "sample.txt", b"local sample", name="", language="und", corpus_id=""
            )
            reserved = reserve_request(store, owner, "safe-retry", fingerprint)
            assert reserve_request(store, owner, "safe-retry", fingerprint) == reserved
            uploaded.append(("conversion-jobs", f"conversion-jobs/{job}.graph.json"))
            store.save(owner, job, "sample.txt", "sample", graph)
            assert store.get(owner, job)["id"] == job
            with pytest.raises(IngestNotFound):
                store.get(other, job)
            assert store.download(owner, job)[0] == graph
            assert store.recover(owner, job, "sample.txt")
            unrelated_job = str(uuid4())
            db.execute(
                "insert into public.conversion_jobs "
                "(id, source_format, name, status, created_at, result_key) "
                "values (%s, 'txt', 'other', 'succeeded', 0, %s)",
                (unrelated_job, "other/" + unrelated_job),
            )
            scoped_other_job = client.get(
                f"{url}/rest/v1/conversion_jobs",
                params={"select": "id", "id": f"eq.{unrelated_job}"},
                headers=scoped_headers,
            )
            scoped_other_status = scoped_other_job.status_code
            assert scoped_other_status == 200 and scoped_other_job.json() == []
            # This shared role can read permitted objects for all owners. The
            # API's owner-bound job lookup is the cross-user boundary.
            object_response = client.get(
                f"{url}/storage/v1/object/conversion-jobs/conversion-jobs/{job}.graph.json",
                headers=scoped_headers,
            )
            object_status = object_response.status_code
            assert object_status == 200
            other_path = f"conversion-jobs/{uuid4()}.graph.json"
            uploaded.append((other_bucket, other_path))
            seeded = client.post(
                f"{url}/storage/v1/object/{other_bucket}/{other_path}",
                headers={**service_headers, "Content-Type": "application/json"},
                content=b"{}",
            )
            seeded.raise_for_status()
            # Confirm the test policy really makes this other object visible to
            # an ordinary public role; otherwise the denial would prove little.
            public_response = client.get(
                f"{url}/storage/v1/object/{other_bucket}/{other_path}",
                headers={"apikey": api_key, "Authorization": "Bearer " + api_key},
            )
            public_status = public_response.status_code
            assert public_status == 200
            other_response = client.get(
                f"{url}/storage/v1/object/{other_bucket}/{other_path}", headers=scoped_headers
            )
            other_status = other_response.status_code
            assert other_status in (400, 401, 403, 404)
            anonymous = client.get(
                f"{url}/rest/v1/conversion_jobs",
                params={"select": "id", "id": f"eq.{job}"},
                headers={"apikey": api_key, "Authorization": "Bearer " + api_key},
            )
            anonymous_status = anonymous.status_code
            assert anonymous_status in (401, 403, 404) or (
                anonymous_status == 200 and anonymous.json() == []
            )
            expired = jwt.encode(
                {"role": "corpora_ingest", "exp": int(time.time()) - 3600},
                secret,
                algorithm="HS256",
            )
            expired_response = client.get(
                f"{url}/rest/v1/conversion_jobs",
                params={"select": "id"},
                headers={"apikey": api_key, "Authorization": "Bearer " + expired},
            )
            expired_status = expired_response.status_code
            assert expired_status in (401, 403)
            for bucket, path in (
                ("conversion-jobs", "other/denied.graph.json"),
                (other_bucket, f"conversion-jobs/{uuid4()}.graph.json"),
            ):
                denied = client.post(
                    f"{url}/storage/v1/object/{bucket}/{path}",
                    headers={**scoped_headers, "Content-Type": "application/json"},
                    content=b"{}",
                )
                assert denied.status_code in (400, 401, 403, 404)
            denied_delete = client.delete(
                f"{url}/storage/v1/object/conversion-jobs/conversion-jobs/{job}.graph.json",
                headers=scoped_headers,
            )
            assert denied_delete.status_code in (400, 401, 403, 404, 405)
            denied_patch = client.patch(
                f"{url}/rest/v1/conversion_jobs",
                params={"id": f"eq.{job}"},
                headers=scoped_headers,
                json={"status": "failed"},
            )
            assert denied_patch.status_code in (401, 403, 404)
        finally:
            cleanup_errors = []
            for bucket, path in uploaded:
                try:
                    response = client.delete(
                        f"{url}/storage/v1/object/{bucket}/{path}", headers=service_headers
                    )
                    if response.status_code not in (200, 204, 404):
                        cleanup_errors.append(f"object deletion returned {response.status_code}")
                except httpx.HTTPError:
                    cleanup_errors.append("object deletion request failed")
            for bucket in reversed(attempted_buckets):
                if bucket not in confirmed_buckets:
                    continue
                exists = db.execute(
                    "select exists(select 1 from storage.buckets where id=%s)", (bucket,)
                ).fetchone()[0]
                if not exists:
                    continue
                remaining = db.execute(
                    "select exists(select 1 from storage.objects where bucket_id=%s)", (bucket,)
                ).fetchone()[0]
                if remaining:
                    cleanup_errors.append("synthetic bucket still contains objects")
                    continue
                try:
                    response = client.delete(
                        f"{url}/storage/v1/bucket/{bucket}", headers=service_headers
                    )
                    if response.status_code not in (200, 204, 404):
                        cleanup_errors.append(f"bucket deletion returned {response.status_code}")
                except httpx.HTTPError:
                    cleanup_errors.append("bucket deletion request failed")
                if db.execute(
                    "select exists(select 1 from storage.buckets where id=%s)", (bucket,)
                ).fetchone()[0]:
                    cleanup_errors.append("synthetic bucket still exists")
            for name in created_test_policies + (role_policies if role_policies_installed else []):
                db.execute(
                    sql.SQL("drop policy {} on storage.objects").format(sql.Identifier(name))
                )
            db.execute("drop table if exists public.corpora_ingest_requests")
            db.execute("drop table if exists public.conversion_jobs")
            if db.execute(
                "select exists(select 1 from pg_roles where rolname='corpora_ingest')"
            ).fetchone()[0]:
                db.execute("revoke all on storage.objects, storage.buckets from corpora_ingest")
                db.execute("revoke usage on schema public, storage from corpora_ingest")
                db.execute("revoke corpora_ingest from authenticator")
                db.execute("drop role corpora_ingest")
            db.execute("notify pgrst, 'reload schema'")
            if db.execute(
                "select exists(select 1 from pg_roles where rolname='corpora_ingest')"
            ).fetchone()[0]:
                cleanup_errors.append("synthetic role still exists")
            if db.execute(
                "select policyname from pg_policies "
                "where schemaname='storage' and tablename='objects' and policyname = any(%s)",
                (role_policies + test_policies,),
            ).fetchall():
                cleanup_errors.append("synthetic policy still exists")
            if uncertain_bucket_write:
                cleanup_errors.append("bucket creation outcome uncertain; destroy test stack")
            if cleanup_errors:
                pytest.fail("; ".join(cleanup_errors))


@pytest.mark.parametrize(
    "kind", ["table", "column", "sequence", "function", "trigger_function", "schema"]
)
def test_restricted_role_fails_closed_on_unrelated_public_grants(kind):
    """Ambient privileges in another schema must prevent role creation."""
    psycopg = pytest.importorskip("psycopg")
    require_disposable_stack()
    dsn = os.environ["DB_URL"]
    assert urlparse(dsn).hostname in {"localhost", "127.0.0.1", "::1"}
    root = Path(__file__).resolve().parents[2]
    jobs_sql = Path(os.environ["CORPORA_TEST_JOBS_SCHEMA"]).read_text()
    reservations = next((root / "supabase/migrations").glob("*ingest_request_reservations.sql"))
    restricted = next((root / "supabase/migrations").glob("*corpora_ingest_restricted_role.sql"))
    with psycopg.connect(dsn, autocommit=True) as db:
        assert db.execute("select to_regclass('public.conversion_jobs') is null").fetchone()[0]
        assert db.execute(
            "select to_regclass('public.corpora_ingest_requests') is null"
        ).fetchone()[0]
        assert not db.execute(
            "select exists(select 1 from pg_roles where rolname='corpora_ingest')"
        ).fetchone()[0]
        assert not db.execute(
            "select exists(select 1 from pg_namespace where nspname='corpora_ingest_test_ambient')"
        ).fetchone()[0]
        names = [
            "corpora_ingest_select_objects",
            "corpora_ingest_insert_objects",
            "corpora_ingest_bound_objects_select",
            "corpora_ingest_bound_objects_insert",
        ]
        assert not db.execute(
            "select policyname from pg_policies where schemaname='storage' "
            "and tablename='objects' and policyname=any(%s)",
            (names,),
        ).fetchall()
        try:
            db.execute(jobs_sql)
            db.execute(reservations.read_text())
            db.execute("create schema corpora_ingest_test_ambient")
            db.execute("grant usage on schema corpora_ingest_test_ambient to public")
            if kind == "table":
                db.execute("create table corpora_ingest_test_ambient.probe (id integer)")
                db.execute("grant select on corpora_ingest_test_ambient.probe to public")
            elif kind == "column":
                db.execute("create table corpora_ingest_test_ambient.probe (id integer)")
                db.execute("grant select(id) on corpora_ingest_test_ambient.probe to public")
            elif kind == "sequence":
                db.execute("create sequence corpora_ingest_test_ambient.probe")
                db.execute("grant usage on sequence corpora_ingest_test_ambient.probe to public")
            elif kind == "function":
                db.execute(
                    "create function corpora_ingest_test_ambient.probe() returns integer "
                    "language sql security definer as 'select 1'"
                )
            elif kind == "trigger_function":
                db.execute(
                    "create function corpora_ingest_test_ambient.probe() returns trigger "
                    "language plpgsql security definer as 'begin return new; end'"
                )
            else:
                db.execute("grant create on schema corpora_ingest_test_ambient to public")
            with pytest.raises(psycopg.errors.RaiseException, match="unexpected"):
                db.execute(restricted.read_text())
            assert not db.execute(
                "select exists(select 1 from pg_roles where rolname='corpora_ingest')"
            ).fetchone()[0]
        finally:
            db.execute("drop schema if exists corpora_ingest_test_ambient cascade")
            db.execute("drop table if exists public.corpora_ingest_requests")
            db.execute("drop table if exists public.conversion_jobs")
            db.execute("notify pgrst, 'reload schema'")


@pytest.mark.parametrize(
    "privilege",
    [
        "UPDATE",
        "DELETE",
        "TRUNCATE",
        "REFERENCES",
        "TRIGGER",
        "UPDATE(name)",
        "REFERENCES(id)",
        "MAINTAIN",
    ],
)
def test_restricted_role_rejects_public_mutation_privileges(privilege):
    """PUBLIC must not grant extra permissions on the permitted job table."""
    psycopg = pytest.importorskip("psycopg")
    sql = pytest.importorskip("psycopg.sql")
    require_disposable_stack()
    dsn = os.environ["DB_URL"]
    assert urlparse(dsn).hostname in {"localhost", "127.0.0.1", "::1"}
    root = Path(__file__).resolve().parents[2]
    jobs_sql = Path(os.environ["CORPORA_TEST_JOBS_SCHEMA"]).read_text()
    reservations = next((root / "supabase/migrations").glob("*ingest_request_reservations.sql"))
    restricted = next((root / "supabase/migrations").glob("*corpora_ingest_restricted_role.sql"))
    with psycopg.connect(dsn, autocommit=True) as db:
        if (
            privilege == "MAINTAIN"
            and int(db.execute("show server_version_num").fetchone()[0]) < 170000
        ):
            pytest.skip("MAINTAIN is supported by PostgreSQL 17+")
        assert db.execute("select to_regclass('public.conversion_jobs') is null").fetchone()[0]
        assert db.execute(
            "select to_regclass('public.corpora_ingest_requests') is null"
        ).fetchone()[0]
        assert not db.execute(
            "select exists(select 1 from pg_roles where rolname='corpora_ingest')"
        ).fetchone()[0]
        names = [
            "corpora_ingest_select_objects",
            "corpora_ingest_insert_objects",
            "corpora_ingest_bound_objects_select",
            "corpora_ingest_bound_objects_insert",
        ]
        assert not db.execute(
            "select policyname from pg_policies where schemaname='storage' "
            "and tablename='objects' and policyname=any(%s)",
            (names,),
        ).fetchall()
        try:
            db.execute(jobs_sql)
            db.execute(reservations.read_text())
            db.execute(
                sql.SQL("grant {} on public.conversion_jobs to public").format(sql.SQL(privilege))
            )
            with pytest.raises(psycopg.errors.RaiseException, match="unexpected"):
                db.execute(restricted.read_text())
            assert not db.execute(
                "select exists(select 1 from pg_roles where rolname='corpora_ingest')"
            ).fetchone()[0]
        finally:
            db.execute("drop table if exists public.corpora_ingest_requests")
            db.execute("drop table if exists public.conversion_jobs")
            db.execute("notify pgrst, 'reload schema'")


@pytest.mark.parametrize("table", ["conversion_jobs", "corpora_ingest_requests"])
def test_restricted_role_rejects_missing_row_security(table):
    """The grant must fail if an ingestion table would bypass all policies."""
    psycopg = pytest.importorskip("psycopg")
    sql = pytest.importorskip("psycopg.sql")
    require_disposable_stack()
    dsn = os.environ["DB_URL"]
    assert urlparse(dsn).hostname in {"localhost", "127.0.0.1", "::1"}
    root = Path(__file__).resolve().parents[2]
    jobs_sql = Path(os.environ["CORPORA_TEST_JOBS_SCHEMA"]).read_text()
    reservations = next((root / "supabase/migrations").glob("*ingest_request_reservations.sql"))
    restricted = next((root / "supabase/migrations").glob("*corpora_ingest_restricted_role.sql"))
    with psycopg.connect(dsn, autocommit=True) as db:
        assert db.execute("select to_regclass('public.conversion_jobs') is null").fetchone()[0]
        assert db.execute(
            "select to_regclass('public.corpora_ingest_requests') is null"
        ).fetchone()[0]
        assert not db.execute(
            "select exists(select 1 from pg_roles where rolname='corpora_ingest')"
        ).fetchone()[0]
        names = [
            "corpora_ingest_select_objects",
            "corpora_ingest_insert_objects",
            "corpora_ingest_bound_objects_select",
            "corpora_ingest_bound_objects_insert",
        ]
        assert not db.execute(
            "select policyname from pg_policies where schemaname='storage' "
            "and tablename='objects' and policyname=any(%s)",
            (names,),
        ).fetchall()
        try:
            db.execute(jobs_sql)
            db.execute(reservations.read_text())
            db.execute(
                sql.SQL("alter table public.{} disable row level security").format(
                    sql.Identifier(table)
                )
            )
            with pytest.raises(psycopg.errors.RaiseException, match="row-level security"):
                db.execute(restricted.read_text())
            assert not db.execute(
                "select exists(select 1 from pg_roles where rolname='corpora_ingest')"
            ).fetchone()[0]
        finally:
            db.execute("drop table if exists public.corpora_ingest_requests")
            db.execute("drop table if exists public.conversion_jobs")
            db.execute("notify pgrst, 'reload schema'")
