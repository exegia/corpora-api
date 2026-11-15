"""Draft object and registry operations preserve owner and digest boundaries."""

import io
import json
import zipfile
from uuid import uuid4

import httpx
import pytest
from cfabric import Fabric as ContextFabric
from fastapi import HTTPException
from fastapi.testclient import TestClient
from tf.fabric import Fabric

from corpora_api import app as app_module
from corpora_api.app import app
from corpora_api.auth import UserAuthError
from corpora_api.drafts import DraftStore
from corpora_api.validation import ValidationBridge


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
        listed = api.get("/v1/ai/drafts", headers=own_headers)
        info = api.get(f"/v1/ai/drafts/{first['id']}", headers=own_headers)
        exported = api.get(f"/v1/ai/drafts/{first['id']}/archive", headers=own_headers)
        other = api.get(f"/v1/ai/drafts/{first['id']}", headers=foreign_headers)
        unauthenticated = api.get("/v1/ai/drafts")
    assert created.json() == first
    assert listed.json()["drafts"] == [first]
    assert info.json() == first
    assert exported.content == archive_data
    assert exported.headers["cache-control"] == "private, no-store"
    assert (other.status_code, unauthenticated.status_code) == (404, 401)
    with pytest.raises(HTTPException) as absent:
        second.resolve(foreign, first["id"])
    assert absent.value.status_code == 404
    objects[next(iter(objects))] = b"tampered"
    with pytest.raises(HTTPException) as mismatch:
        second._download(second.resolve(owner, first["id"]))
    assert mismatch.value.status_code == 409
