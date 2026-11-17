"""New corpus identity reads use admission, pinned private bytes and the real worker."""

import hashlib
import importlib
import json
import time
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from test_corpus_hub_publish import real_archive

from corpora_api import corpus_access_api as access
from corpora_api import corpus_detail_api as detail
from corpora_api.auth import UserAuthError
from corpora_api.corpus_hub_deadline import DeadlineCorpusHubReader
from corpora_api.corpus_process import CorpusProcessError

app_module = importlib.import_module("corpora_api.app")
VIEWS = ("manifest", "index", "sections", "content", "nodes/3")
SHA = "c" * 40


@pytest.fixture
def fixture(monkeypatch, tmp_path):
    corpus, owner = uuid4(), uuid4()
    data = real_archive(tmp_path)
    for key, value in {
        "SUPABASE_URL": "https://db.example.test",
        "SUPABASE_SERVICE_ROLE_KEY": "test-placeholder",
        "HF_CORPUS_ORGANIZATION": "fixture",
        "HF_CORPUS_READ_TOKEN": "test-placeholder",
        "CORPORA_CORPUS_CREATION_ENABLED": "1",
    }.items():
        monkeypatch.setenv(key, value)
    value = {
        "status": "ok",
        "row": {
            "id": str(corpus),
            "owner_id": str(owner),
            "title": "Example",
            "description": None,
            "language": None,
            "properties": {},
            "hf_repo_id": f"fixture/corpus-{corpus}",
            "hf_repo_type": "dataset",
            "state": "ready",
            "revision": 3,
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
        },
        "visibility": "public",
        "head": {
            "commit_sha": SHA,
            "sequence": 1,
            "retained_ref": "corpora-op-" + str(uuid4()),
            "archive_size": len(data),
            "archive_sha256": hashlib.sha256(data).hexdigest(),
        },
    }
    state = {"access": value, "data": data, "private": True, "requests": [], "admissions": []}

    def rpc(_self, name, params):
        assert name == "corpora_read_corpus_access" and params["p_corpus"] == str(corpus)
        state["admissions"].append(params)
        if state.get("revoked"):
            raise HTTPException(401, "Session is no longer active")
        return state["access"]

    def verify(token):
        if token != "valid":
            raise UserAuthError("Invalid token")
        return {"sub": str(owner), "session_id": str(uuid4()), "exp": int(time.time()) + 3600}

    def hub(request):
        state["requests"].append(request)
        assert state["admissions"]
        assert request.url.host == "huggingface.co"
        assert request.headers["authorization"] == "Bearer test-placeholder"
        if request.url.path.startswith("/api/"):
            assert request.url.path.endswith("/revision/" + SHA)
            payload = json.dumps(
                {"id": value["row"]["hf_repo_id"], "private": state["private"], "sha": SHA}
            ).encode()
        else:
            assert request.url.path.endswith("/resolve/" + SHA + "/.corpora/source.corpus")
            payload = state["data"]
        return httpx.Response(state.get("hub_status", 200), stream=httpx.ByteStream(payload))

    monkeypatch.setattr(access.CreationRPC, "call", rpc)
    monkeypatch.setattr(app_module, "verify_user_token", verify)
    monkeypatch.setattr(
        detail, "hub_reader", DeadlineCorpusHubReader(lambda: httpx.MockTransport(hub))
    )
    return TestClient(app_module.app), f"/v1/corpora/{corpus}", state


def test_five_views_read_exact_private_archive_with_real_worker(fixture):
    client, path, state = fixture
    results = {}
    for view in VIEWS:
        response = client.get(path + "/" + view)
        assert response.status_code == 200, response.text
        assert response.headers["cache-control"] == "no-store"
        results[view] = response.json()
    assert results["manifest"]["name"] == "Tiny"
    assert results["index"]["sections"]["levels"] == ["book", "passage"]
    assert results["sections"]["items"][0]["ref"] == "Mini"
    assert results["content"]["passages"][0]["text"] == "In the gap and out."
    assert [t["node"] for p in results["content"]["passages"] for t in p["tokens"]] == [
        1,
        2,
        3,
        4,
        5,
    ]
    assert results["nodes/3"]["features"]["annotation"] == "uncovered"
    assert all(p["p_actor"] is None for p in state["admissions"])
    selected = client.get(path + "/content", params={"ref": "Mini", "limit": 1}).json()
    assert selected["total"] == 1 and selected["passages"][0]["text"] == "In the gap and out."
    assert client.get(path + "/content", params={"offset": 1, "limit": 1}).json()["passages"] == []
    first = client.get(path + "/sections", params={"parent": "Mini", "limit": 1}).json()
    second = client.get(
        path + "/sections", params={"parent": "Mini", "offset": 1, "limit": 1}
    ).json()
    assert first["next_offset"] == 1 and second["next_offset"] is None
    assert [first["items"][0]["ref"], second["items"][0]["ref"]] == ["Mini A", "Mini B"]


@pytest.mark.parametrize("view", VIEWS)
def test_denied_or_revoked_admission_never_contacts_hub(fixture, view):
    client, path, state = fixture
    state["access"] = {"status": "not_found"}
    for headers in ({}, {"Authorization": "Bearer valid"}):
        assert client.get(path + "/" + view, headers=headers).status_code == 404
    state["revoked"] = True
    assert (
        client.get(path + "/" + view, headers={"Authorization": "Bearer valid"}).status_code == 401
    )
    assert not state["requests"]


@pytest.mark.parametrize("authorization", ["", "Bearer invalid", "Basic invalid"])
def test_supplied_invalid_auth_is_never_anonymous(fixture, authorization):
    client, path, state = fixture
    assert (
        client.get(path + "/content", headers={"Authorization": authorization}).status_code == 401
    )
    assert not state["admissions"] and not state["requests"]


@pytest.mark.parametrize(
    "failure", ["digest", "short", "oversized", "public_hub", "missing_hub", "corrupt"]
)
def test_failed_integrity_or_storage_is_not_exposed(fixture, monkeypatch, failure):
    client, path, state = fixture
    if failure == "digest":
        state["data"] = b"x" * len(state["data"])
    elif failure == "short":
        state["data"] = state["data"][:-1]
    elif failure == "oversized":
        state["data"] += b"x"
    elif failure == "public_hub":
        state["private"] = False
    elif failure == "missing_hub":
        state["hub_status"] = 404
    else:
        state["data"] = b"not an archive"
        state["access"]["head"].update(
            archive_size=len(state["data"]),
            archive_sha256=hashlib.sha256(state["data"]).hexdigest(),
        )
    if failure != "corrupt":
        monkeypatch.setattr(
            detail,
            "run_corpus",
            lambda *_args, **_kwargs: pytest.fail("unverified bytes reached loader"),
        )
    response = client.get(path + "/content")
    assert response.status_code == (422 if failure == "corrupt" else 503)
    assert "fixture/" not in response.text and "huggingface" not in response.text


@pytest.mark.parametrize("view", ["nodes/999", "content?ref=Missing", "sections?parent=Missing"])
def test_missing_node_or_section_returns_generic_404(fixture, view):
    client, path, _ = fixture
    response = client.get(path + "/" + view)
    assert response.status_code == 404
    assert response.json() == {"detail": "Corpus detail is unavailable"}


@pytest.mark.parametrize("kind,code", [("too_large", 413), ("unavailable", 503)])
def test_worker_limits_return_documented_errors(fixture, monkeypatch, kind, code):
    client, path, _ = fixture

    def fail(*_args, **_kwargs):
        raise CorpusProcessError(kind, "private backing repository details")

    monkeypatch.setattr(detail, "run_corpus", fail)
    response = client.get(path + "/content")
    assert response.status_code == code
    assert response.json() == {"detail": "Corpus detail is unavailable"}


def test_openapi_only_marks_corpus_get_views_as_optional_auth():
    app_module.app.openapi_schema = None
    schema = app_module.app.openapi()
    for view in VIEWS:
        route = "/v1/corpora/{corpus_id}/" + ("nodes/{node}" if view.startswith("nodes/") else view)
        assert schema["paths"][route]["get"]["security"] == [{"SupabaseUserJWT": []}, {}]
    assert schema["paths"]["/v1/corpora/{corpus_id}/visibility"]["patch"]["security"] == [
        {"SupabaseUserJWT": []}
    ]
    assert schema["paths"]["/v1/corpora"]["get"]["security"] == [{"SupabaseUserJWT": []}]


def test_invalid_view_shape_returns_documented_error(fixture, monkeypatch):
    client, path, _ = fixture
    monkeypatch.setattr(detail, "run_corpus", lambda *_args, **_kwargs: {"invalid": True})
    response = client.get(path + "/nodes/3")
    assert response.status_code == 422
    assert response.json() == {"detail": "Corpus detail is unavailable"}
