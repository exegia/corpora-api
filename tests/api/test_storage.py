"""Owner-scoped corpus reads through the existing Supabase Storage API."""

from __future__ import annotations

import json
import sys
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from corpora_api import app as app_module
from corpora_api.app import app
from corpora_api.auth import UserAuthError
from corpora_api.storage import (
    CorpusStorage,
    HubCorpusStorage,
    StoredCorpusNotFound,
    SupabaseCorpusStorage,
)

ALICE = "b9a94f14-dfc2-4699-ab8c-77378136f2b4"
BOB = "477db53d-74f4-4ae4-8c51-5f1d2282821d"


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> dict[str, bytes]:
    objects = {
        f"{ALICE}/first.corpus": b"PK\x03\x04alice archive",
        f"{ALICE}/second.cusx": b"PK\x03\x04another archive",
        f"{ALICE}/notes.txt": b"not an archive",
        f"{BOB}/private.corpus": b"PK\x03\x04bob archive",
    }

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer test-service-role-placeholder"
        assert request.headers["apikey"] == "test-service-role-placeholder"
        path = request.url.path
        if path == "/storage/v1/object/list/corpora" and request.method == "POST":
            params = json.loads(request.content)
            owner = params["prefix"]
            entries = [
                {"id": name, "name": name, "metadata": {"size": len(data)}}
                for key, data in objects.items()
                if key.startswith(f"{owner}/")
                for name in [key.removeprefix(f"{owner}/")]
            ]
            start = params["offset"]
            return httpx.Response(200, json=entries[start : start + params["limit"]])
        if path.startswith("/storage/v1/object/corpora/") and request.method == "GET":
            key = path.removeprefix("/storage/v1/object/corpora/")
            if key in objects:
                return httpx.Response(200, content=objects[key])
            return httpx.Response(404)
        raise AssertionError(f"Unexpected storage call: {request.method} {path}")

    monkeypatch.setenv("SUPABASE_URL", "https://supabase.example.test")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "test-service-role-placeholder")
    monkeypatch.setenv("SUPABASE_STORAGE_BUCKET", "corpora")
    monkeypatch.setenv("CORPORA_API_TOKEN", "service-token-placeholder")
    transport = httpx.MockTransport(respond)
    storage = SupabaseCorpusStorage(httpx.Client(transport=transport))
    monkeypatch.setattr(app_module, "corpus_storage", storage)

    def verify(token: str) -> dict[str, str]:
        if token == "alice-token":
            return {"sub": ALICE}
        if token == "bob-token":
            return {"sub": BOB}
        raise UserAuthError("Invalid user token")

    monkeypatch.setattr(app_module, "verify_user_token", verify)
    return objects


def headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_list_info_and_streamed_download_are_owner_scoped(store: dict[str, bytes]) -> None:
    with TestClient(app) as client:
        listed = client.get("/v1/storage", headers=headers("alice-token"))
        info = client.get("/v1/storage/first", headers=headers("alice-token"))
        download = client.get("/v1/storage/first.corpus/download", headers=headers("alice-token"))
        foreign = client.get("/v1/storage/private.corpus/download", headers=headers("alice-token"))
        bob = client.get("/v1/storage", headers=headers("bob-token"))
        schema = client.get("/v1/openapi.json").json()

    assert listed.status_code == 200
    assert listed.json() == [
        {
            "filename": "first.corpus",
            "size_bytes": len(store[f"{ALICE}/first.corpus"]),
            "repo_id": "corpora",
            "url": f"https://supabase.example.test/storage/v1/object/corpora/{ALICE}/first.corpus",
        },
        {
            "filename": "second.cusx",
            "size_bytes": len(store[f"{ALICE}/second.cusx"]),
            "repo_id": "corpora",
            "url": f"https://supabase.example.test/storage/v1/object/corpora/{ALICE}/second.cusx",
        },
    ]
    assert info.status_code == 200
    assert info.json() == listed.json()[0]
    assert download.status_code == 200
    assert download.content == store[f"{ALICE}/first.corpus"]
    assert download.headers["content-disposition"] == 'attachment; filename="first.corpus"'
    assert foreign.status_code == 404
    assert [item["filename"] for item in bob.json()] == ["private.corpus"]
    for path in ("/v1/storage", "/v1/storage/{filename}", "/v1/storage/{filename}/download"):
        assert schema["paths"][path]["get"]["security"] == [{"SupabaseUserJWT": []}]


def test_corpus_detail_reads_are_owner_scoped_and_documented(
    monkeypatch: pytest.MonkeyPatch, store: dict[str, bytes]
) -> None:
    calls: list[tuple[str, str, str]] = []

    class ArchiveReadNotFound(Exception):
        pass

    class ArchiveTooLarge(Exception):
        pass

    def read_archive(data: bytes, view: str, **params: object) -> dict[str, object]:
        assert data == store[f"{ALICE}/first.corpus"]
        calls.append((ALICE, view, str(params.get("parent") or params.get("ref") or "")))
        if params.get("ref") == "missing":
            raise ArchiveReadNotFound("section missing")
        if params.get("fmt") == "corrupt":
            raise ValueError("corrupt graph")
        if view == "manifest":
            return {"name": "first"}
        if view == "index":
            return {"toc": None, "sections": None, "node_types": []}
        if view == "sections":
            return {
                "parent": None,
                "levels": [],
                "items": [],
                "total": 0,
                "offset": 0,
                "limit": 50,
                "next_offset": None,
            }
        if view == "content":
            return {
                "ref": None,
                "format": "",
                "passages": [],
                "total": 0,
                "offset": 0,
                "limit": 50,
                "next_offset": None,
            }
        return {
            "node": 1,
            "otype": "word",
            "is_slot": True,
            "slot_type": "word",
            "first_slot": 1,
            "last_slot": 1,
            "section_ref": "Mini 1:1",
            "text": "Alpha",
            "features": {"word": "Alpha"},
            "annotation": None,
            "node_types": ["verse", "word"],
            "context": [],
            "occurrences": 1,
            "occurrences_in_section": 1,
        }

    monkeypatch.setitem(
        sys.modules,
        "corpus_validate",
        SimpleNamespace(
            ArchiveReadNotFound=ArchiveReadNotFound,
            ArchiveTooLarge=ArchiveTooLarge,
            read_archive=read_archive,
        ),
    )

    with TestClient(app) as client:
        manifest = client.get("/v1/storage/first.corpus/manifest", headers=headers("alice-token"))
        index = client.get("/v1/storage/first.corpus/index", headers=headers("alice-token"))
        sections = client.get("/v1/storage/first.corpus/sections", headers=headers("alice-token"))
        content = client.get("/v1/storage/first.corpus/content", headers=headers("alice-token"))
        node = client.get("/v1/storage/first.corpus/nodes/1", headers=headers("alice-token"))
        foreign = client.get("/v1/storage/private.corpus/manifest", headers=headers("alice-token"))
        missing_section = client.get(
            "/v1/storage/first.corpus/content?ref=missing", headers=headers("alice-token")
        )
        corrupt = client.get(
            "/v1/storage/first.corpus/content?fmt=corrupt", headers=headers("alice-token")
        )
        unauthorized = client.get("/v1/storage/first.corpus/manifest")
        schema = client.get("/v1/openapi.json").json()

    assert [response.status_code for response in (manifest, index, sections, content, node)] == [
        200,
        200,
        200,
        200,
        200,
    ]
    assert manifest.json() == {"name": "first"}
    assert node.json()["node"] == 1
    assert foreign.status_code == 404
    assert missing_section.status_code == 404
    assert corrupt.status_code == 422
    assert unauthorized.status_code == 401
    assert [call[1] for call in calls[:5]] == ["manifest", "index", "sections", "content", "node"]
    for path in (
        "/v1/storage/{filename}/manifest",
        "/v1/storage/{filename}/index",
        "/v1/storage/{filename}/sections",
        "/v1/storage/{filename}/content",
        "/v1/storage/{filename}/nodes/{node}",
    ):
        assert schema["paths"][path]["get"]["security"] == [{"SupabaseUserJWT": []}]


def test_corpus_detail_download_is_bounded(
    monkeypatch: pytest.MonkeyPatch, store: dict[str, bytes]
) -> None:
    store[f"{ALICE}/first.corpus"] = b"x" * 3_000_001
    monkeypatch.setitem(
        sys.modules,
        "corpus_validate",
        SimpleNamespace(
            ArchiveReadNotFound=RuntimeError,
            ArchiveTooLarge=RuntimeError,
            read_archive=lambda *_args, **_kwargs: pytest.fail(
                "oversized bytes reached the reader"
            ),
        ),
    )
    with TestClient(app) as client:
        response = client.get("/v1/storage/first.corpus/manifest", headers=headers("alice-token"))
    assert response.status_code == 413


def test_corpus_detail_stream_failure_maps_to_storage_error(
    monkeypatch: pytest.MonkeyPatch, store: dict[str, bytes]
) -> None:
    class BrokenStream(httpx.SyncByteStream):
        def __iter__(self):
            yield b"partial archive"
            raise httpx.ReadError("connection closed")

        def close(self) -> None:
            pass

    response = httpx.Response(200, stream=BrokenStream())
    monkeypatch.setattr(
        app_module,
        "corpus_storage",
        SimpleNamespace(open_download=lambda _user_id, _filename: (response, "first.corpus")),
    )

    with TestClient(app) as client:
        result = client.get("/v1/storage/first.corpus/manifest", headers=headers("alice-token"))

    assert result.status_code == 502
    assert result.json() == {"detail": "Corpus storage request failed"}


def test_storage_requires_user_and_maps_missing_or_unavailable_store(
    monkeypatch: pytest.MonkeyPatch, store: dict[str, bytes]
) -> None:
    with TestClient(app) as client:
        assert client.get("/v1/storage").status_code == 401
        service_request = client.get("/v1/storage", headers=headers("service-token-placeholder"))
        missing_archive = client.get("/v1/storage/absent.corpus", headers=headers("alice-token"))
        monkeypatch.delenv("SUPABASE_STORAGE_BUCKET")
        missing_config = client.get("/v1/storage", headers=headers("alice-token"))
    assert service_request.status_code == 401
    assert missing_archive.status_code == 404
    assert missing_config.status_code == 503
    assert "SUPABASE_STORAGE_BUCKET" in missing_config.json()["detail"]
    with pytest.raises(StoredCorpusNotFound):
        app_module.corpus_storage.info(ALICE, "../private.corpus")


def test_storage_paginates_and_maps_backend_failure(
    monkeypatch: pytest.MonkeyPatch, store: dict[str, bytes]
) -> None:
    monkeypatch.setattr(SupabaseCorpusStorage, "_PAGE_SIZE", 2)
    with TestClient(app) as client:
        listed = client.get("/v1/storage", headers=headers("alice-token"))
    assert listed.status_code == 200
    assert len(listed.json()) == 2

    def unavailable(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    monkeypatch.setattr(
        app_module,
        "corpus_storage",
        SupabaseCorpusStorage(httpx.Client(transport=httpx.MockTransport(unavailable))),
    )
    with TestClient(app) as client:
        response = client.get("/v1/storage", headers=headers("alice-token"))
    assert response.status_code == 502


def test_hub_backend_reads_legacy_metadata_and_archive_bytes(
    monkeypatch: pytest.MonkeyPatch, store: dict[str, bytes]
) -> None:
    archive = b"PK\x03\x04legacy dataset archive"
    repo_id = "philagora/corpora-archives"

    class FakeHubApi:
        def list_repo_tree(self, _repo_id: str, **_kwargs: object) -> list[SimpleNamespace]:
            return [
                SimpleNamespace(path="first.corpus", size=len(archive)),
                SimpleNamespace(path="nested/hidden.corpus", size=len(archive)),
            ]

        def get_paths_info(self, _repo_id: str, **_kwargs: object) -> list[SimpleNamespace]:
            return [SimpleNamespace(path="first.corpus", size=len(archive))]

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/datasets/philagora/corpora-archives/resolve/main/first.corpus"
        assert request.headers["authorization"] == "Bearer test-hub-token-placeholder"
        return httpx.Response(200, content=archive)

    monkeypatch.setenv("STORAGE_BACKEND", "huggingface")
    monkeypatch.setenv("HF_STORAGE_REPO", repo_id)
    monkeypatch.setenv("HF_STORAGE_REPO_TYPE", "dataset")
    monkeypatch.setenv("HF_TOKEN", "test-hub-token-placeholder")
    transport = httpx.MockTransport(respond)
    hub = HubCorpusStorage(api=FakeHubApi(), client=httpx.Client(transport=transport))
    monkeypatch.setattr(app_module, "corpus_storage", CorpusStorage(hub=hub))
    with TestClient(app) as client:
        listed = client.get("/v1/storage", headers=headers("alice-token"))
        info = client.get("/v1/storage/first", headers=headers("alice-token"))
        download = client.get("/v1/storage/first.corpus/download", headers=headers("alice-token"))

    expected = {
        "filename": "first.corpus",
        "size_bytes": len(archive),
        "repo_id": repo_id,
        "url": f"https://huggingface.co/datasets/{repo_id}/resolve/main/first.corpus",
    }
    assert listed.status_code == info.status_code == download.status_code == 200
    assert listed.json() == [expected]
    assert info.json() == expected
    assert download.content == archive


def test_hub_metadata_transport_failures_map_to_bad_gateway(
    monkeypatch: pytest.MonkeyPatch, store: dict[str, bytes]
) -> None:
    class UnavailableHubApi:
        def list_repo_tree(self, _repo_id: str, **_kwargs: object) -> list[SimpleNamespace]:
            raise httpx.ReadTimeout("Hub list timed out")

        def get_paths_info(self, _repo_id: str, **_kwargs: object) -> list[SimpleNamespace]:
            raise httpx.ConnectError("Hub metadata connection failed")

    monkeypatch.setenv("STORAGE_BACKEND", "huggingface")
    monkeypatch.setenv("HF_STORAGE_REPO", "philagora/corpora-archives")
    monkeypatch.setenv("HF_STORAGE_REPO_TYPE", "dataset")
    hub = HubCorpusStorage(api=UnavailableHubApi())
    monkeypatch.setattr(app_module, "corpus_storage", CorpusStorage(hub=hub))

    with TestClient(app) as client:
        listed = client.get("/v1/storage", headers=headers("alice-token"))
        inspected = client.get("/v1/storage/first", headers=headers("alice-token"))

    assert listed.status_code == inspected.status_code == 502


def test_hub_storage_reports_missing_configuration_and_private_access(
    monkeypatch: pytest.MonkeyPatch, store: dict[str, bytes]
) -> None:
    monkeypatch.setenv("STORAGE_BACKEND", "huggingface")
    monkeypatch.delenv("HF_STORAGE_REPO", raising=False)
    monkeypatch.setattr(app_module, "corpus_storage", CorpusStorage())
    with TestClient(app) as client:
        missing_config = client.get("/v1/storage", headers=headers("alice-token"))
    assert missing_config.status_code == 503

    monkeypatch.setenv("HF_STORAGE_REPO", "philagora/corpora-archives")
    monkeypatch.setenv("HF_STORAGE_REPO_TYPE", "dataset")

    def denied(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401)

    hub = HubCorpusStorage(client=httpx.Client(transport=httpx.MockTransport(denied)))
    monkeypatch.setattr(app_module, "corpus_storage", CorpusStorage(hub=hub))
    with TestClient(app) as client:
        private_archive = client.get(
            "/v1/storage/first.corpus/download", headers=headers("alice-token")
        )
    assert private_archive.status_code == 503
    assert "HF_TOKEN" in private_archive.json()["detail"]
