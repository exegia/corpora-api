"""Draft object and registry operations preserve owner and digest boundaries."""

import io
import json
import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import httpx
import pytest
from cfabric import Fabric as ContextFabric
from fastapi import HTTPException
from fastapi.testclient import TestClient
from tf.fabric import Fabric

from corpora_api import app as app_module
from corpora_api import drafts as drafts_module
from corpora_api.app import app
from corpora_api.auth import UserAuthError
from corpora_api.drafts import DraftHead, DraftStore
from corpora_api.validation import ValidationBridge


def test_draft_view_limits_the_complete_response_envelope(monkeypatch):
    owner, document, revision = (str(uuid4()) for _ in range(3))
    head = DraftHead(
        document_id=document,
        owner=owner,
        corpus=f"draft:{document}",
        bucket="corpus-ai-drafts",
        object_key=f"{owner}/ai/{document}/{revision}.corpus",
        revision=revision,
        digest="sha256:" + "a" * 64,
        version="1." + "0" * 101_000,
    )
    store = DraftStore()
    monkeypatch.setattr(store, "resolve", lambda *_: head)
    monkeypatch.setattr(store, "_download", lambda *_: b"archive")
    monkeypatch.setattr(drafts_module, "run_corpus", lambda *_, **__: {"text": "x" * 3_899_000})
    with pytest.raises(HTTPException) as caught:
        store.read(owner, document, "node", node=1)
    assert caught.value.status_code == 413


def test_concurrent_draft_creation_reuses_the_same_immutable_object(monkeypatch):
    owner, job = str(uuid4()), str(uuid4())
    barrier = threading.Barrier(2)
    lock = threading.Lock()
    objects, heads = {}, {}
    monkeypatch.setenv("SUPABASE_URL", "https://fixture.invalid")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "fixture-key")
    monkeypatch.setattr(drafts_module, "read_archive", lambda *_: {"version": "1.0"})

    def transport(request):
        key = request.url.path
        if request.method == "POST":
            assert request.headers["x-upsert"] == "false"
            barrier.wait(timeout=10)
            with lock:
                if key in objects:
                    return httpx.Response(409)
                objects[key] = request.content
                return httpx.Response(201)
        if request.method == "GET":
            with lock:
                return httpx.Response(200, content=objects[key])
        raise AssertionError("unexpected storage request")

    store = DraftStore(httpx.Client(transport=httpx.MockTransport(transport)))

    def missing(*_):
        raise HTTPException(404, "Draft not found")

    def rpc(action, principal, payload):
        if action == "capabilities":
            return {"draft_api": 1}
        assert action == "provision"
        with lock:
            if not heads:
                heads.update(
                    {
                        **{
                            key: value
                            for key, value in payload.items()
                            if key not in ("name", "job_id")
                        },
                        "owner": principal,
                        "state": "draft",
                    }
                )
            return dict(heads)

    monkeypatch.setattr(store, "resolve", missing)
    monkeypatch.setattr(store, "_rpc", rpc)

    class Jobs:
        def fetch(self, *_):
            return "fixture.corpus", b"same verified bytes"

    class Validator:
        def validate(self, *_):
            return {"valid": True}

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: store.create(owner, job, Jobs(), Validator()), range(2)))
    assert results[0] == results[1]
    assert len(objects) == 1


def test_private_draft_lifecycle_survives_new_store_and_detects_tampering(monkeypatch, tmp_path):
    owner, foreign, job = str(uuid4()), str(uuid4()), str(uuid4())
    objects, heads = {}, {}
    root = tmp_path / "tf"
    root.mkdir()
    assert Fabric(locations=str(root), silent="deep").save(
        nodeFeatures={
            "otype": {1: "word", 2: "word", 3: "book"},
            "book": {3: "Mini"},
            "text": {1: "A", 2: "B"},
        },
        edgeFeatures={"oslots": {3: {1, 2}}},
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
    assert ContextFabric(locations=str(root), silent="deep").loadAll(silent="deep")
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.yml", "name: Mini\nversion: '1.0'\nformat: corpus\n")
        for path in root.rglob("*"):
            if path.is_file():
                archive.write(path, f"corpora/{path.relative_to(root)}")
    archive_data = stream.getvalue()

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer placeholder-service-key"
        if request.url.path.endswith("/rpc/corpora_ai_storage"):
            body = json.loads(request.content)
            action, user, payload = body["p_action"], body["p_owner"], body["p_payload"]
            if action == "capabilities":
                return httpx.Response(200, json={"draft_api": 1})
            if action == "resolve":
                row = heads.get((user, payload["corpus"]))
                return (
                    httpx.Response(200, json=row) if row else httpx.Response(200, content=b"null")
                )
            if action == "provision":
                row = {**payload, "owner": user, "state": "draft"}
                row.pop("job_id")
                row.pop("name")
                heads.setdefault((user, row["corpus"]), row)
                return httpx.Response(200, json=heads[(user, row["corpus"])])
            if action == "drafts":
                return httpx.Response(200, json=[v for (u, _), v in heads.items() if u == user])
            raise AssertionError(action)
        key = request.url.path.removeprefix("/storage/v1/object/corpus-ai-drafts/")
        if request.method == "POST":
            assert request.headers["x-upsert"] == "false"
            if key in objects:
                return httpx.Response(409)
            objects[key] = request.content
            return httpx.Response(200)
        if request.method == "GET":
            return (
                httpx.Response(200, content=objects[key]) if key in objects else httpx.Response(404)
            )
        raise AssertionError(request.method)

    class Jobs:
        def fetch(self, user, job_id):
            if user != owner or job_id != job:
                raise HTTPException(status_code=404)
            return "source.corpus", archive_data

    monkeypatch.setenv("SUPABASE_URL", "https://supabase.example.test")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "placeholder-service-key")
    client = httpx.Client(transport=httpx.MockTransport(respond))
    validator = ValidationBridge()
    first = DraftStore(client).create(owner, job, Jobs(), validator)
    assert DraftStore(client).create(owner, job, Jobs(), validator) == first
    second = DraftStore(client)
    assert second.list(owner, 50, 0)["drafts"] == [first]
    assert second._download(second.resolve(owner, first["id"])) == archive_data
    monkeypatch.setattr(app_module, "draft_store", second)
    monkeypatch.setattr(app_module, "conversion_artifact_store", Jobs())
    monkeypatch.setattr(app_module, "validator", validator)

    def verify(token):
        if token in (owner, foreign):
            return {"sub": token}
        raise UserAuthError("invalid")

    monkeypatch.setattr(app_module, "verify_user_token", verify)
    with TestClient(app) as api:
        own_headers = {"Authorization": f"Bearer {owner}"}
        foreign_headers = {"Authorization": f"Bearer {foreign}"}
        created = api.post("/v1/ai/drafts", json={"job_id": job}, headers=own_headers)
        foreign_create = api.post("/v1/ai/drafts", json={"job_id": job}, headers=foreign_headers)
        listed = api.get("/v1/ai/drafts", headers=own_headers)
        info = api.get(f"/v1/ai/drafts/{first['id']}", headers=own_headers)
        exported = api.get(f"/v1/ai/drafts/{first['id']}/archive", headers=own_headers)
        views = {
            view: api.get(f"/v1/ai/drafts/{first['id']}/{view}", headers=own_headers)
            for view in ("manifest", "index", "versions", "nodes/1", "sections", "content")
        }
        foreign_view = api.get(f"/v1/ai/drafts/{first['id']}/manifest", headers=foreign_headers)
        missing_node = api.get(f"/v1/ai/drafts/{first['id']}/nodes/999", headers=own_headers)
        invalid_node = api.get(f"/v1/ai/drafts/{first['id']}/nodes/0", headers=own_headers)
        invalid_limit = api.get(
            f"/v1/ai/drafts/{first['id']}/sections?limit=201", headers=own_headers
        )
        other = api.get(f"/v1/ai/drafts/{first['id']}", headers=foreign_headers)
        unauthenticated = api.get("/v1/ai/drafts")
    assert created.json() == first
    assert listed.json()["drafts"] == [first]
    assert info.json() == first
    assert exported.content == archive_data
    assert exported.headers["cache-control"] == "private, no-store"
    assert exported.headers["etag"] == f'"{first["digest"]}"'
    assert exported.headers["x-draft-revision"] == first["revision"]
    assert all(value.status_code == 200 for value in views.values())
    assert all(value.json()["draft"]["revision"] == first["revision"] for value in views.values())
    assert views["manifest"].json()["data"]["name"] == "Mini"
    assert views["nodes/1"].json()["data"]["node"] == 1
    assert foreign_view.status_code == 404
    assert (missing_node.status_code, invalid_node.status_code, invalid_limit.status_code) == (
        404,
        422,
        422,
    )
    assert (other.status_code, foreign_create.status_code, unauthenticated.status_code) == (
        404,
        404,
        401,
    )
    with pytest.raises(HTTPException) as absent:
        second.resolve(foreign, first["id"])
    assert absent.value.status_code == 404
    objects[next(iter(objects))] = b"tampered"
    with pytest.raises(HTTPException) as mismatch:
        second._download(second.resolve(owner, first["id"]))
    assert mismatch.value.status_code == 409
