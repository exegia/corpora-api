"""Durable plain-text ingestion, schema validation, and owner isolation."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
import uuid
from pathlib import Path

import httpx
import pytest
from corpora_schema_types import validate_graph
from fastapi.testclient import TestClient

from corpora_api import app as app_module
from corpora_api.app import app
from corpora_api.ingest import build_plain_graph
from corpora_api.ingest_store import IngestStore
from corpora_api.ingest_worker import run_docling_worker

ALICE = "b9a94f14-dfc2-4699-ab8c-77378136f2b4"
BOB = "477db53d-74f4-4ae4-8c51-5f1d2282821d"


@pytest.fixture
def store(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[dict[str, bytes], dict[str, dict], httpx.MockTransport]:
    artifacts: dict[str, bytes] = {}
    jobs: dict[str, dict] = {}

    def respond(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.startswith("/storage/v1/object/jobs/"):
            if request.method == "POST":
                artifacts[path] = request.content
                return httpx.Response(201, json={"Key": path})
            if request.method == "GET":
                data = artifacts.get(path)
                if data is not None:
                    return httpx.Response(200, content=data)
                return httpx.Response(404)
            if request.method == "DELETE":
                artifacts.pop(path, None)
                return httpx.Response(204)
        if path == "/rest/v1/conversion_jobs":
            if request.method == "POST":
                row = json.loads(request.content)
                jobs[row["id"]] = row
                return httpx.Response(201)
            if request.method == "GET":
                job_id = request.url.params["id"].removeprefix("eq.")
                owner = request.url.params["owner"].removeprefix("eq.")
                row = jobs.get(job_id)
                return httpx.Response(200, json=[row] if row and row["owner"] == owner else [])
        raise AssertionError(f"Unexpected store call: {request.method} {path}")

    transport = httpx.MockTransport(respond)
    monkeypatch.setenv("SUPABASE_URL", "https://supabase.example.test")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "test-service-role-placeholder")
    monkeypatch.setenv("SUPABASE_JOBS_BUCKET", "jobs")
    monkeypatch.setenv("CORPORA_API_TOKEN", "service-token-placeholder")
    monkeypatch.setattr(app_module, "ingest_store", IngestStore(httpx.Client(transport=transport)))

    def verify(token: str) -> dict[str, str]:
        from corpora_api.auth import UserAuthError

        if token == "alice-token":
            return {"sub": ALICE}
        if token == "bob-token":
            return {"sub": BOB}
        raise UserAuthError("Invalid user token")

    monkeypatch.setattr(app_module, "verify_user_token", verify)
    return artifacts, jobs, transport


def headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_upload_poll_download_survives_store_recreation_and_isolates_owner(
    monkeypatch: pytest.MonkeyPatch,
    store: tuple[dict[str, bytes], dict[str, dict], httpx.MockTransport],
) -> None:
    artifacts, jobs, transport = store
    source = b"Hello, world!\nSecond line.\n"
    with TestClient(app) as client:
        created = client.post(
            "/v1/ingest",
            headers=headers("alice-token"),
            files={"file": ("letter.txt", source)},
            data={"name": "A Letter", "language": "en"},
        )
        assert created.status_code == 202, created.text
        job_id = created.json()["job_id"]
        assert uuid.UUID(job_id)
        assert created.json()["status_url"] == f"/v1/ingest/{job_id}"
        assert jobs[job_id]["owner"] == ALICE
        assert artifacts[f"/storage/v1/object/jobs/conversion-jobs/{job_id}.graph.json"]

        # A fresh adapter has no process-local job or graph state.
        monkeypatch.setattr(
            app_module, "ingest_store", IngestStore(httpx.Client(transport=transport))
        )
        status = client.get(f"/v1/ingest/{job_id}", headers=headers("alice-token"))
        download = client.get(f"/v1/ingest/{job_id}/download", headers=headers("alice-token"))
        foreign_status = client.get(f"/v1/ingest/{job_id}", headers=headers("bob-token"))
        foreign_download = client.get(f"/v1/ingest/{job_id}/download", headers=headers("bob-token"))
        schema = client.get("/v1/openapi.json").json()

    assert status.status_code == 200
    assert status.json()["status"] == "succeeded"
    assert status.json()["result_filename"] == "a-letter.graph.json"
    assert status.json()["download_ready"] is True
    assert download.status_code == 200
    assert download.headers["content-disposition"] == 'attachment; filename="a-letter.graph.json"'
    graph = validate_graph(download.json()).to_wire()
    assert graph["fragments"][0]["text"] == source.decode()
    assert graph["sourceAsset"]["sha256"] == hashlib.sha256(source).hexdigest()
    assert foreign_status.status_code == 404
    assert foreign_download.status_code == 404
    assert schema["paths"]["/v1/ingest"]["post"]["security"] == [{"SupabaseUserJWT": []}]
    assert schema["paths"]["/v1/ingest/{job_id}"]["get"]["security"] == [{"SupabaseUserJWT": []}]
    assert "IngestJobStatus" in schema["components"]["schemas"]


def test_ingest_rejects_missing_identity_and_invalid_input(
    store: tuple[dict[str, bytes], dict[str, dict], httpx.MockTransport],
) -> None:
    with TestClient(app) as client:
        assert client.post("/v1/ingest", files={"file": ("x.txt", b"text")}).status_code == 401
        assert (
            client.post(
                "/v1/ingest",
                headers=headers("service-token-placeholder"),
                files={"file": ("x.txt", b"text")},
            ).status_code
            == 401
        )
        cases = [
            ("x.bin", b"unknown", {}, 415),
            ("x.pdf", b"%PDF", {}, 503),
            ("x.txt", b"\xff", {}, 422),
            ("x.txt", b"", {}, 422),
            ("x.txt", b"hello", {"language": "bad tag!"}, 422),
            ("x.txt", b"hello", {"corpus_id": "not-a-uuid"}, 422),
            ("x.txt", b"x" * 3_000_001, {}, 413),
        ]
        for filename, source, data, status in cases:
            response = client.post(
                "/v1/ingest",
                headers=headers("alice-token"),
                files={"file": (filename, source)},
                data=data,
            )
            assert response.status_code == status, response.text
        missing = client.get("/v1/ingest/not-a-uuid", headers=headers("alice-token"))
        assert missing.status_code == 404
    assert not store[0]
    assert not store[1]


def test_existing_corpus_is_referenced_without_minting_another() -> None:
    corpus_id = str(uuid.uuid4())
    graph = build_plain_graph("document.txt", b"Exact text", corpus_id=corpus_id)
    assert graph.corpus is None
    assert graph.work["corpusId"] == corpus_id


def test_missing_store_configuration_fails_before_publishing(
    monkeypatch: pytest.MonkeyPatch,
    store: tuple[dict[str, bytes], dict[str, dict], httpx.MockTransport],
) -> None:
    monkeypatch.delenv("SUPABASE_JOBS_BUCKET")
    monkeypatch.delenv("SUPABASE_STORAGE_BUCKET", raising=False)
    with TestClient(app) as client:
        response = client.post(
            "/v1/ingest",
            headers=headers("alice-token"),
            files={"file": ("document.txt", b"Exact text")},
        )
    assert response.status_code == 503
    assert not store[0]
    assert not store[1]


@pytest.mark.parametrize("bad_graph", [b"not JSON", b'{"schemaVersion":"1.0.0"}'])
def test_download_rejects_a_corrupted_stored_graph(
    bad_graph: bytes,
    store: tuple[dict[str, bytes], dict[str, dict], httpx.MockTransport],
) -> None:
    artifacts, _jobs, _transport = store
    with TestClient(app) as client:
        created = client.post(
            "/v1/ingest",
            headers=headers("alice-token"),
            files={"file": ("document.txt", b"Exact text")},
        )
        assert created.status_code == 202
        job_id = created.json()["job_id"]
        artifacts[f"/storage/v1/object/jobs/conversion-jobs/{job_id}.graph.json"] = bad_graph
        download = client.get(f"/v1/ingest/{job_id}/download", headers=headers("alice-token"))
    assert download.status_code == 502


def test_optional_worker_uses_staged_upload_and_validates_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CORPORA_INGEST_WORKER_PYTHON", "/opt/docling/bin/python")

    def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        source = Path(command[command.index("--source") + 1])
        result = Path(command[command.index("--result") + 1])
        assert source.name == "document.pdf"
        assert source.read_bytes() == b"document contents"
        graph = build_plain_graph("document.txt", b"document contents").to_wire()
        graph["sourceAsset"].update(
            sourceFormat="pdf", mediaType="application/pdf", filename="document.pdf"
        )
        result.write_text(json.dumps(graph), encoding="utf-8")
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr("corpora_api.ingest_worker.subprocess.run", fake_run)
    graph = run_docling_worker(
        "document.pdf", b"document contents", name="", language="und", corpus_id=""
    )
    assert graph.source_asset["sourceFormat"] == "pdf"


def test_configured_worker_without_optional_dependencies_fails_before_storage(monkeypatch, store):
    if importlib.util.find_spec("admin") is not None:
        pytest.skip("requires an API environment without the optional admin package")
    artifacts, jobs, _transport = store
    monkeypatch.setenv("CORPORA_INGEST_WORKER_PYTHON", sys.executable)
    with TestClient(app) as client:
        response = client.post(
            "/v1/ingest",
            headers=headers("alice-token"),
            files={"file": ("document.pdf", b"%PDF fixture")},
        )
    assert response.status_code == 503
    assert response.json() == {"detail": "Docling worker dependencies are unavailable"}
    assert not artifacts and not jobs
