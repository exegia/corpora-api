import importlib
import json
import time
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from corpora_api import corpus_readers_api as readers

app_module = importlib.import_module("corpora_api.app")


@pytest.fixture
def context(monkeypatch):
    actor, session, corpus, user = (uuid4() for _ in range(4))
    claims = {"sub": str(actor), "session_id": str(session), "exp": int(time.time()) + 3600}
    monkeypatch.setattr(app_module, "verify_user_token", lambda _: claims)
    return TestClient(app_module.app), corpus, user, claims


def test_reader_grants_bind_verified_identity_and_support_email(context, monkeypatch):
    client, corpus, user, claims = context

    def rpc(_self, name, params):
        assert params["p_actor"] == claims["sub"] and params["p_corpus"] == str(corpus)
        if name == "corpora_add_corpus_reader_by_email":
            assert params["p_email"] == "member@example.org"
            return {"status": "ok", "corpus_id": str(corpus), "user_id": str(user), "present": True}
        assert name == "corpora_set_corpus_reader" and params["p_reader"] == str(user)
        return {
            "status": "ok",
            "corpus_id": str(corpus),
            "user_id": str(user),
            "present": params["p_present"],
        }

    monkeypatch.setattr(readers.CreationRPC, "call", rpc)
    path = f"/v1/corpora/{corpus}/readers"
    headers = {"Authorization": "Bearer valid"}
    assert client.put(path + "/" + str(user)).status_code == 401
    added = client.post(path, headers=headers, json={"email": "member@example.org"})
    assert added.status_code == 200 and added.json()["user_id"] == str(user)
    assert added.headers["cache-control"] == "no-store"
    assert client.put(path + "/" + str(user), headers=headers).json()["present"] is True
    assert client.delete(path + "/" + str(user), headers=headers).json()["present"] is False
    assert client.post(path, headers=headers, json={"email": "bad"}).status_code == 422


def test_reader_and_shared_pages_reject_invalid_order(context, monkeypatch):
    client, corpus, user, _ = context
    later = uuid4()
    first, last = sorted((user, later), key=lambda x: x.int)
    records = [{"user_id": str(x), "created_at": "2026-01-01T00:00:00Z"} for x in (first, last)]
    result = {"status": "ok", "corpus_id": str(corpus), "readers": records}
    monkeypatch.setattr(readers.CreationRPC, "call", lambda *_: result)
    path = f"/v1/corpora/{corpus}/readers"
    headers = {"Authorization": "Bearer valid"}
    page = client.get(path, headers=headers, params={"limit": 1})
    assert page.status_code == 200 and page.json()["next_after"] == str(first)
    result["readers"] = list(reversed(records))
    assert client.get(path, headers=headers, params={"limit": 1}).status_code == 503
    assert client.get("/v1/corpora/shared").status_code == 401


def test_shared_page_accepts_large_valid_summaries(context, monkeypatch):
    client, _, _, _ = context
    ids = sorted((uuid4() for _ in range(100)), key=lambda value: value.int)
    rows = [
        {
            "id": str(corpus),
            "owner_id": str(uuid4()),
            "title": "字" * 512,
            "language": None,
            "state": "ready",
            "revision": 1,
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
        }
        for corpus in ids
    ]
    result = {"status": "list", "rows": rows}
    size = len(json.dumps(result, ensure_ascii=False).encode())
    assert size > 131072

    def rpc(self, name, params):
        assert name == "corpora_list_shared_corpora" and params["p_limit"] == 100
        if size > self.max_response_bytes:
            raise readers.CorpusMetadataError("response too large")
        return result

    monkeypatch.setattr(readers.CreationRPC, "call", rpc)
    response = client.get(
        "/v1/corpora/shared", headers={"Authorization": "Bearer valid"}, params={"limit": 100}
    )
    assert response.status_code == 200 and len(response.json()["items"]) == 100
