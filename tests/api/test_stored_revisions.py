from __future__ import annotations

import hashlib
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException

from corpora_api.stored_revisions import Revision, StoredRevisions


def test_stage_readback_and_published_registry_facts(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "http://example.test")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "test-key")
    owner, revision = str(uuid4()), str(uuid4())
    data = b"archive bytes"
    fact = Revision(
        owner=owner,
        filename="one.corpus",
        revision=revision,
        digest="sha256:" + hashlib.sha256(data).hexdigest(),
        version="v1.0",
    )
    objects, requests = {}, []
    wrong_owner = False

    def handler(request):
        requests.append((request.method, request.url.path))
        if "/storage/" in request.url.path:
            if request.method == "POST":
                assert request.headers["x-upsert"] == "false"
                if request.url.path in objects:
                    return httpx.Response(409)
                objects[request.url.path] = request.content
                return httpx.Response(200)
            return httpx.Response(200, content=objects[request.url.path])
        if request.url.path.endswith("corpora_stored_get"):
            return httpx.Response(
                200,
                json={
                    **fact.model_dump(mode="json"),
                    **({"owner": str(uuid4())} if wrong_owner else {}),
                },
            )
        if request.url.path.endswith("corpora_stored_publish"):
            return httpx.Response(400, json={"message": "ARCHIVE_CONFLICT"})
        return httpx.Response(204)

    store = StoredRevisions(httpx.Client(transport=httpx.MockTransport(handler)))
    store.publish(fact, data, None)
    assert store.get(owner, "one.corpus", revision).data == data
    store.stage(fact, data)  # A duplicate stage succeeds only after digest readback.
    objects[next(iter(objects))] = b"tampered"
    with pytest.raises(HTTPException) as error:
        store.stage(fact, data)
    assert error.value.status_code == 409
    objects[next(iter(objects))] = data
    with pytest.raises(HTTPException) as error:
        store.publish(fact, data, str(uuid4()))
    assert error.value.status_code == 409
    before = len(requests)
    with pytest.raises(HTTPException):
        store.publish(fact, data, "invalid revision")
    assert len(requests) == before
    wrong_owner = True
    with pytest.raises(HTTPException) as error:
        store.get(owner, "one.corpus", revision)
    assert error.value.status_code == 503
    assert requests[-1][1].endswith("corpora_stored_get")
