"""Conversation ownership, retry and stale-scope behavior with a real TF archive."""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from tf.fabric import Fabric

from corpora_api import app as app_module
from corpora_api.app import app
from corpora_api.auth import UserAuthError
from corpora_api.models import AIThreadCreate, AIThreadSuggestionData
from corpora_api.thread_store import ThreadStore

ALICE = "b9a94f14-dfc2-4699-ab8c-77378136f2b4"
BOB = "477db53d-74f4-4ae4-8c51-5f1d2282821d"
CREATE_KEY = "00000000-0000-4000-8000-000000000001"
MESSAGE_KEY = "00000000-0000-4000-8000-000000000002"
FORK_KEY = "00000000-0000-4000-8000-000000000003"
DRAFT = "00000000-0000-4000-8000-000000000004"


def test_thread_scopes_accept_ranges_in_corpus_reading_order() -> None:
    scope = {
        "corpus": "first",
        "level": "passage",
        "node_id": 5,
        "label": "passage",
        "version": "1.0",
        "unit_range": {"start": 5, "end": 4},
    }
    assert AIThreadCreate.model_validate({"scope": scope}).scope.unit_range.end == 4
    suggestion = AIThreadSuggestionData.model_validate(
        {
            "id": "s1",
            "scope": scope,
            "kind": "text",
            "target_node": 5,
            "diff": [{"field": "text", "old": "a", "new": "b"}],
            "rationale": "Example",
            "base_version": "1.0",
            "content_hash": "sha256:test",
        }
    )
    assert suggestion.scope.unit_range.end == 4


@pytest.fixture
def setup_threads(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    root = tmp_path / "tf"
    root.mkdir()
    assert Fabric(locations=str(root), silent="deep").save(
        nodeFeatures={"otype": {1: "word", 2: "book"}, "book": {2: "Mini"}, "text": {1: "A"}},
        edgeFeatures={"oslots": {2: {1}}},
        metaData={
            "otype": {"valueType": "str"},
            "oslots": {"valueType": "str"},
            "book": {"valueType": "str"},
            "text": {"valueType": "str"},
            "otext": {
                "sectionTypes": "book",
                "sectionFeatures": "book",
                "fmt:text-orig-full": "{text}",
            },
        },
    )
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("manifest.yml", "name: Mini\nversion: '1.0'\n")
        for path in root.rglob("*"):
            if path.is_file():
                archive.write(path, f"corpora/{path.relative_to(root)}")
    data = stream.getvalue()
    allowed = {ALICE}
    rows: dict[str, dict] = {}
    retries = [0]

    class Storage:
        def open_download(self, owner: str, filename: str):
            if owner not in allowed or filename != "first":
                raise HTTPException(404, "Corpus not found")
            return httpx.Response(200, content=data), filename

    class Drafts:
        def resolve(self, owner: str, draft_id: str):
            if owner not in allowed or draft_id != DRAFT:
                raise HTTPException(404, "Draft not found")
            return SimpleNamespace(owner=owner)

        def _download(self, head):
            return data

    def registry(request: httpx.Request) -> httpx.Response:
        assert request.headers["apikey"] == "service-placeholder"
        params = request.url.params
        if request.method == "POST":
            row = json.loads(request.content)
            if row["id"] in rows:
                return httpx.Response(201, json=[])
            rows[row["id"]] = row
            return httpx.Response(201, json=[row])
        owner = params.get("owner", "").removeprefix("eq.")
        assert owner in (ALICE, BOB)
        found = [row for row in rows.values() if row["owner"] == owner]
        if params.get("id"):
            found = [row for row in found if row["id"] == params["id"].removeprefix("eq.")]
        if params.get("corpus"):
            found = [row for row in found if row["corpus"] == params["corpus"].removeprefix("eq.")]
        if request.method == "PATCH":
            assert len(found) <= 1
            if retries[0] == 0:
                retries[0] += 1
                found[0]["revision"] += 1
                return httpx.Response(200, json=[])
            if not found or found[0]["revision"] != int(params["revision"].removeprefix("eq.")):
                return httpx.Response(200, json=[])
            found[0].update(json.loads(request.content))
        return httpx.Response(200, json=found[: int(params.get("limit", "100"))])

    def verify(token: str):
        if token == "alice-token":
            return {"sub": ALICE}
        if token == "bob-token":
            return {"sub": BOB}
        raise UserAuthError("Invalid user token")

    monkeypatch.setenv("SUPABASE_URL", "https://example.invalid")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "service-placeholder")
    monkeypatch.setattr(app_module, "verify_user_token", verify)
    monkeypatch.setattr(app_module, "corpus_storage", Storage())
    monkeypatch.setattr(app_module, "draft_store", Drafts())
    monkeypatch.setattr(
        app_module,
        "thread_store",
        ThreadStore(httpx.Client(transport=httpx.MockTransport(registry))),
    )
    return rows, allowed, retries, registry


def test_thread_ownership_idempotency_and_stale_scope(setup_threads, monkeypatch):
    rows, allowed, retries, registry = setup_threads
    alice = {"Authorization": "Bearer alice-token", "Idempotency-Key": CREATE_KEY}
    bob = {"Authorization": "Bearer bob-token"}
    scope = {"corpus": "first", "level": "word", "node_id": 1, "label": "word", "version": "1.0"}
    with TestClient(app) as client:
        schema = client.get("/v1/openapi.json").json()
        for path, method in (
            ("/v1/ai/threads", "post"),
            ("/v1/ai/threads", "get"),
            ("/v1/ai/threads/{thread_id}", "get"),
            ("/v1/ai/threads/{thread_id}/sections", "post"),
            ("/v1/ai/threads/{thread_id}/messages", "post"),
            ("/v1/ai/threads/{thread_id}/messages", "get"),
            ("/v1/ai/threads/{thread_id}/suggestions", "get"),
        ):
            assert schema["paths"][path][method]["security"] == [{"SupabaseUserJWT": []}]
        assert client.post("/v1/ai/threads", json={"scope": scope}).status_code == 401
        bad_key = client.post(
            "/v1/ai/threads",
            json={"scope": scope},
            headers={**alice, "Idempotency-Key": "invalid"},
        )
        assert bad_key.status_code == 422
        created = client.post("/v1/ai/threads", json={"scope": scope}, headers=alice)
        assert created.status_code == 200, created.text
        thread = created.json()
        thread_id, section_id = thread["id"], thread["sections"][0]["id"]
        assert created.headers["cache-control"] == "private, no-store"
        assert client.post("/v1/ai/threads", json={"scope": scope}, headers=alice).json() == thread
        conflict = client.post(
            "/v1/ai/threads", json={"scope": {**scope, "label": "other"}}, headers=alice
        )
        assert conflict.status_code == 409
        assert client.get(f"/v1/ai/threads/{thread_id}", headers=bob).status_code == 404
        message_headers = {**alice, "Idempotency-Key": MESSAGE_KEY}
        body = {"section_id": section_id, "content": "hello"}
        first = client.post(
            f"/v1/ai/threads/{thread_id}/messages", json=body, headers=message_headers
        )
        assert first.status_code == 200, first.text
        again = client.post(
            f"/v1/ai/threads/{thread_id}/messages", json=body, headers=message_headers
        )
        assert again.json() == first.json()
        changed = client.post(
            f"/v1/ai/threads/{thread_id}/messages",
            json={**body, "content": "changed"},
            headers=message_headers,
        )
        assert changed.status_code == 409
        assert retries[0] == 1
        monkeypatch.setattr(
            app_module,
            "thread_store",
            ThreadStore(httpx.Client(transport=httpx.MockTransport(registry))),
        )
        assert client.get(f"/v1/ai/threads/{thread_id}", headers=alice).json() == thread
        threads = client.get("/v1/ai/threads", params={"corpus": "first"}, headers=alice).json()
        assert [row["id"] for row in threads["threads"]] == [thread_id]
        assert (
            client.get(
                "/v1/ai/threads", params={"corpus": "first", "cursor": "bad"}, headers=alice
            ).status_code
            == 422
        )
        listed = client.get(f"/v1/ai/threads/{thread_id}/messages", headers=alice).json()
        assert [message["content"] for message in listed["messages"]] == ["hello"]
        fork = client.post(
            f"/v1/ai/threads/{thread_id}/sections",
            json={"scope": scope},
            headers={**alice, "Idempotency-Key": FORK_KEY},
        )
        assert fork.status_code == 200 and len(fork.json()["sections"]) == 2
        rows[thread_id]["data"]["suggestions"].append(
            {
                "section_id": section_id,
                "created_at": thread["created_at"],
                "suggestion": {
                    "id": "suggestion-1",
                    "scope": scope,
                    "kind": "text",
                    "target_node": 1,
                    "diff": [{"field": "text", "old": "A", "new": "B"}],
                    "rationale": "Example",
                    "base_version": "1.0",
                    "content_hash": "sha256:test",
                },
            }
        )
        suggestions = client.get(f"/v1/ai/threads/{thread_id}/suggestions", headers=alice).json()
        assert suggestions["suggestions"][0]["suggestion"]["id"] == "suggestion-1"
        stale = client.post(
            "/v1/ai/threads",
            json={"scope": {**scope, "version": "stale"}},
            headers={**alice, "Idempotency-Key": "00000000-0000-4000-8000-000000000005"},
        )
        assert stale.status_code == 409 and stale.json()["code"] == "stale"
        draft = client.post(
            "/v1/ai/threads",
            json={"scope": {**scope, "corpus": "draft:" + DRAFT}},
            headers={**alice, "Idempotency-Key": "00000000-0000-4000-8000-000000000006"},
        )
        assert draft.status_code == 200, draft.text
        draft_scope = {**scope, "corpus": "draft:" + DRAFT}
        assert (
            client.post("/v1/ai/validate", json={"scope": draft_scope}, headers=alice).status_code
            == 200
        )
        assert (
            client.post("/v1/ai/validate", json={"scope": draft_scope}, headers=bob).status_code
            == 404
        )
        assert len(rows) == 2
        allowed.remove(ALICE)
        assert client.get(f"/v1/ai/threads/{thread_id}", headers=alice).status_code == 404
