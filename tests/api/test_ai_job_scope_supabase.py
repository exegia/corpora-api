"""Opt-in integration against disposable local Supabase services."""

from __future__ import annotations

import io
import os
import time
import zipfile
from urllib.parse import urlparse
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from tf.fabric import Fabric

from corpora_api import app as app_module
from corpora_api.auth import verify_user_token
from corpora_api.conversion_artifacts import ConversionArtifactStore

pytestmark = pytest.mark.skipif(
    not os.getenv("CORPORA_TEST_LOCAL_SUPABASE"), reason="requires disposable local Supabase"
)


def test_job_scope_through_local_auth_postgrest_and_storage(tmp_path, monkeypatch):
    psycopg = pytest.importorskip("psycopg")
    url, dsn = os.environ["API_URL"].rstrip("/"), os.environ["DB_URL"]
    assert urlparse(url).hostname in {"localhost", "127.0.0.1", "::1"}
    assert urlparse(dsn).hostname in {"localhost", "127.0.0.1", "::1"}
    anon, service = os.environ["ANON_KEY"], os.environ["SERVICE_ROLE_KEY"]
    bucket, job = "scope-test-" + uuid4().hex, str(uuid4())
    monkeypatch.setenv("SUPABASE_URL", url)
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", service)
    monkeypatch.setenv("SUPABASE_JOBS_BUCKET", bucket)
    monkeypatch.setenv("SUPABASE_JWKS_URL", url + "/auth/v1/.well-known/jwks.json")
    monkeypatch.setenv("SUPABASE_JWT_ISSUER", url + "/auth/v1")
    monkeypatch.setattr(app_module, "conversion_artifact_store", ConversionArtifactStore())
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
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.yml", "name: Mini\nversion: '1.0'\n")
        for path in root.rglob("*"):
            if path.is_file():
                archive.write(path, f"corpora/{path.relative_to(root)}")
    service_headers = {"apikey": service, "Authorization": "Bearer " + service}
    users = []
    bucket_created, object_created = False, False
    key = f"conversion-jobs/{job}.corpus"
    object_url = f"{url}/storage/v1/object/{bucket}/{key}"
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
            client.post(
                object_url, headers=service_headers, content=stream.getvalue()
            ).raise_for_status()
            object_created = True
            db.execute(
                "INSERT INTO public.conversion_jobs"
                "(id,source_format,output_format,name,status,created_at,"
                "result_key,owner,validation) "
                "VALUES (%s,'epub','corpus','Mini','succeeded',%s,%s,%s,%s::jsonb)",
                (job, time.time(), key, users[0][0], '{"valid":true}'),
            )
            scope = {
                "corpus": "job:" + job,
                "level": "word",
                "node_id": 1,
                "label": "word",
                "version": "1.0",
            }
            with TestClient(app_module.app) as api:
                own_headers = {"Authorization": "Bearer " + users[0][1]}
                foreign_headers = {"Authorization": "Bearer " + users[1][1]}
                own = api.post("/v1/ai/validate", json={"scope": scope}, headers=own_headers)
                foreign = api.post(
                    "/v1/ai/validate", json={"scope": scope}, headers=foreign_headers
                )
                assert own.status_code == 200, own.text
                assert own.json()["checked_nodes"] == 1 and own.json()["findings"] == []
                assert foreign.status_code == 404
                db.execute("UPDATE public.conversion_jobs SET status='running' WHERE id=%s", (job,))
                pending = api.post("/v1/ai/validate", json={"scope": scope}, headers=own_headers)
                assert pending.status_code == 409
        finally:
            db.execute("DELETE FROM public.conversion_jobs WHERE id=%s", (job,))
            if object_created:
                client.delete(object_url, headers=service_headers).raise_for_status()
            if bucket_created:
                client.delete(
                    url + "/storage/v1/bucket/" + bucket, headers=service_headers
                ).raise_for_status()
            for user, _ in users:
                client.delete(
                    url + "/auth/v1/admin/users/" + user, headers=service_headers
                ).raise_for_status()
