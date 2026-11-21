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
    project_id, other_project, directory_id, document_id = (str(uuid4()) for _ in range(4))
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
                params={"select": "id,owner_id"},
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
            for headers in (anonymous, owner[1], outsider[1]):
                assert (
                    client.get(
                        url + "/rest/v1/projects", headers=headers, params={"select": "id,name"}
                    ).status_code
                    == 200
                )
                assert client.get(
                    url + "/rest/v1/projects",
                    headers=headers,
                    params={"select": "id,corpus_document_id"},
                ).status_code in (401, 403)
            project_detail = (
                "id,name,description,status,type,created_at,updated_at,owner_id,language,category,"
                "user_directory(id,name,username),organizations(id,name,website),"
                "project_licences(agreed_at,licences(id,title,url,domain_content,domain_data,"
                "domain_software,family,maintainer,status),user_directory(id,name,username))"
            )
            response = client.get(
                url + "/rest/v1/projects",
                headers=owner[1],
                params={"id": "eq." + project_id, "select": project_detail},
            )
            assert response.status_code == 200, response.text
            response = client.get(
                url + "/rest/v1/projects",
                headers=anonymous,
                params={"id": "eq." + project_id, "select": project_detail},
            )
            assert response.status_code == 200, response.text
            assert response.json()[0]["user_directory"] is None
            assert (
                client.get(
                    url + "/rest/v1/user_directory",
                    headers=anonymous,
                    params={"select": "id,name,username"},
                ).json()
                == []
            )
            response = client.post(
                url + "/rest/v1/projects",
                headers=outsider[1],
                params={"select": "id"},
                json={"id": other_project, "name": "Other project", "user_id": directory_id},
            )
            assert response.status_code == 201, response.text
            for value in (document_id, str(uuid4())):
                response = client.patch(
                    url + "/rest/v1/projects",
                    headers=outsider[1],
                    params={"id": "eq." + other_project, "select": "id"},
                    json={"corpus_document_id": value},
                )
                assert response.status_code >= 400 and "DOCUMENT_UNAVAILABLE" in response.text
            db.execute(
                "UPDATE public.corpus_documents SET owner_id=%s WHERE id=%s",
                (outsider[0], document_id),
            )
            response = client.patch(
                url + "/rest/v1/projects",
                headers=outsider[1],
                params={"id": "eq." + other_project, "select": "id"},
                json={"corpus_document_id": document_id},
            )
            assert response.status_code == 200, response.text
            db.execute(
                "UPDATE public.corpus_documents SET owner_id=%s WHERE id=%s",
                (member[0], document_id),
            )
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
            db.execute("DELETE FROM public.projects WHERE id=%s", (other_project,))
            db.execute("DELETE FROM public.corpus_documents WHERE id=%s", (document_id,))
            if users:
                db.execute(
                    "DELETE FROM corpora_session_guard.superadmins WHERE user_id=%s", (users[0][0],)
                )
            db.execute("DELETE FROM public.user_directory WHERE id=%s", (directory_id,))
            db.execute("DELETE FROM public.licences WHERE id='BOOTSTRAP-TEST'")
            for user_id, _ in users:
                client.delete(url + "/auth/v1/admin/users/" + user_id, headers=admin_headers)


def _sign_in(client: httpx.Client, url: str, anon: str, email: str, password: str):
    response = client.post(
        url + "/auth/v1/token?grant_type=password",
        headers={"apikey": anon},
        json={"email": email, "password": password},
    )
    assert response.status_code == 200, response.text
    return {
        "apikey": anon,
        "Authorization": "Bearer " + response.json()["access_token"],
        "Prefer": "return=representation",
    }


def test_fresh_bootstrap_web_role_and_licence_authorization():
    """Exercise the Web's role check and licence writes for every kind of caller.

    The requests mirror corpora-web's Supabase calls: the corpora_project_superadmin
    RPC, licence catalogue create/update/text save, and project licence attach,
    agree and detach. Signed out means a revoked session token; anonymous means
    the publishable key alone, which is what the Web sends after sign-out.
    """
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
    anon, service = env["ANON_KEY"], env["SERVICE_ROLE_KEY"]
    admin_headers = {"apikey": service, "Authorization": "Bearer " + service}
    anonymous = {"apikey": anon, "Prefer": "return=representation"}
    suffix = uuid4().hex[:12].upper()
    licence, created = "BOOTSTRAP-ROLE-" + suffix, "BOOTSTRAP-NEW-" + suffix
    owner_project, admin_project, directory_id = (str(uuid4()) for _ in range(3))
    users: list[str] = []
    with (
        psycopg.connect(dsn, autocommit=True) as db,
        httpx.Client(timeout=20, trust_env=False) as client,
    ):
        try:
            sessions = {}
            for name in ("superadmin", "user"):
                email = f"{name}-{uuid4()}@example.invalid"
                password = str(uuid4()) + "Aa9!"
                response = client.post(
                    url + "/auth/v1/admin/users",
                    headers=admin_headers,
                    json={"email": email, "password": password, "email_confirm": True},
                )
                assert response.status_code in (200, 201), response.text
                users.append(response.json()["id"])
                sessions[name] = _sign_in(client, url, anon, email, password)
                if name == "superadmin":
                    stale = _sign_in(client, url, anon, email, password)
            superadmin, user = sessions["superadmin"], sessions["user"]
            rpc = url + "/rest/v1/rpc/corpora_project_superadmin"
            # No role exists until it is assigned explicitly.
            assert client.post(rpc, headers=superadmin, json={}).json() is False
            initialize(workdir, UUID(users[0]))
            assert (
                client.post(url + "/auth/v1/logout?scope=local", headers=stale).status_code == 204
            )
            signed_out = {**stale, "Prefer": "return=representation"}
            db.execute(
                "INSERT INTO public.user_directory(id,username,email) VALUES (%s,%s,%s)",
                (directory_id, "test-" + str(uuid4()), f"{uuid4()}@example.invalid"),
            )
            db.execute(
                "INSERT INTO public.licences(id,title) VALUES (%s,'Temporary licence')",
                (licence,),
            )

            # Role check, as read by the Web's isCurrentSuperadmin().
            assert client.post(rpc, headers=superadmin, json={}).json() is True
            assert client.post(rpc, headers=user, json={}).json() is False
            for headers in (anonymous, signed_out):
                assert client.post(rpc, headers=headers, json={}).status_code >= 400

            # Catalogue reads.
            catalogue = url + "/rest/v1/licences"
            for headers in (superadmin, user, anonymous):
                response = client.get(
                    catalogue, headers=headers, params={"id": "eq." + licence, "select": "id"}
                )
                assert response.status_code == 200, response.text
                assert response.json() == [{"id": licence}]
            assert client.get(catalogue, headers=signed_out).status_code >= 400

            # Catalogue creation is superadmin-only.
            for headers in (user, anonymous, signed_out):
                response = client.post(
                    catalogue, headers=headers, json={"id": created, "title": "Denied"}
                )
                assert response.status_code >= 400, response.text
            response = client.post(
                catalogue,
                headers=superadmin,
                params={"select": "id"},
                json={"id": created, "title": "Created by the superadmin"},
            )
            assert response.status_code == 201, response.text

            # Catalogue edits and licence text saves are superadmin-only.
            item = {"id": "eq." + licence, "select": "id"}
            for change in ({"title": "Changed"}, {"full_text": "Changed text"}):
                response = client.patch(catalogue, headers=user, params=item, json=change)
                assert response.status_code == 200 and response.json() == [], response.text
                for headers in (anonymous, signed_out):
                    assert (
                        client.patch(
                            catalogue, headers=headers, params=item, json=change
                        ).status_code
                        >= 400
                    )
                assert db.execute(
                    "SELECT title='Temporary licence' AND full_text IS NULL "
                    "FROM public.licences WHERE id=%s",
                    (licence,),
                ).fetchone()[0]
            for change in ({"title": "Edited"}, {"full_text": "Edited text"}):
                response = client.patch(catalogue, headers=superadmin, params=item, json=change)
                assert response.status_code == 200 and response.json() == [{"id": licence}]

            # Project licences: the owner and the superadmin may attach, agree and detach.
            projects = url + "/rest/v1/projects"
            for project, headers in ((owner_project, user), (admin_project, superadmin)):
                response = client.post(
                    projects,
                    headers=headers,
                    params={"select": "id"},
                    json={"id": project, "name": "Temporary project", "user_id": directory_id},
                )
                assert response.status_code == 201, response.text
            attachments = url + "/rest/v1/project_licences"
            attach = {"licence_id": licence, "agreed_by_user_id": directory_id}
            for headers in (user, anonymous, signed_out):
                response = client.post(
                    attachments, headers=headers, json={"project_id": admin_project, **attach}
                )
                assert response.status_code >= 400, response.text
            for project in (owner_project, admin_project):
                headers = user if project == owner_project else superadmin
                response = client.post(
                    attachments, headers=headers, json={"project_id": project, **attach}
                )
                assert response.status_code == 201, response.text
            response = client.post(
                attachments, headers=superadmin, json={"project_id": owner_project, **attach}
            )
            assert response.status_code == 409  # The Web reports "already attached".
            pair = {
                "project_id": "eq." + admin_project,
                "licence_id": "eq." + licence,
                "select": "licence_id",
            }
            agreement = {"agreed_by_user_id": directory_id, "agreed_at": "2026-01-01T00:00:00Z"}
            response = client.patch(attachments, headers=user, params=pair, json=agreement)
            assert response.status_code == 200 and response.json() == []
            response = client.delete(attachments, headers=user, params=pair)
            assert response.status_code == 200 and response.json() == []
            for headers in (anonymous, signed_out):
                assert (
                    client.patch(
                        attachments, headers=headers, params=pair, json=agreement
                    ).status_code
                    >= 400
                )
                assert client.delete(attachments, headers=headers, params=pair).status_code >= 400
            response = client.patch(attachments, headers=superadmin, params=pair, json=agreement)
            assert response.json() == [{"licence_id": licence}]
            own = {**pair, "project_id": "eq." + owner_project}
            response = client.patch(attachments, headers=user, params=own, json=agreement)
            assert response.json() == [{"licence_id": licence}]
            # Anonymous readers see the attachment but not the agreeing directory user.
            response = client.get(
                projects,
                headers=anonymous,
                params={
                    "id": "eq." + owner_project,
                    "select": "id,project_licences(licences(id),user_directory(id))",
                },
            )
            assert response.status_code == 200, response.text
            assert response.json()[0]["project_licences"] == [
                {"licences": {"id": licence}, "user_directory": None}
            ]
            response = client.delete(attachments, headers=user, params=own)
            assert response.json() == [{"licence_id": licence}]
            response = client.delete(attachments, headers=superadmin, params=pair)
            assert response.json() == [{"licence_id": licence}]
        finally:
            db.execute(
                "DELETE FROM public.projects WHERE id = ANY(%s)",
                ([owner_project, admin_project],),
            )
            db.execute("DELETE FROM public.licences WHERE id = ANY(%s)", ([licence, created],))
            db.execute("DELETE FROM public.user_directory WHERE id=%s", (directory_id,))
            if users:
                db.execute(
                    "DELETE FROM corpora_session_guard.superadmins WHERE user_id=%s", (users[0],)
                )
            for user_id in users:
                client.delete(url + "/auth/v1/admin/users/" + user_id, headers=admin_headers)
