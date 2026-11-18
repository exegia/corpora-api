import importlib
import time
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from corpora_api import project_corpora_api as corpora

app_module = importlib.import_module("corpora_api.app")


@pytest.fixture
def context(monkeypatch):
    actor, session, project, corpus = (uuid4() for _ in range(4))
    claims = {"sub": str(actor), "session_id": str(session), "exp": int(time.time()) + 3600}
    monkeypatch.setattr(app_module, "verify_user_token", lambda _: claims)
    monkeypatch.delenv("CORPORA_CORPUS_CREATION_ENABLED", raising=False)
    return TestClient(app_module.app), project, corpus, claims


def test_project_corpus_mutations_bind_user_and_identity(context, monkeypatch):
    client, project, corpus, claims = context
    calls = []

    def rpc(_self, name, params):
        calls.append((name, params))
        assert params["p_actor"] == claims["sub"] and params["p_session"] == claims["session_id"]
        assert params["p_project"] == str(project) and params["p_corpus"] == str(corpus)
        return {
            "status": "ok",
            "project_id": str(project),
            "corpus_id": str(corpus),
            "linked": params["p_present"],
        }

    monkeypatch.setattr(corpora.CreationRPC, "call", rpc)
    path = f"/v1/projects/{project}/corpora/{corpus}"
    headers = {"Authorization": "Bearer valid"}
    assert client.put(path).status_code == 401
    added = client.put(path, headers=headers)
    assert added.status_code == 200 and added.json()["linked"] is True
    assert added.headers["cache-control"] == "no-store"
    assert client.delete(path, headers=headers).json()["linked"] is False
    assert [params["p_present"] for _, params in calls] == [True, False]
    monkeypatch.setattr(corpora.CreationRPC, "call", lambda *_: {"status": "not_found"})
    assert client.put(path, headers=headers).status_code == 404
    monkeypatch.setattr(
        corpora.CreationRPC,
        "call",
        lambda *_: {
            "status": "ok",
            "project_id": str(uuid4()),
            "corpus_id": str(corpus),
            "linked": True,
        },
    )
    assert client.put(path, headers=headers).status_code == 503


def test_project_corpus_pages_validate_order_and_bounds(context, monkeypatch):
    client, project, corpus, claims = context
    first, last = sorted((corpus, uuid4()), key=lambda value: value.int)

    def row(identifier):
        return {
            "id": str(identifier),
            "owner_id": claims["sub"],
            "title": "A corpus",
            "language": None,
            "state": "ready",
            "revision": 1,
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
            "linked": True,
        }

    result = {"status": "list", "rows": [row(first), row(last)]}
    monkeypatch.setattr(corpora.CreationRPC, "call", lambda *_: result)
    path = f"/v1/projects/{project}/corpora"
    headers = {"Authorization": "Bearer valid"}
    page = client.get(path, headers=headers, params={"limit": 1})
    assert page.status_code == 200 and page.json()["next_cursor"] == str(first)
    assert page.headers["cache-control"] == "no-store"
    result["rows"] = list(reversed(result["rows"]))
    assert client.get(path, headers=headers, params={"limit": 1}).status_code == 503
    result["rows"] = [row(first) | {"linked": False}]
    assert client.get(path, headers=headers).status_code == 503
    assert client.get(path, headers=headers, params={"limit": 101}).status_code == 422
