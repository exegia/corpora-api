"""Project ownership and session revocation against a disposable local Supabase."""

import os
import time
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

import httpx
import pytest

pytestmark = pytest.mark.skipif(
    not os.getenv("CORPORA_TEST_LOCAL_SUPABASE"), reason="requires disposable local Supabase"
)


def test_project_owner_is_authenticated_and_cannot_be_forged():
    psycopg = pytest.importorskip("psycopg")
    url, dsn = os.environ["API_URL"].rstrip("/"), os.environ["DB_URL"]
    assert urlparse(url).hostname in {"localhost", "127.0.0.1", "::1"}
    assert urlparse(dsn).hostname in {"localhost", "127.0.0.1", "::1"}
    anon, service = os.environ["ANON_KEY"], os.environ["SERVICE_ROLE_KEY"]
    migrations = Path(__file__).resolve().parents[2] / "supabase/migrations"
    users = []
    with psycopg.connect(dsn, autocommit=True) as db, httpx.Client(timeout=15) as client:
        assert db.execute("SELECT to_regclass('public.projects') IS NULL").fetchone()[0]
        assert db.execute("SELECT to_regclass('public.user_directory') IS NULL").fetchone()[0]
        assert db.execute("SELECT to_regnamespace('corpora_session_guard') IS NULL").fetchone()[0]
        try:
            db.execute(
                "CREATE TABLE public.projects(id uuid PRIMARY KEY DEFAULT gen_random_uuid(), "
                "name text NOT NULL, owner_id uuid, user_id uuid NOT NULL)"
            )
            db.execute("ALTER TABLE public.projects ENABLE ROW LEVEL SECURITY")
            db.execute("GRANT ALL ON public.projects TO anon,authenticated,service_role")
            db.execute(
                "CREATE TABLE public.user_directory(id uuid PRIMARY KEY, email text NOT NULL)"
            )
            db.execute("ALTER TABLE public.user_directory ENABLE ROW LEVEL SECURITY")
            db.execute("GRANT ALL ON public.user_directory TO anon,authenticated,service_role")
            db.execute(
                'CREATE POLICY "anon read user_directory" ON public.user_directory '
                "FOR SELECT TO public USING (true)"
            )
            directory_id, unassigned = str(uuid4()), str(uuid4())
            db.execute(
                "INSERT INTO public.user_directory VALUES (%s,%s)",
                (directory_id, "test@example.org"),
            )
            db.execute(
                "INSERT INTO public.projects(id,name,owner_id,user_id) VALUES (%s,%s,NULL,%s)",
                (unassigned, "unassigned", directory_id),
            )
            db.execute(
                'CREATE POLICY "anon full access (temporary)" ON public.projects '
                "FOR ALL TO anon USING (true) WITH CHECK (true)"
            )
            db.execute(
                'CREATE POLICY "authenticated full access (temporary)" ON public.projects '
                "FOR ALL TO authenticated USING (true) WITH CHECK (true)"
            )
            db.execute(next(migrations.glob("*_restricted_session_guard.sql")).read_text())
            db.execute(
                "CREATE TABLE corpora_session_guard.superadmins(user_id uuid PRIMARY KEY "
                "REFERENCES auth.users(id))"
            )
            migration = next(migrations.glob("*_project_ownership_guard.sql")).read_text()
            db.execute("UPDATE public.projects SET owner_id=%s WHERE id=%s", (uuid4(), unassigned))
            with pytest.raises(Exception, match="PROJECT_EXISTING_OWNERS_REQUIRE_REVIEW"):
                db.execute(migration)
            db.execute("ROLLBACK")
            db.execute("UPDATE public.projects SET owner_id=NULL WHERE id=%s", (unassigned,))
            db.execute(migration)
            for _ in range(3):
                response = client.post(
                    url + "/auth/v1/signup",
                    headers={"apikey": anon},
                    json={"email": f"{uuid4()}@example.org", "password": str(uuid4())},
                )
                assert response.status_code == 200
                users.append((response.json()["user"]["id"], response.json()["access_token"]))
            owner, other, admin = users
            db.execute("INSERT INTO corpora_session_guard.superadmins VALUES (%s)", (admin[0],))
            for role in ("anon", "authenticated"):
                for privilege in ("TRUNCATE", "REFERENCES", "TRIGGER"):
                    assert not db.execute(
                        "SELECT has_table_privilege(%s,%s,%s)", (role, "public.projects", privilege)
                    ).fetchone()[0]
                    assert not db.execute(
                        "SELECT has_table_privilege(%s,%s,%s)",
                        (role, "public.user_directory", privilege),
                    ).fetchone()[0]
            for function in (
                "public.corpora_project_actor()",
                "public.corpora_project_superadmin()",
                "public.corpora_directory_reader()",
            ):
                assert not db.execute(
                    "SELECT has_function_privilege('anon',%s,'EXECUTE')", (function,)
                ).fetchone()[0]
                assert db.execute(
                    "SELECT has_function_privilege('authenticated',%s,'EXECUTE')", (function,)
                ).fetchone()[0]

            def headers(user=None):
                result = {"apikey": anon, "Prefer": "return=representation"}
                if user is not None:
                    result["Authorization"] = "Bearer " + user[1]
                return result

            path = url + "/rest/v1/projects"
            project_id = str(uuid4())
            directory = url + "/rest/v1/user_directory"
            deadline = time.monotonic() + 10
            while True:
                directory_response = client.get(directory, headers=headers(owner))
                status = directory_response.status_code
                if status == 200 or time.monotonic() >= deadline:
                    break
                time.sleep(0.2)
            assert status == 200, directory_response.text
            assert client.get(directory, headers=headers()).status_code in (401, 403)
            assert client.patch(
                directory + "?id=eq." + directory_id,
                headers=headers(owner),
                json={"email": "changed@example.org"},
            ).status_code in (401, 403)
            item = {"id": project_id, "name": "owner test", "user_id": directory_id}
            assert client.post(path, headers=headers(), json=item).status_code in (401, 403)
            created = client.post(path, headers=headers(owner), json=item)
            assert created.status_code == 201, created.text
            assert created.json()[0]["owner_id"] == owner[0]
            forged = client.post(
                path,
                headers=headers(other),
                json={**item, "id": str(uuid4()), "owner_id": owner[0]},
            )
            assert forged.status_code in (401, 403)
            target = path + "?id=eq." + project_id
            assert (
                client.patch(target, headers=headers(other), json={"name": "foreign"}).json() == []
            )
            transfer = client.patch(target, headers=headers(owner), json={"owner_id": other[0]})
            assert transfer.status_code in (401, 403)
            changed = client.patch(target, headers=headers(owner), json={"name": "owner changed"})
            assert changed.status_code == 200 and changed.json()[0]["name"] == "owner changed"
            claim_url = path + "?id=eq." + unassigned
            assert (
                client.patch(claim_url, headers=headers(other), json={"owner_id": other[0]}).json()
                == []
            )
            claimed = client.patch(claim_url, headers=headers(admin), json={"owner_id": other[0]})
            assert claimed.status_code == 200 and claimed.json()[0]["owner_id"] == other[0]
            assert (
                client.post(url + "/auth/v1/logout?scope=local", headers=headers(owner)).status_code
                == 204
            )
            revoked = client.patch(target, headers=headers(owner), json={"name": "revoked"})
            assert revoked.status_code >= 400
            assert client.get(directory, headers=headers(owner)).status_code >= 400
            assert (
                db.execute(
                    "SELECT name FROM public.projects WHERE id=%s", (project_id,)
                ).fetchone()[0]
                == "owner changed"
            )
        finally:
            db.execute("DROP TABLE IF EXISTS public.projects CASCADE")
            db.execute("DROP TABLE IF EXISTS public.user_directory CASCADE")
            db.execute("DROP FUNCTION IF EXISTS public.corpora_directory_reader() CASCADE")
            db.execute("DROP FUNCTION IF EXISTS public.corpora_project_superadmin() CASCADE")
            db.execute("DROP FUNCTION IF EXISTS public.corpora_project_actor() CASCADE")
            db.execute("DROP SCHEMA IF EXISTS corpora_session_guard CASCADE")
            for user_id, _ in users:
                client.delete(
                    url + "/auth/v1/admin/users/" + user_id,
                    headers={"apikey": service, "Authorization": "Bearer " + service},
                )
