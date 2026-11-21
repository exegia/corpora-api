"""Opt-in end-to-end draft lifecycle against a disposable local Supabase stack.

Requires a fresh stack prepared from supabase/bootstrap (baseline plus later
migrations, including the working draft registry) and initialized buckets.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import sys
import time
import zipfile
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

import httpx
import pytest
from cfabric import Fabric as ContextFabric
from fastapi.testclient import TestClient
from tf.fabric import Fabric

pytestmark = pytest.mark.skipif(
    os.getenv("CORPORA_TEST_DRAFT_REGISTRY") != "1",
    reason="requires a disposable local Supabase stack with the draft registry",
)

LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
DRAFT_BUCKET = "corpus-ai-drafts"
JOB_BUCKET = "conversion-jobs"

# Runs in a separate interpreter so nothing from the creating process survives.
RESTARTED_READS = r"""
import hashlib, json, os, sys
from fastapi.testclient import TestClient
from corpora_api.app import app

owner = {"Authorization": "Bearer " + os.environ["DRAFT_TEST_OWNER_TOKEN"]}
draft = os.environ["DRAFT_TEST_ID"]
with TestClient(app) as api:
    listed = api.get("/v1/ai/drafts", headers=owner)
    info = api.get("/v1/ai/drafts/" + draft, headers=owner)
    archive = api.get("/v1/ai/drafts/" + draft + "/archive", headers=owner)
json.dump(
    {
        "list": [listed.status_code, listed.json()],
        "info": [info.status_code, info.json()],
        "archive": [
            archive.status_code,
            hashlib.sha256(archive.content).hexdigest(),
            archive.headers.get("etag"),
            archive.headers.get("x-draft-revision"),
            archive.headers.get("cache-control"),
        ],
    },
    sys.stdout,
)
"""


def _corpus_archive(root: Path) -> bytes:
    root.mkdir()
    assert Fabric(locations=str(root), silent="deep").save(
        nodeFeatures={
            "otype": {1: "word", 2: "word", 3: "book"},
            "book": {3: "Mini"},
            "text": {1: "A", 2: "B"},
        },
        edgeFeatures={"oslots": {3: {1, 2}}},
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
    assert ContextFabric(locations=str(root), silent="deep").loadAll(silent="deep")
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.yml", "name: Mini\nversion: '1.0'\nformat: corpus\n")
        for path in root.rglob("*"):
            if path.is_file():
                archive.write(path, f"corpora/{path.relative_to(root)}")
    return stream.getvalue()


def test_draft_lifecycle_is_durable_owner_bound_and_hidden(monkeypatch, tmp_path):
    psycopg = pytest.importorskip("psycopg")
    workdir = Path(os.environ["CORPORA_TEST_DRAFT_REGISTRY_WORKDIR"]).resolve(strict=True)
    env = json.loads(
        subprocess.check_output(
            ["supabase", "status", "--workdir", str(workdir), "--output-format", "json"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    )["env"]
    url, dsn = env["API_URL"].rstrip("/"), env["DB_URL"]
    assert urlparse(url).hostname in LOCAL_HOSTS
    assert urlparse(dsn).hostname in LOCAL_HOSTS
    service, anon = env["SERVICE_ROLE_KEY"], env["ANON_KEY"]
    settings = {
        "SUPABASE_URL": url,
        "SUPABASE_SERVICE_ROLE_KEY": service,
        "SUPABASE_INGEST_API_KEY": env["PUBLISHABLE_KEY"],
        "SUPABASE_JWKS_URL": url + "/auth/v1/.well-known/jwks.json",
        "SUPABASE_JWT_ISSUER": url + "/auth/v1",
        "SUPABASE_JOBS_BUCKET": JOB_BUCKET,
    }
    monkeypatch.delenv("PROJECT_REF", raising=False)
    monkeypatch.delenv("SUPABASE_STORAGE_BUCKET", raising=False)
    for name, value in settings.items():
        monkeypatch.setenv(name, value)
    from corpora_api.app import app

    admin = {"apikey": service, "Authorization": "Bearer " + service}
    archive = _corpus_archive(tmp_path / "tf")
    digest = "sha256:" + hashlib.sha256(archive).hexdigest()
    job = str(uuid4())
    job_key = f"{JOB_BUCKET}/{job}.corpus"
    users: dict[str, tuple[str, str]] = {}
    draft_key = None
    with (
        psycopg.connect(dsn, autocommit=True) as db,
        httpx.Client(timeout=30, trust_env=False) as client,
    ):
        try:
            for name in ("owner", "outsider"):
                email, password = f"{name}-{uuid4().hex}@example.test", uuid4().hex + "aA9!"
                created = client.post(
                    url + "/auth/v1/admin/users",
                    headers=admin,
                    json={"email": email, "password": password, "email_confirm": True},
                )
                assert created.status_code in (200, 201)
                signed_in = client.post(
                    url + "/auth/v1/token?grant_type=password",
                    headers={"apikey": anon},
                    json={"email": email, "password": password},
                )
                assert signed_in.status_code == 200
                users[name] = (created.json()["id"], signed_in.json()["access_token"])
            owner_id, owner_token = users["owner"]
            outsider_token = users["outsider"][1]
            own = {"Authorization": "Bearer " + owner_token}
            other = {"Authorization": "Bearer " + outsider_token}

            # A completed, validated conversion owned by the owner is the source.
            db.execute(
                "INSERT INTO public.conversion_jobs(id,source_format,name,status,created_at,"
                "result_key,owner,validation,output_format) "
                "VALUES (%s,'tei','Mini','succeeded',%s,%s,%s,'{\"valid\": true}','corpus')",
                (job, time.time(), job_key, owner_id),
            )
            uploaded = client.post(
                f"{url}/storage/v1/object/{JOB_BUCKET}/{job_key}",
                headers={**admin, "Content-Type": "application/zip", "x-upsert": "false"},
                content=archive,
            )
            assert uploaded.status_code in (200, 201)

            with TestClient(app) as api:
                created = api.post("/v1/ai/drafts", json={"job_id": job}, headers=own)
                assert created.status_code == 200, created.text
                draft = created.json()
                draft_key = f"{owner_id}/ai/{draft['id']}/{draft['revision']}.corpus"
                assert draft["digest"] == digest
                assert draft["corpus"] == "draft:" + draft["id"]
                assert draft["state"] == "draft"
                retried = api.post("/v1/ai/drafts", json={"job_id": job}, headers=own)
                assert (retried.status_code, retried.json()) == (200, draft)
                foreign_job = api.post("/v1/ai/drafts", json={"job_id": job}, headers=other)
                unknown_job = api.post(
                    "/v1/ai/drafts", json={"job_id": str(uuid4())}, headers=other
                )
                assert foreign_job.status_code == unknown_job.status_code == 404
                assert foreign_job.json() == unknown_job.json()

                unknown = str(uuid4())
                for suffix in ("", "/archive"):
                    foreign = api.get(f"/v1/ai/drafts/{draft['id']}{suffix}", headers=other)
                    missing = api.get(f"/v1/ai/drafts/{unknown}{suffix}", headers=other)
                    owner_missing = api.get(f"/v1/ai/drafts/{unknown}{suffix}", headers=own)
                    malformed = api.get(f"/v1/ai/drafts/not-a-uuid{suffix}", headers=other)
                    responses = (foreign, missing, owner_missing, malformed)
                    assert {r.status_code for r in responses} == {404}
                    assert len({r.content for r in responses}) == 1
                    assert draft["id"] not in foreign.text
                listed_other = api.get("/v1/ai/drafts", headers=other)
                assert listed_other.json() == {"drafts": [], "next_offset": None}

            # Restart: a fresh interpreter reads only durable Supabase state.
            restarted = subprocess.run(
                [sys.executable, "-c", RESTARTED_READS],
                env={
                    **os.environ,
                    **settings,
                    "DRAFT_TEST_OWNER_TOKEN": owner_token,
                    "DRAFT_TEST_ID": draft["id"],
                },
                capture_output=True,
                text=True,
                timeout=120,
                check=True,
            )
            after = json.loads(restarted.stdout)
            assert after["list"] == [200, {"drafts": [draft], "next_offset": None}]
            assert after["info"] == [200, draft]
            assert after["archive"] == [
                200,
                digest.removeprefix("sha256:"),
                f'"{digest}"',
                draft["revision"],
                "private, no-store",
            ]

            # Provenance and owner binding are recorded server-side.
            row = db.execute(
                "SELECT owner::text,source_job_id,source_digest,object_key,name "
                "FROM public.corpus_ai_drafts WHERE document_id=%s",
                (draft["id"],),
            ).fetchone()
            assert row == (owner_id, job, digest, draft_key, "Mini")

            # Browser roles cannot reach the registry, the RPC or the archives.
            for key in (anon, owner_token, outsider_token):
                headers = {"apikey": anon, "Authorization": "Bearer " + key}
                table = client.get(url + "/rest/v1/corpus_ai_drafts?select=*", headers=headers)
                assert table.status_code in (401, 403, 404)
                rpc = client.post(
                    url + "/rest/v1/rpc/corpora_ai_storage",
                    headers=headers,
                    json={"p_action": "drafts", "p_owner": owner_id, "p_payload": {}},
                )
                assert rpc.status_code in (401, 403, 404)
                draft_url = f"{url}/storage/v1/object/{DRAFT_BUCKET}/"
                read = client.get(draft_url + draft_key, headers=headers)
                assert read.status_code in (400, 401, 403, 404)
                write = client.post(
                    f"{draft_url}{owner_id}/ai/{uuid4()}/{uuid4()}.corpus",
                    headers={**headers, "Content-Type": "application/zip"},
                    content=b"forged",
                )
                assert write.status_code in (400, 401, 403, 404)
            listing = client.post(
                f"{url}/storage/v1/object/list/{DRAFT_BUCKET}",
                headers={"apikey": anon, "Authorization": "Bearer " + owner_token},
                json={"prefix": owner_id, "limit": 100},
            )
            assert listing.status_code != 200 or listing.json() == []
            assert (
                db.execute(
                    "SELECT count(*) FROM storage.objects WHERE bucket_id=%s AND name=%s",
                    (DRAFT_BUCKET, draft_key),
                ).fetchone()[0]
                == 1
            )

            # The registry fails closed: it refuses to replay over itself and
            # reports no capability while the archive bucket is not private.
            migration = (
                Path(__file__).resolve().parents[2]
                / "supabase/migrations/20261120090000_working_draft_registry.sql"
            ).read_text()
            with pytest.raises(
                psycopg.errors.RaiseException, match="WORKING_DRAFT_REGISTRY_ALREADY_PRESENT"
            ):
                db.execute(migration, prepare=False)
            db.execute("ROLLBACK")
            with db.transaction(force_rollback=True):
                db.execute("UPDATE storage.buckets SET public=true WHERE id=%s", (DRAFT_BUCKET,))
                db.execute("SET LOCAL ROLE service_role")
                with pytest.raises(psycopg.errors.RaiseException, match="AI_UNAVAILABLE"):
                    db.execute(
                        "SELECT public.corpora_ai_storage('capabilities',%s,'{}')", (owner_id,)
                    )
            assert db.execute(
                "SELECT public FROM storage.buckets WHERE id=%s", (DRAFT_BUCKET,)
            ).fetchone() == (False,)
        finally:
            if draft_key:
                client.delete(f"{url}/storage/v1/object/{DRAFT_BUCKET}/{draft_key}", headers=admin)
            client.delete(f"{url}/storage/v1/object/{JOB_BUCKET}/{job_key}", headers=admin)
            ids = [user_id for user_id, _ in users.values()]
            db.execute("DELETE FROM public.corpus_ai_drafts WHERE owner::text = ANY(%s)", (ids,))
            db.execute("DELETE FROM public.conversion_jobs WHERE id=%s", (job,))
            for user_id in ids:
                removed = client.delete(url + "/auth/v1/admin/users/" + user_id, headers=admin)
                assert removed.status_code in (200, 204, 404)
            assert (
                db.execute(
                    "SELECT count(*) FROM storage.objects WHERE bucket_id=%s AND name LIKE %s",
                    (DRAFT_BUCKET, "%/ai/%"),
                ).fetchone()[0]
                == 0
            )
