"""Real local Auth, PostgREST, Storage and API coverage for session-bound views."""

from __future__ import annotations

import io
import json
import os
import time
import zipfile
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from tf.fabric import Fabric

from corpora_api import app as app_module
from corpora_api.drafts import DraftStore
from corpora_api.session_read import SessionBoundRead

pytestmark = pytest.mark.skipif(
    not os.getenv("CORPORA_TEST_LOCAL_SUPABASE"), reason="requires disposable local Supabase"
)


def test_views_use_real_supabase_session_and_private_storage(tmp_path, monkeypatch):
    psycopg = pytest.importorskip("psycopg")
    url, dsn = os.environ["API_URL"].rstrip("/"), os.environ["DB_URL"]
    assert urlparse(url).hostname in {"localhost", "127.0.0.1", "::1"}
    assert urlparse(dsn).hostname in {"localhost", "127.0.0.1", "::1"}
    anon, service = os.environ["ANON_KEY"], os.environ["SERVICE_ROLE_KEY"]
    monkeypatch.setenv("SUPABASE_URL", url)
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", service)
    monkeypatch.setenv("SUPABASE_JWKS_URL", url + "/auth/v1/.well-known/jwks.json")
    monkeypatch.setenv("SUPABASE_JWT_ISSUER", url + "/auth/v1")
    monkeypatch.setattr(app_module, "draft_store", DraftStore())
    monkeypatch.setattr(
        app_module,
        "configured_draft_lease",
        lambda: SessionBoundRead(
            lambda: psycopg.connect(dsn, autocommit=True), assume_service_role=True
        ),
    )
    root = tmp_path / "tf"
    root.mkdir()
    assert Fabric(locations=str(root), silent="deep").save(
        nodeFeatures={"otype": {1: "word", 2: "book"}, "book": {2: "Mini"}, "text": {1: "A"}},
        edgeFeatures={"oslots": {2: {1}}},
        metaData={
            "otype": {"valueType": "str"},
            "oslots": {"valueType": "str"},
            "book": {"valueType": "str"},
            "text": {"valueType": "str"},
            "otext": {
                "sectionTypes": "book",
                "sectionFeatures": "book",
                "fmt:text-orig-full": "{text}",
            },
        },
    )
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zipped:
        zipped.writestr("manifest.yml", "name: Mini\nversion: '1.0'\n")
        for path in root.rglob("*"):
            if path.is_file():
                zipped.write(path, f"corpora/{path.relative_to(root)}")
    data = archive.getvalue()
    service_headers = {"apikey": service, "Authorization": "Bearer " + service}
    users, bucket_created, object_key, grant_applied = [], False, None, False
    migration = (
        Path(__file__).resolve().parents[2]
        / "supabase/migrations/20261115165514_stored_session_reads.sql"
    ).read_text()
    lock_function = migration.split("CREATE FUNCTION public.corpora_stored_session_get", 1)[0]
    auth_dsn = psycopg.conninfo.make_conninfo(dsn, user="supabase_auth_admin")
    with (
        psycopg.connect(dsn, autocommit=True) as db,
        psycopg.connect(auth_dsn, autocommit=True) as auth_db,
        httpx.Client(timeout=20) as client,
    ):
        assert db.execute(
            "SELECT to_regclass('public.corpora_test_draft_heads') IS NULL"
        ).fetchone()[0]
        assert db.execute(
            "SELECT to_regprocedure('public.corpora_ai_storage(text,uuid,jsonb)') IS NULL"
        ).fetchone()[0]
        assert db.execute(
            "SELECT to_regprocedure('public.corpora_stored_lock_session(uuid,uuid,bigint)') IS NULL"
        ).fetchone()[0]
        try:
            privileges = (
                ("id", "SELECT"),
                ("user_id", "SELECT"),
                ("not_after", "SELECT"),
                ("not_after", "UPDATE"),
            )
            for column, privilege in privileges:
                assert not auth_db.execute(
                    "SELECT has_column_privilege('service_role','auth.sessions',%s,%s)",
                    (column, privilege),
                ).fetchone()[0]
            auth_db.execute(
                "GRANT SELECT(id,user_id,not_after), UPDATE(not_after) "
                "ON auth.sessions TO service_role"
            )
            grant_applied = True
            db.execute(lock_function)
            db.execute(
                "REVOKE ALL ON FUNCTION public.corpora_stored_lock_session(uuid,uuid,bigint) "
                "FROM PUBLIC, anon, authenticated"
            )
            db.execute(
                "GRANT EXECUTE ON FUNCTION public.corpora_stored_lock_session(uuid,uuid,bigint) "
                "TO service_role"
            )
            db.execute(
                "CREATE TABLE public.corpora_test_draft_heads("
                "owner uuid, document_id uuid, head jsonb)"
            )
            db.execute("ALTER TABLE public.corpora_test_draft_heads ENABLE ROW LEVEL SECURITY")
            db.execute(
                "REVOKE ALL ON public.corpora_test_draft_heads FROM PUBLIC, anon, authenticated"
            )
            db.execute("GRANT SELECT ON public.corpora_test_draft_heads TO service_role")
            db.execute("""
                CREATE FUNCTION public.corpora_ai_storage(
                    p_action text, p_owner uuid, p_payload jsonb)
                RETURNS jsonb LANGUAGE sql SECURITY INVOKER SET search_path='' AS $$
                    SELECT head FROM public.corpora_test_draft_heads
                    WHERE p_action='resolve' AND owner=p_owner
                      AND 'draft:' || document_id::text = p_payload->>'corpus'
                $$
            """)
            db.execute(
                "REVOKE ALL ON FUNCTION public.corpora_ai_storage(text,uuid,jsonb) "
                "FROM PUBLIC, anon, authenticated"
            )
            db.execute(
                "GRANT EXECUTE ON FUNCTION public.corpora_ai_storage(text,uuid,jsonb) "
                "TO service_role"
            )
            db.execute("NOTIFY pgrst, 'reload schema'")
            for _ in range(2):
                response = client.post(
                    url + "/auth/v1/signup",
                    headers={"apikey": anon},
                    json={"email": f"{uuid4()}@example.org", "password": str(uuid4())},
                )
                response.raise_for_status()
                users.append((response.json()["user"]["id"], response.json()["access_token"]))
            response = client.post(
                url + "/storage/v1/bucket",
                headers=service_headers,
                json={"id": "corpus-ai-drafts", "name": "corpus-ai-drafts", "public": False},
            )
            response.raise_for_status()
            bucket_created = True
            owner, document, revision = users[0][0], str(uuid4()), str(uuid4())
            object_key = f"{owner}/ai/{document}/{revision}.corpus"
            client.post(
                f"{url}/storage/v1/object/corpus-ai-drafts/{object_key}",
                headers=service_headers,
                content=data,
            ).raise_for_status()
            import hashlib

            head = {
                "owner": owner,
                "document_id": document,
                "revision": revision,
                "corpus": f"draft:{document}",
                "bucket": "corpus-ai-drafts",
                "object_key": object_key,
                "digest": "sha256:" + hashlib.sha256(data).hexdigest(),
                "version": "1.0",
            }
            db.execute(
                "INSERT INTO public.corpora_test_draft_heads VALUES (%s,%s,%s::jsonb)",
                (owner, document, json.dumps(head)),
            )
            time.sleep(1)
            with TestClient(app_module.app) as api:
                headers = {"Authorization": "Bearer " + users[0][1]}
                for view in ("manifest", "index", "versions", "nodes/1", "sections", "content"):
                    response = api.get(f"/v1/ai/drafts/{document}/{view}", headers=headers)
                    assert response.status_code == 200, (view, response.status_code, response.text)
                    assert response.json()["draft"]["id"] == document
                foreign = api.get(
                    f"/v1/ai/drafts/{document}/manifest",
                    headers={"Authorization": "Bearer " + users[1][1]},
                )
                assert foreign.status_code == 404
                response = client.post(url + "/auth/v1/logout", headers={"apikey": anon, **headers})
                assert response.status_code in (200, 204)
                for view in ("manifest", "index", "versions", "nodes/1", "sections", "content"):
                    assert (
                        api.get(f"/v1/ai/drafts/{document}/{view}", headers=headers).status_code
                        == 401
                    )
        finally:
            if object_key:
                client.request(
                    "DELETE",
                    url + "/storage/v1/object/corpus-ai-drafts",
                    headers=service_headers,
                    json={"prefixes": [object_key]},
                ).raise_for_status()
            if bucket_created:
                client.delete(
                    url + "/storage/v1/bucket/corpus-ai-drafts", headers=service_headers
                ).raise_for_status()
            for user, _token in users:
                client.delete(
                    url + "/auth/v1/admin/users/" + user, headers=service_headers
                ).raise_for_status()
            db.execute("DROP FUNCTION IF EXISTS public.corpora_ai_storage(text,uuid,jsonb)")
            db.execute("DROP TABLE IF EXISTS public.corpora_test_draft_heads")
            db.execute(
                "DROP FUNCTION IF EXISTS public.corpora_stored_lock_session(uuid,uuid,bigint)"
            )
            if grant_applied:
                auth_db.execute(
                    "REVOKE SELECT(id,user_id,not_after), UPDATE(not_after) "
                    "ON auth.sessions FROM service_role"
                )
                for column, privilege in privileges:
                    assert not auth_db.execute(
                        "SELECT has_column_privilege('service_role','auth.sessions',%s,%s)",
                        (column, privilege),
                    ).fetchone()[0]
            db.execute("NOTIFY pgrst, 'reload schema'")
