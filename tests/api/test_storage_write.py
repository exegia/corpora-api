"""Authenticated publishing and deletion of validated stored corpus archives."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import unquote

import httpx
import pytest
from fastapi.testclient import TestClient

from corpora_api import app as app_module
from corpora_api.app import app
from corpora_api.auth import UserAuthError
from corpora_api.conversion_artifacts import (
    ConversionArtifactInvalid,
    ConversionArtifactNotFound,
    ConversionArtifactNotReady,
    ConversionArtifactStore,
    ConversionArtifactTooLarge,
)
from corpora_api.storage import (
    HubCorpusStorage,
    StorageNotConfigured,
    StoredCorpus,
    StoredCorpusConflict,
    StoredCorpusNotFound,
    SupabaseCorpusStorage,
)
from corpora_api.validation import MAX_VALIDATION_ARCHIVE_BYTES

ALICE = "b9a94f14-dfc2-4699-ab8c-77378136f2b4"
BOB = "477db53d-74f4-4ae4-8c51-5f1d2282821d"
JOB = "b85f991a-8aaf-4f48-b26e-a9ef63e6a7f4"
ARCHIVE = b"PK\x03\x04validated corpus bytes"
RESULT_KEY = f"conversion-jobs/{JOB}.corpus"


def conversion_row(**changes: object) -> dict[str, object]:
    row: dict[str, object] = {
        "id": JOB,
        "owner": ALICE,
        "status": "succeeded",
        "result_key": RESULT_KEY,
        "name": "source.epub",
        "display_name": "A Good Corpus",
        "output_format": "corpus",
        "validation": {"valid": True, "reasons": []},
    }
    row.update(changes)
    return row


def artifact_store(
    row: dict[str, object] | None, content: bytes = ARCHIVE
) -> ConversionArtifactStore:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/rest/v1/conversion_jobs":
            assert request.method == "GET"
            assert request.url.params["id"] == f"eq.{JOB}"
            owner = request.url.params["owner"]
            if owner != f"eq.{ALICE}" or row is None:
                return httpx.Response(200, json=[])
            return httpx.Response(200, json=[row])
        if request.url.path == f"/storage/v1/object/jobs/{RESULT_KEY}":
            assert request.method == "GET"
            return httpx.Response(200, content=content)
        raise AssertionError(
            f"Unexpected conversion artifact request: {request.method} {request.url}"
        )

    return ConversionArtifactStore(httpx.Client(transport=httpx.MockTransport(respond)))


def test_conversion_artifact_fetch_checks_owner_status_validation_and_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SUPABASE_URL", "https://supabase.example.test")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "service-placeholder")
    monkeypatch.setenv("SUPABASE_JOBS_BUCKET", "jobs")
    store = artifact_store(conversion_row())

    filename, content = store.fetch(ALICE, JOB)

    assert filename == "a-good-corpus.corpus"
    assert content == ARCHIVE
    with pytest.raises(ConversionArtifactNotFound):
        store.fetch(BOB, JOB)
    with pytest.raises(ConversionArtifactNotReady):
        artifact_store(conversion_row(status="running")).fetch(ALICE, JOB)
    with pytest.raises(ConversionArtifactInvalid):
        artifact_store(conversion_row(validation={"valid": False})).fetch(ALICE, JOB)
    with pytest.raises(ConversionArtifactInvalid):
        artifact_store(conversion_row(result_key=f"conversion-jobs/{JOB}/v1.0.corpus")).fetch(
            ALICE, JOB
        )


def test_conversion_artifact_fetch_bounds_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUPABASE_URL", "https://supabase.example.test")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "service-placeholder")
    monkeypatch.setenv("SUPABASE_JOBS_BUCKET", "jobs")
    store = artifact_store(conversion_row(), b"x" * (MAX_VALIDATION_ARCHIVE_BYTES + 1))

    with pytest.raises(ConversionArtifactTooLarge):
        store.fetch(ALICE, JOB)


def test_publish_and_delete_routes_require_user_and_never_accept_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[object, ...]] = []

    class FakeArtifacts:
        def fetch(self, owner: str, job_id: str, filename: str | None = None) -> tuple[str, bytes]:
            calls.append(("fetch", owner, job_id, filename))
            return filename or "a-good-corpus.corpus", ARCHIVE

    class FakeValidator:
        def validate(self, filename: str, archive: bytes) -> dict[str, object]:
            calls.append(("validate", filename, archive))
            return {"valid": True, "reasons": []}

    class FakeStorage:
        def upload(self, owner: str, filename: str, archive: bytes) -> StoredCorpus:
            calls.append(("upload", owner, filename, archive))
            return StoredCorpus(filename, len(archive), "private", "https://storage.test/archive")

        def delete(self, owner: str, filename: str) -> None:
            calls.append(("delete", owner, filename))

    def verify(token: str) -> dict[str, str]:
        if token == "alice-token":
            return {"sub": ALICE}
        raise UserAuthError("Invalid user token")

    monkeypatch.setattr(app_module, "conversion_artifact_store", FakeArtifacts())
    monkeypatch.setattr(app_module, "validator", FakeValidator())
    monkeypatch.setattr(app_module, "corpus_storage", FakeStorage())
    monkeypatch.setattr(app_module, "verify_user_token", verify)

    with TestClient(app) as client:
        unauthorized = client.post("/v1/storage", json={"job_id": JOB})
        published = client.post(
            "/v1/storage", headers={"Authorization": "Bearer alice-token"}, json={"job_id": JOB}
        )
        path_rejected = client.post(
            "/v1/storage",
            headers={"Authorization": "Bearer alice-token"},
            json={"job_id": JOB, "path": "/tmp/archive.corpus"},
        )
        deleted = client.delete(
            "/v1/storage/a-good-corpus.corpus",
            headers={"Authorization": "Bearer alice-token"},
        )

    assert unauthorized.status_code == 401
    assert published.status_code == 201
    assert published.json()["filename"] == "a-good-corpus.corpus"
    assert path_rejected.status_code == 422
    assert deleted.status_code == 204
    assert calls[0] == ("fetch", ALICE, JOB, None)
    assert calls[-1] == ("delete", ALICE, "a-good-corpus.corpus")


def test_invalid_artifact_is_not_published(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    class FakeArtifacts:
        def fetch(self, *_args: object) -> tuple[str, bytes]:
            return "invalid.corpus", ARCHIVE

    class FakeValidator:
        def validate(self, *_args: object) -> dict[str, object]:
            return {"valid": False, "reasons": ["bad archive"]}

    class FakeStorage:
        def upload(self, *_args: object) -> StoredCorpus:
            calls.append("upload")
            return StoredCorpus("invalid.corpus", len(ARCHIVE), "private", "https://storage.test")

    monkeypatch.setattr(app_module, "conversion_artifact_store", FakeArtifacts())
    monkeypatch.setattr(app_module, "validator", FakeValidator())
    monkeypatch.setattr(app_module, "corpus_storage", FakeStorage())
    monkeypatch.setattr(app_module, "verify_user_token", lambda _token: {"sub": ALICE})

    with TestClient(app) as client:
        response = client.post(
            "/v1/storage", headers={"Authorization": "Bearer alice-token"}, json={"job_id": JOB}
        )

    assert response.status_code == 422
    assert calls == []


def test_supabase_upload_and_delete_stay_under_the_verified_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    objects: dict[str, bytes] = {}

    def respond(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/storage/v1/object/list/corpora":
            owner = json.loads(request.content)["prefix"]
            return httpx.Response(
                200,
                json=[
                    {"id": name, "name": name, "metadata": {"size": len(data)}}
                    for key, data in objects.items()
                    if key.startswith(f"{owner}/")
                    for name in [key.removeprefix(f"{owner}/")]
                ],
            )
        if path.startswith("/storage/v1/object/corpora/"):
            key = unquote(path.removeprefix("/storage/v1/object/corpora/"))
            if request.method == "POST":
                if key in objects:
                    return httpx.Response(409)
                objects[key] = request.content
                return httpx.Response(200)
            if request.method == "DELETE":
                if key not in objects:
                    return httpx.Response(404)
                del objects[key]
                return httpx.Response(200)
        raise AssertionError(f"Unexpected Supabase storage request: {request.method} {path}")

    monkeypatch.setenv("SUPABASE_URL", "https://supabase.example.test")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "service-placeholder")
    monkeypatch.setenv("SUPABASE_STORAGE_BUCKET", "corpora")
    storage = SupabaseCorpusStorage(httpx.Client(transport=httpx.MockTransport(respond)))

    stored = storage.upload(ALICE, "new", ARCHIVE)
    assert stored.filename == "new.corpus"
    assert f"{ALICE}/new.corpus" in objects
    with pytest.raises(StoredCorpusConflict):
        storage.upload(ALICE, "new", ARCHIVE)
    with pytest.raises(StoredCorpusNotFound):
        storage.delete(BOB, "new.corpus")
    storage.delete(ALICE, "new.corpus")
    assert objects == {}


def test_hub_upload_and_delete_use_configured_repo(monkeypatch: pytest.MonkeyPatch) -> None:
    objects: dict[str, bytes] = {}

    class FakeHubApi:
        def create_repo(self, *_args: object, **_kwargs: object) -> None:
            return None

        def list_repo_tree(self, *_args: object, **_kwargs: object) -> list[SimpleNamespace]:
            return [SimpleNamespace(path=name, size=len(data)) for name, data in objects.items()]

        def get_paths_info(
            self, _repo_id: str, *, paths: list[str], **_kwargs: object
        ) -> list[SimpleNamespace]:
            return [
                SimpleNamespace(path=name, size=len(objects[name]))
                for name in paths
                if name in objects
            ]

        def upload_file(
            self, *, path_or_fileobj: str, path_in_repo: str, **_kwargs: object
        ) -> None:
            objects[path_in_repo] = Path(path_or_fileobj).read_bytes()

        def delete_file(self, path: str, *_args: object, **_kwargs: object) -> None:
            del objects[path]

    monkeypatch.setenv("HF_STORAGE_REPO", "philagora/corpora-archives")
    monkeypatch.setenv("HF_STORAGE_REPO_TYPE", "dataset")
    monkeypatch.setenv("HF_TOKEN", "read-token-placeholder")
    monkeypatch.setenv("HF_STORAGE_WRITE_TOKEN", "write-token-placeholder")
    storage = HubCorpusStorage(api=FakeHubApi())

    stored = storage.upload(ALICE, "new.corpus", ARCHIVE)
    assert stored.repo_id == "philagora/corpora-archives"
    assert objects == {"new.corpus": ARCHIVE}
    with pytest.raises(StoredCorpusConflict):
        storage.upload(ALICE, "new.corpus", ARCHIVE)
    storage.delete(ALICE, "new.corpus")
    assert objects == {}


def test_hub_writes_do_not_reuse_the_read_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HF_STORAGE_REPO", "philagora/corpora-archives")
    monkeypatch.setenv("HF_STORAGE_REPO_TYPE", "dataset")
    monkeypatch.setenv("HF_TOKEN", "read-token-placeholder")
    monkeypatch.delenv("HF_STORAGE_WRITE_TOKEN", raising=False)
    storage = HubCorpusStorage()

    with pytest.raises(StorageNotConfigured, match="HF_STORAGE_WRITE_TOKEN"):
        storage.upload(ALICE, "new.corpus", ARCHIVE)
