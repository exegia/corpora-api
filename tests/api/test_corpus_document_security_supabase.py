"""Real Auth, PostgREST and Storage checks for legacy corpus privacy."""

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


def test_document_and_bucket_access_are_owner_scoped():
    psycopg = pytest.importorskip("psycopg")
    url, dsn = os.environ["API_URL"].rstrip("/"), os.environ["DB_URL"]
    assert urlparse(url).hostname in {"localhost", "127.0.0.1", "::1"}
    assert urlparse(dsn).hostname in {"localhost", "127.0.0.1", "::1"}
    anon, service = os.environ["ANON_KEY"], os.environ["SERVICE_ROLE_KEY"]
    migrations = Path(__file__).resolve().parents[2] / "supabase/migrations"
    service_headers = {"apikey": service, "Authorization": "Bearer " + service}
    users = []
    bucket_created = False
    paths = [f"{uuid4()}/old.corpus", f"{uuid4()}/new.corpus"]
    policy_names = [
        "anon delete project corpora",
        "anon read project corpora",
        "anon update project corpora",
        "anon upload project corpora",
        "test_broad_storage",
        "corpora_private_objects_operation_guard",
        "corpora_private_objects_read",
        "corpora_private_objects_insert",
        "corpora_private_objects_update",
        "corpora_private_objects_delete",
        "corpora_private_objects_anon_guard",
        "corpora_private_objects_user_read_guard",
        "corpora_private_objects_user_insert_guard",
        "corpora_private_objects_user_update_guard",
        "corpora_private_objects_user_delete_guard",
    ]
    with psycopg.connect(dsn, autocommit=True) as db, httpx.Client(timeout=20) as client:
        assert db.execute("SELECT to_regnamespace('corpora_session_guard') IS NULL").fetchone()[0]
        try:
            db.execute(
                "CREATE TABLE public.projects(id uuid PRIMARY KEY,owner_id uuid,user_id uuid)"
            )
            db.execute("ALTER TABLE public.projects ENABLE ROW LEVEL SECURITY")
            db.execute("GRANT ALL ON public.projects TO anon,authenticated,service_role")
            db.execute("CREATE TABLE public.user_directory(id uuid PRIMARY KEY,email text)")
            db.execute("ALTER TABLE public.user_directory ENABLE ROW LEVEL SECURITY")
            db.execute("GRANT ALL ON public.user_directory TO anon,authenticated,service_role")
            db.execute(
                'CREATE POLICY "anon read user_directory" ON public.user_directory '
                "FOR SELECT TO public USING (true)"
            )
            for role in ("anon", "authenticated"):
                db.execute(
                    f'CREATE POLICY "{role} full access (temporary)" ON public.projects '
                    f"FOR ALL TO {role} USING (true) WITH CHECK (true)"
                )
            db.execute(next(migrations.glob("*_restricted_session_guard.sql")).read_text())
            db.execute(
                "CREATE TABLE corpora_session_guard.superadmins(user_id uuid PRIMARY KEY "
                "REFERENCES auth.users(id))"
            )
            db.execute(next(migrations.glob("*_project_ownership_guard.sql")).read_text())
            db.execute(
                "CREATE TABLE public.corpus_documents(id uuid PRIMARY KEY "
                "DEFAULT gen_random_uuid(),name text NOT NULL,path text NOT NULL)"
            )
            db.execute(
                "CREATE TABLE public.corpus_commits(id uuid PRIMARY KEY DEFAULT gen_random_uuid(),"
                "document_id uuid NOT NULL REFERENCES public.corpus_documents(id) "
                "ON DELETE CASCADE,sha text NOT NULL)"
            )
            db.execute(
                "CREATE TABLE public.corpus_changes(id uuid PRIMARY KEY DEFAULT gen_random_uuid(),"
                "document_id uuid NOT NULL REFERENCES public.corpus_documents(id) "
                "ON DELETE CASCADE,previous_value text)"
            )
            db.execute("CREATE TABLE public.licences(id text PRIMARY KEY,title text NOT NULL)")
            for table in ("corpus_documents", "corpus_commits", "corpus_changes", "licences"):
                db.execute(f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY")
                db.execute(f"GRANT ALL ON public.{table} TO anon,authenticated,service_role")
            for table, label in (
                ("corpus_documents", "corpus documents"),
                ("corpus_commits", "corpus commits"),
            ):
                for action, expression in (
                    ("read", "FOR SELECT USING (true)"),
                    ("insert", "FOR INSERT WITH CHECK (true)"),
                    ("delete", "FOR DELETE USING (true)"),
                ):
                    db.execute(
                        f'CREATE POLICY "anon {action} {label}" ON public.{table} {expression}'
                    )
            db.execute(
                'CREATE POLICY "anon insert licences" ON public.licences '
                "FOR INSERT WITH CHECK (true)"
            )
            db.execute(
                'CREATE POLICY "anon update licences" ON public.licences '
                "FOR UPDATE USING (true) WITH CHECK (true)"
            )
            db.execute(
                "CREATE POLICY licences_insert_authenticated ON public.licences "
                "FOR INSERT TO authenticated WITH CHECK (true)"
            )
            db.execute(
                "CREATE POLICY licences_select_public ON public.licences FOR SELECT USING (true)"
            )
            db.execute(
                "CREATE POLICY licences_update_authenticated ON public.licences "
                "FOR UPDATE TO authenticated USING (true) WITH CHECK (true)"
            )
            db.execute(
                "CREATE POLICY corpus_changes_select ON public.corpus_changes "
                "FOR SELECT TO authenticated USING (true)"
            )
            for action, sql in (
                ("read", "FOR SELECT USING (bucket_id='project-corpora')"),
                ("insert", "FOR INSERT WITH CHECK (bucket_id='project-corpora')"),
                (
                    "update",
                    "FOR UPDATE USING (bucket_id='project-corpora') "
                    "WITH CHECK (bucket_id='project-corpora')",
                ),
                ("delete", "FOR DELETE USING (bucket_id='project-corpora')"),
            ):
                name = "upload" if action == "insert" else action
                db.execute(f'CREATE POLICY "anon {name} project corpora" ON storage.objects {sql}')
            # An unrelated broad policy must not bypass the new restrictive guards.
            db.execute(
                "CREATE POLICY test_broad_storage ON storage.objects "
                "FOR ALL USING (true) WITH CHECK (true)"
            )
            for _ in range(3):
                response = client.post(
                    url + "/auth/v1/signup",
                    headers={"apikey": anon},
                    json={"email": f"{uuid4()}@example.org", "password": str(uuid4())},
                )
                response.raise_for_status()
                users.append((response.json()["user"]["id"], response.json()["access_token"]))
            owner, other, admin = users
            db.execute("INSERT INTO corpora_session_guard.superadmins VALUES (%s)", (admin[0],))
            created = client.post(
                url + "/storage/v1/bucket",
                headers=service_headers,
                json={"id": "project-corpora", "name": "project-corpora", "public": False},
            )
            created.raise_for_status()
            bucket_created = True

            def headers(user=None):
                result = {"apikey": anon, "Prefer": "return=representation"}
                if user:
                    result["Authorization"] = "Bearer " + user[1]
                return result

            def object_url(path):
                return url + "/storage/v1/object/project-corpora/" + path

            old = client.post(object_url(paths[0]), headers=headers(owner), content=b"old fixture")
            assert old.status_code in (200, 201), old.text
            unowned = str(uuid4())
            db.execute(
                "INSERT INTO public.corpus_documents(id,name,path) VALUES (%s,'unowned',%s)",
                (unowned, paths[0]),
            )
            db.execute(
                "INSERT INTO public.corpus_changes(document_id,previous_value) "
                "VALUES (%s,'private')",
                (unowned,),
            )
            db.execute("INSERT INTO public.licences VALUES ('test-catalogue','Test')")
            db.execute(next(migrations.glob("*_secure_corpus_documents.sql")).read_text())
            db.execute(next(migrations.glob("*_deny_revoked_storage_reads.sql")).read_text())
            deadline = time.monotonic() + 10
            docs = url + "/rest/v1/corpus_documents"
            while client.get(docs + "?select=owner_id", headers=headers(owner)).status_code != 200:
                assert time.monotonic() < deadline
                time.sleep(0.2)
            assert client.get(docs, headers=headers()).json() == []
            assert client.get(docs, headers=headers(owner)).json() == []
            assert client.get(docs, headers=headers(admin)).json()[0]["id"] == unowned
            changes = url + "/rest/v1/corpus_changes"
            assert client.get(changes, headers=headers()).status_code in (401, 403)
            assert client.get(changes, headers=headers(owner)).json() == []
            assert (
                client.get(changes, headers=headers(admin)).json()[0]["previous_value"] == "private"
            )
            payload = {"name": "owned", "path": paths[1]}
            assert client.post(docs, headers=headers(), json=payload).status_code in (401, 403)
            while True:
                forged = client.post(
                    docs, headers=headers(other), json={**payload, "owner_id": owner[0]}
                )
                if forged.status_code != 400 or "PGRST204" not in forged.text:
                    break
                assert time.monotonic() < deadline
                time.sleep(0.2)
            assert forged.status_code in (401, 403), forged.text
            document = client.post(docs, headers=headers(owner), json=payload)
            assert document.status_code == 201, document.text
            document_id = document.json()[0]["id"]
            assert document.json()[0]["owner_id"] == owner[0]
            db.execute(
                "INSERT INTO public.corpus_changes(document_id,previous_value) VALUES (%s,'owned')",
                (document_id,),
            )
            assert (
                client.get(changes, headers=headers(owner)).json()[0]["previous_value"] == "owned"
            )
            assert client.get(changes, headers=headers(other)).json() == []
            assert client.get(docs, headers=headers(other)).json() == []
            transfer = client.patch(
                docs + "?id=eq." + document_id, headers=headers(owner), json={"owner_id": other[0]}
            )
            assert transfer.status_code in (401, 403)
            commits = url + "/rest/v1/corpus_commits"
            commit = {"document_id": document_id, "sha": "fixture"}
            assert client.post(commits, headers=headers(other), json=commit).status_code in (
                401,
                403,
            )
            assert client.post(commits, headers=headers(owner), json=commit).status_code == 201
            assert client.get(commits, headers=headers(other)).json() == []
            licences = url + "/rest/v1/licences"
            assert client.get(licences, headers=headers()).json()[0]["id"] == "test-catalogue"
            assert client.post(
                licences, headers=headers(), json={"id": "bad", "title": "bad"}
            ).status_code in (401, 403)
            assert client.post(
                licences, headers=headers(owner), json={"id": "bad", "title": "bad"}
            ).status_code in (401, 403)
            assert (
                client.post(
                    licences, headers=headers(admin), json={"id": "admin", "title": "admin"}
                ).status_code
                == 201
            )
            update = client.patch(
                licences + "?id=eq.test-catalogue", headers=headers(owner), json={"title": "bad"}
            )
            assert update.status_code == 200 and update.json() == []
            update = client.patch(
                licences + "?id=eq.test-catalogue",
                headers=headers(admin),
                json={"title": "reviewed"},
            )
            assert update.status_code == 200 and update.json()[0]["title"] == "reviewed"
            assert client.get(object_url(paths[0]), headers=headers()).status_code != 200
            assert client.get(object_url(paths[0]), headers=headers(other)).status_code != 200
            assert (
                client.get(object_url(paths[0]), headers=headers(owner)).content == b"old fixture"
            )
            assert (
                client.get(object_url(paths[0]), headers=headers(admin)).content == b"old fixture"
            )
            assert client.post(
                object_url(paths[1]), headers=headers(), content=b"bad"
            ).status_code in (400, 401, 403)
            uploaded = client.post(
                object_url(paths[1]), headers=headers(owner), content=b"new fixture"
            )
            assert uploaded.status_code in (200, 201), uploaded.text
            updated = client.post(
                object_url(paths[1]),
                headers={**headers(owner), "x-upsert": "true"},
                content=b"updated",
            )
            assert updated.status_code in (200, 201), updated.text
            overwritten = client.post(
                object_url(paths[1]), headers={**headers(other), "x-upsert": "true"}, content=b"bad"
            )
            assert overwritten.status_code in (400, 401, 403)
            assert client.get(object_url(paths[1]), headers=headers(owner)).content == b"updated"
            assert client.delete(object_url(paths[1]), headers=headers(other)).status_code in (
                400,
                401,
                403,
                404,
            )
            issued = client.post(
                url + "/storage/v1/object/sign/project-corpora/" + paths[1],
                headers=headers(owner),
                json={"expiresIn": 60},
            )
            assert issued.status_code == 200, issued.text
            old_signed_url = url + "/storage/v1" + issued.json()["signedURL"]
            # Cached direct downloads do not recheck revoked sessions. Prevent
            # direct reads and URL signing while retaining owner uploads/deletes.
            with db.transaction():
                db.execute(next(migrations.glob("*_restrict_legacy_bucket_reads.sql")).read_text())
            assert client.get(object_url(paths[1]), headers=headers(owner)).status_code != 200
            assert client.get(object_url(paths[1]), headers=headers(admin)).status_code != 200
            assert client.get(object_url(paths[1]), headers=service_headers).content == b"updated"
            signed = client.post(
                url + "/storage/v1/object/sign/project-corpora/" + paths[1],
                headers=headers(owner),
                json={"expiresIn": 60},
            )
            assert signed.status_code != 200
            # Existing signed URLs are independent credentials and remain valid.
            # Their signing key or object path must be separately revoked.
            old_signed = client.get(old_signed_url)
            assert old_signed.status_code == 200
            replaced = client.post(
                object_url(paths[1]),
                headers={**headers(owner), "x-upsert": "true"},
                content=b"replacement",
            )
            assert replaced.status_code in (200, 201), replaced.text
            removed = client.delete(object_url(paths[1]), headers=headers(owner))
            assert removed.status_code in (200, 204), removed.text
            claimed = client.patch(
                docs + "?id=eq." + unowned, headers=headers(admin), json={"owner_id": owner[0]}
            )
            assert claimed.status_code == 200 and claimed.json()[0]["owner_id"] == owner[0]
            assert (
                client.post(url + "/auth/v1/logout?scope=local", headers=headers(owner)).status_code
                == 204
            )
            assert client.get(docs, headers=headers(owner)).status_code >= 400
            assert client.get(object_url(paths[1]), headers=headers(owner)).status_code in (
                400,
                401,
                403,
                404,
            )
            assert client.post(docs, headers=headers(owner), json=payload).status_code >= 400
        finally:
            db.execute("ROLLBACK")
            if bucket_created:
                for path in paths:
                    client.delete(object_url(path), headers=service_headers)
                client.delete(url + "/storage/v1/bucket/project-corpora", headers=service_headers)
            for name in policy_names:
                db.execute(f'DROP POLICY IF EXISTS "{name}" ON storage.objects')
            for table in (
                "corpus_changes",
                "corpus_commits",
                "corpus_documents",
                "licences",
                "projects",
                "user_directory",
            ):
                db.execute(f"DROP TABLE IF EXISTS public.{table} CASCADE")
            for function in (
                "corpora_legacy_document_editable(uuid)",
                "corpora_legacy_superadmin_readable()",
                "corpora_legacy_storage_session_readable()",
                "corpora_directory_reader()",
                "corpora_project_superadmin()",
                "corpora_project_actor()",
            ):
                db.execute(f"DROP FUNCTION IF EXISTS public.{function} CASCADE")
            db.execute("DROP SCHEMA IF EXISTS corpora_session_guard CASCADE")
            for user, _ in users:
                client.delete(url + "/auth/v1/admin/users/" + user, headers=service_headers)
