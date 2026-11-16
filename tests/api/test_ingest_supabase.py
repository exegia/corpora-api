"""Opt-in ingestion test against disposable local Supabase services.

Set CORPORA_TEST_DOCLING_PYTHON to a worker interpreter containing the API,
its schema bindings, and corpora-admin with the Docling extra to test Markdown.
"""

from __future__ import annotations

import hashlib
import os
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from urllib.parse import quote, urlparse
from uuid import UUID, uuid4

import httpx
import pytest
from corpora_schema_types import validate_graph

from corpora_api.auth import verify_user_token

pytestmark = pytest.mark.skipif(
    not os.getenv("CORPORA_TEST_LOCAL_SUPABASE"), reason="requires disposable local Supabase"
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
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=120) as api:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError("Local API process exited during startup")
                try:
                    if api.get("/v1/health", timeout=1).status_code == 200:
                        break
                except httpx.RequestError:
                    pass
                time.sleep(0.1)
            else:
                raise TimeoutError("Local API process did not start")
            yield api, process.pid
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


@pytest.mark.parametrize(
    ("filename", "source", "expected_text"),
    [
        ("letter.txt", b"Hello, world!\nSecond line.\n", "Hello, world!\nSecond line.\n"),
        pytest.param(
            "proof.md",
            b"# Worker proof\n\nA small document for ingestion.\n",
            "A small document for ingestion.",
            marks=pytest.mark.skipif(
                not os.getenv("CORPORA_TEST_DOCLING_PYTHON"),
                reason="requires a configured real Docling worker interpreter",
            ),
        ),
    ],
)
def test_ingest_persists_graph_across_processes_and_isolates_users(
    monkeypatch, filename, source, expected_text
):
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
    if filename.endswith(".md"):
        monkeypatch.setenv(
            "CORPORA_INGEST_WORKER_PYTHON", os.environ["CORPORA_TEST_DOCLING_PYTHON"]
        )
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
            own_headers = {"Authorization": "Bearer " + users[0][1]}
            foreign_headers = {"Authorization": "Bearer " + users[1][1]}
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                port = probe.getsockname()[1]
            with running_api(port) as (api, original_pid):
                created = api.post(
                    "/v1/ingest",
                    headers=own_headers,
                    files={"file": (filename, source)},
                    data={"name": "A Letter", "language": "en"},
                )
                assert created.status_code == 202, created.text
                job = str(UUID(created.json()["job_id"]))
            with running_api(port) as (api, restarted_pid):
                assert restarted_pid != original_pid
                status = api.get("/v1/ingest/" + job, headers=own_headers)
                download = api.get("/v1/ingest/" + job + "/download", headers=own_headers)
                assert status.status_code == 200 and status.json()["download_ready"] is True
                assert download.status_code == 200
                graph = validate_graph(download.json()).to_wire()
                text = "".join(item["text"] for item in graph["fragments"])
                assert expected_text in text
                if filename.endswith(".txt"):
                    assert text == expected_text
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
