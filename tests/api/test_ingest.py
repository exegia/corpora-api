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
from corpora_api.auth import current_user_token
from corpora_api.ingest import build_plain_graph
from corpora_api.ingest_requests import input_fingerprint, reserve_request
from corpora_api.ingest_store import IngestStore, IngestStoreError
from corpora_api.ingest_worker import run_docling_worker

ALICE = "b9a94f14-dfc2-4699-ab8c-77378136f2b4"
BOB = "477db53d-74f4-4ae4-8c51-5f1d2282821d"


def artifact_path(job_id: str) -> str:
    return f"/storage/v1/object/conversion-jobs/conversion-jobs/{ALICE}/{job_id}.graph.json"


@pytest.fixture
def store(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[dict[str, bytes], dict[str, dict], httpx.MockTransport]:
    artifacts: dict[str, bytes] = {}
    jobs: dict[str, dict] = {}
    reservations: dict[tuple[str, str], dict] = {}

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.headers["apikey"] == "sb_publishable_test_ingest_placeholder"
        assert request.headers["authorization"] in {"Bearer alice-token", "Bearer bob-token"}
        path = request.url.path
        if path.startswith("/storage/v1/object/conversion-jobs/"):
            if request.method == "POST":
                if path in artifacts:
                    return httpx.Response(409)
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
        if path == "/rest/v1/corpora_ingest_requests":
            if request.method == "POST":
                row = json.loads(request.content)
                key = (row["owner_id"], row["request_key_hash"])
                if key in reservations:
                    return httpx.Response(409)
                reservations[key] = row
                return httpx.Response(201)
            if request.method == "GET":
                key = (
                    request.url.params["owner_id"].removeprefix("eq."),
                    request.url.params["request_key_hash"].removeprefix("eq."),
                )
                row = reservations.get(key)
                return httpx.Response(200, json=[row] if row else [])
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
    monkeypatch.setenv("SUPABASE_INGEST_API_KEY", "sb_publishable_test_ingest_placeholder")
    context = current_user_token.set("alice-token")
    monkeypatch.setenv("SUPABASE_JOBS_BUCKET", "conversion-jobs")
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
    yield artifacts, jobs, transport
    current_user_token.reset(context)


def headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.parametrize(
    ("status", "payload", "missing"),
    [
        (404, {}, True),
        (400, {"statusCode": "404", "error": "not_found", "code": "NoSuchKey"}, True),
        (400, {"statusCode": "404", "error": "not_found", "code": "other"}, False),
        (400, {"statusCode": "403", "error": "not_found", "code": "NoSuchKey"}, False),
        (403, {}, False),
    ],
)
def test_recover_recognizes_only_missing_storage_objects(store, status, payload, missing):
    adapter = IngestStore(
        httpx.Client(
            transport=httpx.MockTransport(lambda _request: httpx.Response(status, json=payload))
        )
    )
    if missing:
        assert adapter.recover(ALICE, str(uuid.uuid4()), "a.txt") is False
    else:
        with pytest.raises(IngestStoreError):
            adapter.recover(ALICE, str(uuid.uuid4()), "a.txt")


def test_idempotent_upload_reuses_job_and_rejects_changed_input(store):
    artifacts, jobs, _ = store
    client = TestClient(app)
    auth = {**headers("alice-token"), "Idempotency-Key": "request-1"}
    first = client.post("/v1/ingest", headers=auth, files={"file": ("a.txt", b"hello")})
    second = client.post("/v1/ingest", headers=auth, files={"file": ("a.txt", b"hello")})
    changed = client.post("/v1/ingest", headers=auth, files={"file": ("a.txt", b"changed")})
    other = client.post(
        "/v1/ingest",
        headers={**headers("bob-token"), "Idempotency-Key": "request-1"},
        files={"file": ("a.txt", b"hello")},
    )
    assert first.status_code == second.status_code == other.status_code == 202
    assert first.json() == second.json()
    assert other.json()["job_id"] != first.json()["job_id"]
    assert changed.status_code == 409
    assert len(jobs) == len(artifacts) == 2


def test_idempotent_save_recovers_existing_graph_after_lost_upload(store):
    artifacts, jobs, transport = store
    ingest_store = IngestStore(httpx.Client(transport=transport))
    job_id = str(uuid.uuid4())
    first_graph = json.dumps(build_plain_graph("a.txt", b"hello", name="First").to_wire()).encode()
    later_graph = json.dumps(build_plain_graph("a.txt", b"hello", name="Later").to_wire()).encode()
    key = artifact_path(job_id)
    artifacts[key] = first_graph
    ingest_store.save(ALICE, job_id, "a.txt", "Later", later_graph, retry=True)
    assert artifacts[key] == first_graph
    assert jobs[job_id]["result_key"] == f"conversion-jobs/{ALICE}/{job_id}.graph.json"
    assert jobs[job_id]["display_name"] == "First"


def test_retry_recovers_uploaded_graph_without_rerunning_builder(store, monkeypatch):
    artifacts, jobs, _ = store
    fingerprint = input_fingerprint("a.txt", b"hello", name="", language="und", corpus_id="")
    job_id = reserve_request(app_module.ingest_store, ALICE, "recover-me", fingerprint)
    graph = json.dumps(build_plain_graph("a.txt", b"hello").to_wire()).encode()
    artifacts[artifact_path(job_id)] = graph

    def unexpected_builder(*args, **kwargs):
        raise AssertionError("Recovery must not run the builder")

    monkeypatch.setattr(app_module, "build_plain_graph", unexpected_builder)
    response = TestClient(app).post(
        "/v1/ingest",
        headers={**headers("alice-token"), "Idempotency-Key": "recover-me"},
        files={"file": ("a.txt", b"hello")},
    )
    assert response.status_code == 202
    assert response.json()["job_id"] == job_id
    assert jobs[job_id]["owner"] == ALICE


@pytest.mark.parametrize("outcome", ["committed", "missing", "mismatch", "foreign", "read_failed"])
@pytest.mark.parametrize("failure", ["gateway", "timeout"])
def test_ambiguous_job_response_preserves_graph(store, outcome, failure):
    artifacts, jobs, _transport = store
    job = str(uuid.uuid4())
    graph = json.dumps(build_plain_graph("source.txt", b"durable text").to_wire()).encode()

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/storage/v1/object/conversion-jobs/"):
            if request.method == "POST":
                artifacts[request.url.path] = request.content
                return httpx.Response(201)
            if request.method == "DELETE":
                artifacts.pop(request.url.path, None)
                return httpx.Response(204)
            if request.method == "GET":
                value = artifacts.get(request.url.path)
                if value is not None:
                    return httpx.Response(200, content=value)
                return httpx.Response(404)
        if request.url.path == "/rest/v1/conversion_jobs":
            if request.method == "POST":
                row = json.loads(request.content)
                if outcome in ("committed", "mismatch", "foreign"):
                    jobs[row["id"]] = row
                if outcome == "mismatch":
                    jobs[row["id"]]["name"] = "different.txt"
                if outcome == "foreign":
                    jobs[row["id"]]["owner"] = BOB
                # A gateway error does not prove whether the row committed.
                if failure == "timeout":
                    raise httpx.ReadTimeout("lost response", request=request)
                return httpx.Response(503)
            if request.method == "GET":
                if outcome == "read_failed":
                    return httpx.Response(503)
                return httpx.Response(200, json=[jobs[job]] if job in jobs else [])
        raise AssertionError("Unexpected request")

    adapter = IngestStore(httpx.Client(transport=httpx.MockTransport(respond)))
    if outcome == "committed":
        adapter.save(ALICE, job, "source.txt", "Source", graph)
    else:
        with pytest.raises(IngestStoreError):
            adapter.save(ALICE, job, "source.txt", "Source", graph)
    assert artifacts[artifact_path(job)] == graph
    if outcome == "committed":
        assert adapter.status(ALICE, job)["download_ready"] is True
        downloaded, _filename = adapter.download(ALICE, job)
        assert downloaded == graph
    elif outcome in ("missing", "read_failed"):
        assert job not in jobs


def test_ingest_returns_job_when_insert_response_is_lost(monkeypatch, store):
    artifacts, jobs, transport = store

    def respond(request: httpx.Request) -> httpx.Response:
        response = transport.handle_request(request)
        if request.url.path == "/rest/v1/conversion_jobs" and request.method == "POST":
            raise httpx.ReadTimeout("lost response", request=request)
        return response

    adapter = IngestStore(httpx.Client(transport=httpx.MockTransport(respond)))
    monkeypatch.setattr(app_module, "ingest_store", adapter)
    with TestClient(app) as client:
        result = client.post(
            "/v1/ingest",
            files={"file": ("source.txt", b"durable text")},
            headers=headers("alice-token"),
        )
        assert result.status_code == 202, result.text
        job = result.json()["job_id"]
        assert jobs[job]["owner"] == ALICE
        assert artifacts[artifact_path(job)]
        assert client.get(f"/v1/ingest/{job}", headers=headers("alice-token")).status_code == 200


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
        assert artifacts[artifact_path(job_id)]

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
        artifacts[artifact_path(job_id)] = bad_graph
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
