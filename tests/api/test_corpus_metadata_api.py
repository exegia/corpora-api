import importlib
import time
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from corpora_api import corpus_metadata_api as metadata
from corpora_api.corpus_creation import CreationRPC
from corpora_api.corpus_metadata import CorpusMetadataError

app_module = importlib.import_module("corpora_api.app")


@pytest.fixture
def configured(monkeypatch):
    for key, value in {
        "CORPORA_CORPUS_CREATION_ENABLED": "1",
        "HF_CORPUS_ORGANIZATION": "fixture",
        "SUPABASE_URL": "https://db.test",
        "SUPABASE_SERVICE_ROLE_KEY": "test-key-placeholder",
    }.items():
        monkeypatch.setenv(key, value)
    claims = {"sub": str(uuid4()), "session_id": str(uuid4()), "exp": int(time.time()) + 3600}
    monkeypatch.setattr(app_module, "verify_user_token", lambda _: claims)
    return claims


@pytest.mark.parametrize(
    "fields",
    [
        {},
        {"title": None},
        {"title": "   "},
        {"properties": None},
        {"properties": {"large": "x" * 16384}},
        {"title": "bad\x00title"},
        {"owner_id": str(uuid4())},
        {"state": "ready"},
        {"hf_repo_id": "foreign/repo"},
    ],
)
def test_invalid_metadata_edit_never_reaches_database(monkeypatch, configured, fields):
    monkeypatch.setattr(CreationRPC, "call", lambda *_: pytest.fail("Unexpected RPC"))
    with TestClient(app_module.app) as client:
        response = client.patch(
            f"/v1/corpora/{uuid4()}/metadata",
            headers={"Authorization": "Bearer fixture"},
            json={"expected_revision": 1, **fields},
        )
    assert response.status_code == 422


def test_metadata_routes_require_user_authentication():
    with TestClient(app_module.app) as client:
        assert client.get("/v1/corpora").status_code == 401
        assert client.get(f"/v1/corpora/{uuid4()}/metadata").status_code == 401
        assert client.patch(f"/v1/corpora/{uuid4()}/metadata", json={}).status_code == 401


def test_metadata_patch_preflight_allows_documented_browser_origin():
    with TestClient(app_module.app) as client:
        response = client.options(
            f"/v1/corpora/{uuid4()}/metadata",
            headers={
                "Origin": "https://docs.exegia.co",
                "Access-Control-Request-Method": "PATCH",
                "Access-Control-Request-Headers": "authorization,content-type",
            },
        )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "https://docs.exegia.co"
    assert "PATCH" in response.headers["access-control-allow-methods"]


@pytest.mark.parametrize("invalid", [{"status": "list", "rows": [{}]}, {}, None])
def test_invalid_metadata_page_fails_closed(monkeypatch, configured, invalid):
    monkeypatch.setattr(CreationRPC, "call", lambda *_: invalid)
    with pytest.raises(CorpusMetadataError):
        metadata.list_metadata(configured, None, None, 50)


@pytest.mark.parametrize("status,code", [("not_found", 404), ("conflict", 409)])
def test_metadata_domain_errors_are_generic(monkeypatch, configured, status, code):
    monkeypatch.setattr(CreationRPC, "call", lambda *_: {"status": status})
    with TestClient(app_module.app) as client:
        response = client.patch(
            f"/v1/corpora/{uuid4()}/metadata",
            headers={"Authorization": "Bearer fixture"},
            json={"expected_revision": 1, "language": "en"},
        )
    assert response.status_code == code
    assert "repo" not in response.text


def test_rpc_page_response_limit_is_enforced(configured):
    def response(_request):
        return httpx.Response(200, stream=httpx.ByteStream(b'"' + b"x" * 100 + b'"'))

    with pytest.raises(CorpusMetadataError):
        CreationRPC(lambda: httpx.MockTransport(response), max_response_bytes=32).call(
            "corpora_list_metadata", {}
        )
