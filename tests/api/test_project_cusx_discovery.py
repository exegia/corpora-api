"""C-USX discoveries preserve exact source selections."""

import importlib
import time
from uuid import uuid4

from fastapi.testclient import TestClient
from test_project_cusx_verify import case

from corpora_api import project_discovery_api as discovery

app_module = importlib.import_module("corpora_api.app")


def test_cusx_discovery_requires_verified_source_quote(monkeypatch):
    _, endpoint, _ = case()
    actor, project, discovery_id = uuid4(), uuid4(), uuid4()
    claims = {"sub": str(actor), "session_id": str(uuid4()), "exp": int(time.time()) + 3600}
    monkeypatch.setattr(app_module, "verify_user_token", lambda _: claims)
    calls = []

    def rpc(name, _params):
        calls.append(name)
        if name == "corpora_admit_project_discovery":
            return {"status": "ok", "exists": False}
        assert name == "corpora_register_project_discovery"
        return {"status": "ok", "version": 1}

    async def retrieve(actual, selected, kind):
        assert actual == claims and selected.document_id == "sources/book.cusx" and kind == "cusx"
        return "Selected words"

    monkeypatch.setattr(discovery, "_call", rpc)
    monkeypatch.setattr(discovery, "retrieve_document", retrieve)
    client = TestClient(app_module.app)
    path = f"/v1/projects/{project}/discoveries"
    body = {
        "id": str(discovery_id),
        "source": endpoint,
        "quote": "Selected words",
        "cited_name": "Example citation",
        "reason": "review",
    }
    headers = {"Authorization": "Bearer valid"}
    assert client.post(path, json=body).status_code == 401
    assert client.post(path, headers=headers, json=body).status_code == 200
    assert calls == ["corpora_admit_project_discovery", "corpora_register_project_discovery"]
    calls.clear()
    assert (
        client.post(path, headers=headers, json={**body, "quote": "Not selected"}).status_code
        == 422
    )
    assert calls == ["corpora_admit_project_discovery"]
