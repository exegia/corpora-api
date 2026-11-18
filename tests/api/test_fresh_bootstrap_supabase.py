"""Opt-in smoke test for a fresh, disposable native Supabase bootstrap."""

import json
import os
import subprocess
from pathlib import Path
from runpy import run_path
from urllib.parse import urlparse
from uuid import UUID, uuid4

import httpx
import pytest

pytestmark = pytest.mark.skipif(
    os.getenv("CORPORA_TEST_FRESH_BOOTSTRAP") != "1",
    reason="requires a dedicated fresh local Supabase bootstrap",
)


def test_fresh_bootstrap_auth_and_private_storage():
    psycopg = pytest.importorskip("psycopg")
    workdir = Path(os.environ["CORPORA_TEST_FRESH_WORKDIR"]).resolve(strict=True)
    result = subprocess.run(
        ["supabase", "status", "--workdir", str(workdir), "--output-format", "json"],
        check=True,
        capture_output=True,
        text=True,
    )
    env = json.loads(result.stdout)["env"]
    url, dsn = env["API_URL"].rstrip("/"), env["DB_URL"]
    assert urlparse(url).hostname in {"localhost", "127.0.0.1", "::1"}
    assert urlparse(dsn).hostname in {"localhost", "127.0.0.1", "::1"}
    initialize = run_path(
        str(Path(__file__).resolve().parents[2] / "scripts/initialize_fresh_supabase.py")
    )["initialize"]
    initialize(workdir)
    anon, service = env["ANON_KEY"], env["SERVICE_ROLE_KEY"]
    admin_headers = {
        "apikey": service,
        "Authorization": "Bearer " + service,
        "Prefer": "return=representation",
    }
    anonymous = {
        "apikey": anon,
        "Authorization": "Bearer " + anon,
        "Prefer": "return=representation",
    }
    project_id, directory_id, document_id = str(uuid4()), str(uuid4()), str(uuid4())
    users: list[tuple[str, dict[str, str]]] = []
    objects: list[tuple[str, str]] = []
    with (
        psycopg.connect(dsn, autocommit=True) as db,
        httpx.Client(timeout=20, trust_env=False) as client,
    ):
        assert db.execute("SELECT count(*) FROM auth.users").fetchone()[0] == 0
        assert (
            db.execute(
                "SELECT count(*) FROM supabase_migrations.schema_migrations "
                "WHERE version='20261118100059'"
            ).fetchone()[0]
            == 1
        )
        baseline = (
            Path(__file__).resolve().parents[2]
            / "supabase/bootstrap/20261118100059_fresh_baseline.sql"
        ).read_text()
        with pytest.raises(
            psycopg.errors.RaiseException, match="FRESH_BOOTSTRAP_REQUIRES_EMPTY_PROJECT"
        ):
            db.execute(baseline, prepare=False)
        db.execute("ROLLBACK")
        assert db.execute("SELECT to_regclass('public.projects') IS NOT NULL").fetchone()[0]
        try:
            for name in ("owner", "member", "outsider"):
                email = f"{name}-{uuid4()}@example.invalid"
                password = str(uuid4()) + "Aa9!"
                response = client.post(
                    url + "/auth/v1/admin/users",
                    headers=admin_headers,
                    json={"email": email, "password": password, "email_confirm": True},
                )
                assert response.status_code in (200, 201), response.text
                user_id = response.json()["id"]
                response = client.post(
                    url + "/auth/v1/token?grant_type=password",
                    headers={"apikey": anon},
                    json={"email": email, "password": password},
                )
                assert response.status_code == 200, response.text
                users.append(
                    (
                        user_id,
                        {
                            "apikey": anon,
                            "Authorization": "Bearer " + response.json()["access_token"],
                            "Prefer": "return=representation",
                        },
                    )
                )
                assert (
                    db.execute(
                        "SELECT count(*) FROM public.users WHERE id=%s", (user_id,)
                    ).fetchone()[0]
                    == 1
                )
            owner, member, outsider = users
            initialize(workdir, UUID(owner[0]))
            initialize(workdir, UUID(owner[0]))  # Safe to repeat.
            db.execute(
                "INSERT INTO public.user_directory(id,username,email) VALUES (%s,%s,%s)",
                (directory_id, "test-" + str(uuid4()), f"{uuid4()}@example.invalid"),
            )
            db.execute(
                "INSERT INTO public.licences(id,title) "
                "VALUES ('BOOTSTRAP-TEST','Temporary licence')"
            )
            rpc = url + "/rest/v1/rpc/corpora_project_superadmin"
            assert client.post(rpc, headers=owner[1], json={}).json() is True
            assert client.post(rpc, headers=member[1], json={}).json() is False
            assert client.post(rpc, headers=anonymous, json={}).status_code in (401, 403, 404)
            assert not db.execute(
                "SELECT has_function_privilege('anon',"
                "'public.corpora_project_superadmin()','EXECUTE')"
            ).fetchone()[0]
            assert not db.execute(
                "SELECT has_function_privilege('anon','public.rls_auto_enable()','EXECUTE')"
            ).fetchone()[0]
            response = client.post(
                url + "/rest/v1/projects",
                headers=owner[1],
                json={"id": project_id, "name": "Temporary project", "user_id": directory_id},
            )
            assert response.status_code == 201, response.text
            assert response.json()[0]["owner_id"] == owner[0]
            lookup = url + "/rest/v1/rpc/corpora_project_document_id"
            assert (
                client.post(lookup, headers=member[1], json={"p_project": project_id}).json()
                is None
            )
            db.execute(
                "INSERT INTO public.corpus_documents(id,name,source,path,owner_id) "
                "VALUES (%s,'Temporary document','upload','temporary',%s)",
                (document_id, member[0]),
            )
            db.execute(
                "UPDATE public.projects SET corpus_document_id=%s WHERE id=%s",
                (document_id, project_id),
            )
            for headers in (owner[1], member[1]):
                response = client.post(lookup, headers=headers, json={"p_project": project_id})
                assert response.status_code == 200, response.text
                assert response.json() == document_id
            assert (
                client.post(lookup, headers=outsider[1], json={"p_project": project_id}).json()
                is None
            )
            assert client.post(
                lookup, headers=anonymous, json={"p_project": project_id}
            ).status_code in (401, 403, 404)
            response = client.post(
                url + "/rest/v1/licences",
                headers=member[1],
                json={"id": "denied", "title": "Denied"},
            )
            assert response.status_code in (401, 403)
            response = client.post(
                url + "/rest/v1/organizations", headers=anonymous, json={"name": "denied"}
            )
            assert response.status_code in (401, 403)
            path = str(uuid4()) + "/fixture.corpus"
            objects.append(("project-corpora", path))
            object_url = url + "/storage/v1/object/project-corpora/" + path
            response = client.post(object_url, headers=owner[1], content=b"fixture")
            assert response.status_code in (200, 201), response.text
            assert client.get(object_url, headers=owner[1]).status_code in (400, 401, 403, 404)
            assert client.get(object_url, headers=admin_headers).content == b"fixture"
            path = f"conversion-jobs/{owner[0]}/{uuid4()}.graph.json"
            objects.append(("conversion-jobs", path))
            ingest_url = url + "/storage/v1/object/conversion-jobs/" + path
            response = client.post(ingest_url, headers=owner[1], content=b"{}")
            assert response.status_code in (200, 201), response.text
            assert client.get(ingest_url, headers=owner[1]).status_code == 200
            assert (
                client.post(url + "/auth/v1/logout?scope=local", headers=owner[1]).status_code
                == 204
            )
            assert client.post(rpc, headers=owner[1], json={}).status_code >= 400
            assert (
                client.post(lookup, headers=owner[1], json={"p_project": project_id}).status_code
                >= 400
            )
            assert client.get(ingest_url, headers=owner[1]).status_code >= 400
        finally:
            for bucket, path in objects:
                client.delete(url + f"/storage/v1/object/{bucket}/" + path, headers=admin_headers)
            db.execute("DELETE FROM public.projects WHERE id=%s", (project_id,))
            db.execute("DELETE FROM public.corpus_documents WHERE id=%s", (document_id,))
            if users:
                db.execute(
                    "DELETE FROM corpora_session_guard.superadmins WHERE user_id=%s", (users[0][0],)
                )
            db.execute("DELETE FROM public.user_directory WHERE id=%s", (directory_id,))
            db.execute("DELETE FROM public.licences WHERE id='BOOTSTRAP-TEST'")
            for user_id, _ in users:
                client.delete(url + "/auth/v1/admin/users/" + user_id, headers=admin_headers)
