"""Real owner isolation on an explicitly disposable local Supabase stack."""

import json
import os
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from corpora_api.app import app
from corpora_api.auth import current_user_token
from corpora_api.ingest import build_plain_graph
from corpora_api.ingest_requests import input_fingerprint, reserve_request
from corpora_api.ingest_store import IngestNotFound, IngestStore

pytestmark = pytest.mark.skipif(
    os.getenv("CORPORA_TEST_DISPOSABLE_SUPABASE") != "1", reason="requires disposable Supabase"
)


@contextmanager
def running_api(port):
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "corpora_api.app:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--no-access-log",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=30) as api:
            deadline = time.monotonic() + 20
            while True:
                if process.poll() is not None or time.monotonic() >= deadline:
                    raise RuntimeError("Local API failed to start")
                try:
                    if api.get("/v1/health", timeout=1).status_code == 200:
                        break
                except httpx.RequestError:
                    pass
                time.sleep(0.1)
            yield api
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def test_user_isolation_and_retry(monkeypatch):
    psycopg = pytest.importorskip("psycopg")
    manifest = json.loads(Path(os.environ["CORPORA_TEST_STACK_MANIFEST"]).read_text())
    stack = json.loads((Path(manifest["directory"]) / "start.json").read_text())
    assert manifest["state"] == "started" and manifest["name"].startswith("corpora-ingest-test-")
    assert stack["id"] == manifest["stack_id"] and stack["runtime"] == "native"
    assert all(stack["env"][k] == os.environ[k] for k in ("API_URL", "DB_URL"))
    url, dsn = os.environ["API_URL"].rstrip("/"), os.environ["DB_URL"]
    assert urlparse(url).hostname in {"localhost", "127.0.0.1", "::1"}
    assert urlparse(dsn).hostname in {"localhost", "127.0.0.1", "::1"}
    anon, service = os.environ["ANON_KEY"], os.environ["SERVICE_ROLE_KEY"]
    admin_headers = {"apikey": service, "Authorization": f"Bearer {service}"}
    root = Path(__file__).resolve().parents[2]
    migrations = root / "supabase/migrations"
    monkeypatch.setenv("SUPABASE_URL", url)
    monkeypatch.setenv("SUPABASE_INGEST_API_KEY", os.environ["PUBLISHABLE_KEY"])
    monkeypatch.setenv("SUPABASE_JOBS_BUCKET", "conversion-jobs")
    with psycopg.connect(dsn, autocommit=True) as db, httpx.Client(timeout=20) as client:
        assert db.execute("select to_regclass('public.conversion_jobs') is null").fetchone()[0]
        assert db.execute(
            "select to_regclass('public.corpora_ingest_requests') is null"
        ).fetchone()[0]
        assert not db.execute(
            "select exists(select 1 from storage.buckets where id='conversion-jobs')"
        ).fetchone()[0]
        db.execute(Path(os.environ["CORPORA_TEST_JOBS_SCHEMA"]).read_text())
        db.execute(next(migrations.glob("*ingest_request_reservations.sql")).read_text())
        db.execute(next(migrations.glob("*ingest_user_access.sql")).read_text())
        db.execute(
            "grant select, insert on public.conversion_jobs, public.corpora_ingest_requests to anon"
        )
        for table in ("conversion_jobs", "corpora_ingest_requests"):
            db.execute(
                f"create policy test_public_select on public.{table} "
                "for select to public using (true)"
            )
            db.execute(
                f"create policy test_public_insert on public.{table} "
                "for insert to public with check (true)"
            )
        # Prove restrictive policies protect the ingestion bucket even with a
        # permissive public policy; unrelated buckets retain that policy.
        db.execute(
            "create policy test_public_select on storage.objects for select to public using (true)"
        )
        db.execute(
            "create policy test_public_insert on storage.objects "
            "for insert to public with check (true)"
        )
        db.execute("notify pgrst, 'reload schema'")
        for bucket in ("conversion-jobs", "other-test"):
            response = client.post(
                f"{url}/storage/v1/bucket",
                headers=admin_headers,
                json={"id": bucket, "name": bucket, "public": False},
            )
            assert response.status_code in (200, 201)
        users = []
        for _ in range(2):
            email, password = f"{uuid4().hex}@example.test", uuid4().hex + "aA9!"
            response = client.post(
                f"{url}/auth/v1/admin/users",
                headers=admin_headers,
                json={"email": email, "password": password, "email_confirm": True},
            )
            assert response.status_code in (200, 201)
            user_id = response.json()["id"]
            response = client.post(
                f"{url}/auth/v1/token?grant_type=password",
                headers={"apikey": anon},
                json={"email": email, "password": password},
            )
            assert response.status_code == 200
            users.append((user_id, response.json()["access_token"]))
        owner, token = users[0]
        other, other_token = users[1]
        headers = {"apikey": anon, "Authorization": f"Bearer {token}"}
        foreign_headers = {"apikey": anon, "Authorization": f"Bearer {other_token}"}
        anonymous = {"apikey": anon, "Authorization": f"Bearer {anon}"}
        deadline = time.monotonic() + 10
        while (
            client.get(
                f"{url}/rest/v1/corpora_ingest_requests?select=job_id&limit=0", headers=headers
            ).status_code
            != 200
        ):
            if time.monotonic() >= deadline:
                pytest.fail("PostgREST schema did not reload")
            time.sleep(0.1)
        monkeypatch.setenv("SUPABASE_JWKS_URL", url + "/auth/v1/.well-known/jwks.json")
        monkeypatch.setenv("SUPABASE_JWT_ISSUER", url + "/auth/v1")
        api = TestClient(app)
        created = api.post(
            "/v1/ingest",
            headers={"Authorization": f"Bearer {token}"},
            files={"file": ("actual.txt", b"actual user")},
        )
        assert created.status_code == 202
        api_job = created.json()["job_id"]
        assert (
            api.get(
                f"/v1/ingest/{api_job}/download", headers={"Authorization": f"Bearer {token}"}
            ).status_code
            == 200
        )
        assert (
            api.get(
                f"/v1/ingest/{api_job}", headers={"Authorization": f"Bearer {other_token}"}
            ).status_code
            == 404
        )
        assert api.get(f"/v1/ingest/{api_job}").status_code == 401
        worker_python = os.getenv("CORPORA_TEST_DOCLING_PYTHON")
        if worker_python:
            monkeypatch.setenv("CORPORA_INGEST_WORKER_PYTHON", worker_python)
            worker_job = api.post(
                "/v1/ingest",
                headers={"Authorization": f"Bearer {token}"},
                files={"file": ("proof.md", b"# Heading\n\nDocument content.\n")},
            )
            assert worker_job.status_code == 202
            worker_graph = api.get(
                f"/v1/ingest/{worker_job.json()['job_id']}/download",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert worker_graph.status_code == 200
            assert "Document content." in "".join(
                part["text"] for part in worker_graph.json()["fragments"]
            )
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        with running_api(port) as first_api:
            created = first_api.post(
                "/v1/ingest",
                headers={"Authorization": f"Bearer {token}"},
                files={"file": ("restart.txt", b"persistent")},
            )
            assert created.status_code == 202
            restarted_job = created.json()["job_id"]
        with running_api(port) as restarted_api:
            assert (
                restarted_api.get(
                    f"/v1/ingest/{restarted_job}/download",
                    headers={"Authorization": f"Bearer {token}"},
                ).status_code
                == 200
            )
        context = current_user_token.set(token)
        try:
            store = IngestStore()
            fingerprint = input_fingerprint(
                "sample.txt", b"local sample", name="", language="und", corpus_id=""
            )
            job = reserve_request(store, owner, "retry", fingerprint)
            assert reserve_request(store, owner, "retry", fingerprint) == job
            graph = json.dumps(build_plain_graph("sample.txt", b"local sample").to_wire()).encode()
            store.save(owner, job, "sample.txt", "sample", graph)
            assert store.download(owner, job)[0] == graph
            assert store.recover(owner, job, "sample.txt")
        finally:
            current_user_token.reset(context)
        object_url = (
            f"{url}/storage/v1/object/conversion-jobs/conversion-jobs/{owner}/{job}.graph.json"
        )
        assert client.get(object_url, headers=headers).status_code == 200
        assert client.get(object_url, headers=foreign_headers).status_code in (400, 403, 404)
        assert client.get(object_url, headers=anonymous).status_code in (400, 403, 404)
        assert client.delete(object_url, headers=headers).status_code in (400, 403, 404)
        foreign = client.get(
            f"{url}/rest/v1/conversion_jobs", headers=foreign_headers, params={"id": f"eq.{job}"}
        )
        assert foreign.status_code == 200 and foreign.json() == []
        own_row = client.get(
            f"{url}/rest/v1/conversion_jobs", headers=headers, params={"id": f"eq.{job}"}
        ).json()[0]
        forged_job = str(uuid4())
        own_row["id"] = forged_job
        own_row["result_key"] = f"conversion-jobs/{owner}/{forged_job}.graph.json"
        assert client.post(
            f"{url}/rest/v1/conversion_jobs", headers=foreign_headers, json=own_row
        ).status_code in (401, 403)
        forged = client.post(
            f"{url}/rest/v1/corpora_ingest_requests",
            headers=foreign_headers,
            json={
                "owner_id": owner,
                "request_key_hash": "a" * 64,
                "input_hash": "b" * 64,
                "job_id": str(uuid4()),
            },
        )
        assert forged.status_code in (401, 403)
        forged_path = (
            f"{url}/storage/v1/object/conversion-jobs/conversion-jobs/{owner}/{uuid4()}.graph.json"
        )
        assert client.post(forged_path, headers=foreign_headers, content=b"{}").status_code in (
            400,
            403,
        )
        anonymous_rows = client.get(
            f"{url}/rest/v1/conversion_jobs", headers=anonymous, params={"id": f"eq.{job}"}
        )
        assert anonymous_rows.status_code in (401, 403) or (
            anonymous_rows.status_code == 200 and anonymous_rows.json() == []
        )
        other_url = f"{url}/storage/v1/object/other-test/sample.json"
        assert client.post(other_url, headers=admin_headers, content=b"{}").status_code in (
            200,
            201,
        )
        assert client.get(other_url, headers=foreign_headers).status_code == 200
        context = current_user_token.set(other_token)
        try:
            with pytest.raises(IngestNotFound):
                store.get(other, job)
        finally:
            current_user_token.reset(context)

        def concurrent_retry(_):
            token_context = current_user_token.set(token)
            try:
                retry_store = IngestStore()
                retry_job = reserve_request(retry_store, owner, "concurrent", fingerprint)
                retry_store.save(owner, retry_job, "sample.txt", "sample", graph, retry=True)
                return retry_job
            finally:
                current_user_token.reset(token_context)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(concurrent_retry, range(2)))
        assert results[0] == results[1]
        assert (
            db.execute(
                "select count(*) from public.conversion_jobs where id=%s", (results[0],)
            ).fetchone()[0]
            == 1
        )

        class LostResponses(httpx.BaseTransport):
            def __init__(self):
                self.inner = httpx.HTTPTransport()
                self.lost = set()

            def handle_request(self, request):
                response = self.inner.handle_request(request)
                if request.method == "POST" and request.url.path not in self.lost:
                    response.read()
                    assert response.status_code in (200, 201, 204)
                    response.close()
                    self.lost.add(request.url.path)
                    raise httpx.ReadTimeout("simulated lost response", request=request)
                return response

            def close(self):
                self.inner.close()

        token_context = current_user_token.set(token)
        try:
            transport = LostResponses()
            with httpx.Client(transport=transport, timeout=15) as simulated:
                retry_store = IngestStore(simulated)
                lost_job = reserve_request(retry_store, owner, "lost", fingerprint)
                retry_store.save(owner, lost_job, "sample.txt", "sample", graph, retry=True)
                assert retry_store.download(owner, lost_job)[0] == graph
                assert len(transport.lost) == 3
        finally:
            current_user_token.reset(token_context)
