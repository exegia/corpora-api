"""Opt-in ingestion test against disposable local Supabase services."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from urllib.parse import quote, urlparse
from uuid import UUID, uuid4

import httpx
import pytest
from corpora_schema_types import validate_graph
from fastapi.testclient import TestClient

from corpora_api import app as app_module
from corpora_api.auth import verify_user_token
from corpora_api.ingest_store import IngestStore

pytestmark = pytest.mark.skipif(
    not os.getenv("CORPORA_TEST_LOCAL_SUPABASE"), reason="requires disposable local Supabase"
)


def test_ingest_persists_graph_across_processes_and_isolates_users(monkeypatch):
    psycopg = pytest.importorskip("psycopg")
    url, dsn = os.environ["API_URL"].rstrip("/"), os.environ["DB_URL"]
    assert urlparse(url).hostname in {"localhost", "127.0.0.1", "::1"}
    assert urlparse(dsn).hostname in {"localhost", "127.0.0.1", "::1"}
    anon, service = os.environ["ANON_KEY"], os.environ["SERVICE_ROLE_KEY"]
    bucket = "ingest-test-" + uuid4().hex
    monkeypatch.setenv("SUPABASE_URL", url)
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", service)
    monkeypatch.setenv("SUPABASE_JOBS_BUCKET", bucket)
    monkeypatch.setenv("SUPABASE_JWKS_URL", url + "/auth/v1/.well-known/jwks.json")
    monkeypatch.setenv("SUPABASE_JWT_ISSUER", url + "/auth/v1")
    monkeypatch.setattr(app_module, "ingest_store", IngestStore())
    service_headers = {"apikey": service, "Authorization": "Bearer " + service}
    users, job = [], None
    bucket_created = False
    with httpx.Client(timeout=15) as client, psycopg.connect(dsn, autocommit=True) as db:
        assert db.execute("SELECT to_regclass('public.conversion_jobs') IS NOT NULL").fetchone()[0]
        try:
            for _ in range(2):
                response = client.post(
                    url + "/auth/v1/signup",
                    headers={"apikey": anon, "Authorization": "Bearer " + anon},
                    json={"email": f"{uuid4()}@example.org", "password": str(uuid4())},
                )
                response.raise_for_status()
                token = response.json()["access_token"]
                users.append((verify_user_token(token)["sub"], token))
            response = client.post(
                url + "/storage/v1/bucket",
                headers=service_headers,
                json={"id": bucket, "name": bucket, "public": False},
            )
            response.raise_for_status()
            bucket_created = True
            source = b"Hello, world!\nSecond line.\n"
            with TestClient(app_module.app) as api:
                own_headers = {"Authorization": "Bearer " + users[0][1]}
                foreign_headers = {"Authorization": "Bearer " + users[1][1]}
                created = api.post(
                    "/v1/ingest",
                    headers=own_headers,
                    files={"file": ("letter.txt", source)},
                    data={"name": "A Letter", "language": "en"},
                )
                assert created.status_code == 202, created.text
                job = str(UUID(created.json()["job_id"]))
                status = api.get("/v1/ingest/" + job, headers=own_headers)
                download = api.get("/v1/ingest/" + job + "/download", headers=own_headers)
                assert status.status_code == 200 and status.json()["download_ready"] is True
                assert download.status_code == 200
                graph = validate_graph(download.json()).to_wire()
                assert graph["fragments"][0]["text"] == source.decode()
                assert graph["sourceAsset"]["sha256"] == hashlib.sha256(source).hexdigest()
                assert api.get("/v1/ingest/" + job, headers=foreign_headers).status_code == 404
                assert (
                    api.get("/v1/ingest/" + job + "/download", headers=foreign_headers).status_code
                    == 404
                )
                object_url = f"{url}/storage/v1/object/{bucket}/conversion-jobs/{job}.graph.json"
                for _, user_token in users:
                    browser_headers = {"apikey": anon, "Authorization": "Bearer " + user_token}
                    denied = client.get(object_url, headers=browser_headers)
                    assert denied.status_code in (400, 403, 404)
                script = (
                    "import json,sys; from corpora_api.ingest_store import IngestStore; "
                    "from corpora_schema_types import validate_graph; "
                    "s=IngestStore(); row=s.status(sys.argv[1],sys.argv[2]); "
                    "data,_=s.download(sys.argv[1],sys.argv[2]); "
                    "g=validate_graph(json.loads(data)).to_wire(); "
                    "print(json.dumps({'ready':row['download_ready'],"
                    "'text_matches':g['fragments'][0]['text']=='Hello, world!\\nSecond line.\\n'}))"
                )
                child = subprocess.run(
                    [sys.executable, "-c", script, users[0][0], job],
                    capture_output=True,
                    text=True,
                    timeout=20,
                    check=True,
                )
                assert json.loads(child.stdout) == {"ready": True, "text_matches": True}
        finally:
            if users:
                db.execute("DELETE FROM public.conversion_jobs WHERE owner=%s", (users[0][0],))
            if bucket_created:
                rows = db.execute(
                    "SELECT name FROM storage.objects WHERE bucket_id=%s", (bucket,)
                ).fetchall()
                for (name,) in rows:
                    client.delete(
                        f"{url}/storage/v1/object/{bucket}/{quote(name, safe='/')}",
                        headers=service_headers,
                    ).raise_for_status()
                client.delete(
                    url + "/storage/v1/bucket/" + bucket, headers=service_headers
                ).raise_for_status()
            for user, _ in users:
                client.delete(
                    url + "/auth/v1/admin/users/" + user, headers=service_headers
                ).raise_for_status()
