from __future__ import annotations

from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from corpora_api.stored_version_routes import stored_version_router


def test_proposed_mutation_routes_are_not_mounted_in_production_app():
    from corpora_api.app import app

    operations = {
        (route.path, method)
        for route in app.routes
        for method in (getattr(route, "methods", None) or ())
        if hasattr(route, "path")
    }
    assert ("/v1/storage/{filename}/manifest", "PATCH") not in operations
    assert ("/v1/storage/{filename}/nodes/{node}", "PATCH") not in operations
    assert ("/v1/storage/{filename}/restore", "POST") not in operations


def test_unmounted_route_contract_and_preconditions(monkeypatch):
    owner, head, next_head = str(uuid4()), str(uuid4()), str(uuid4())
    calls = []

    class Versions:
        def __init__(self, _store):
            pass

        def edit(self, user, filename, expected, operation, updates, node=None):
            calls.append((user, filename, expected, operation, updates, node))
            if expected != head:
                raise HTTPException(412, "Stored revision is stale")
            return {"revision": next_head, "version": "v1.1", "result": {"name": "Edited"}}

        def restore(self, user, filename, expected, selected):
            calls.append((user, filename, expected, selected))
            return {"revision": next_head, "version": "v1.1", "result": {"name": "Restored"}}

    monkeypatch.setattr("corpora_api.stored_version_routes.StoredVersions", Versions)
    monkeypatch.setattr(
        "corpora_api.stored_version_routes.stored_history",
        lambda _store, user, filename: {"head_revision": head, "versions": [{"id": "v1.0"}]},
    )
    app = FastAPI()

    @app.middleware("http")
    async def fake_identity(request: Request, call_next):
        if request.headers.get("Authorization") == "Bearer test-user":
            request.state.user_id = owner
        return await call_next(request)

    app.include_router(stored_version_router(object()))
    with TestClient(app) as client:
        endpoint = "/v1/storage/one.corpus"
        headers = {"Authorization": "Bearer test-user"}
        assert client.get(endpoint + "/versions").status_code == 401
        response = client.get(endpoint + "/versions", headers=headers)
        assert response.status_code == 200 and response.headers["ETag"] == f'"{head}"'
        assert response.headers["Cache-Control"] == "no-store"
        assert response.json()["head_revision"] == head
        assert (
            client.patch(
                endpoint + "/manifest", headers=headers, json={"name": "Edited"}
            ).status_code
            == 428
        )
        for invalid in (head, "*", 'W/"' + head + '"', '"' + head.upper() + '"'):
            assert (
                client.patch(
                    endpoint + "/manifest",
                    headers={**headers, "If-Match": invalid},
                    json={"name": "Edited"},
                ).status_code
                == 400
            )
        assert not calls
        patch = client.patch(
            endpoint + "/manifest",
            headers={**headers, "If-Match": f'"{head}"'},
            json={"name": "Edited"},
        )
        assert patch.status_code == 200 and patch.json() == {"name": "Edited"}
        assert patch.headers["ETag"] == f'"{next_head}"'
        assert calls[-1] == (owner, "one.corpus", head, "manifest", {"name": "Edited"}, None)
        stale = client.patch(
            endpoint + "/manifest",
            headers={**headers, "If-Match": f'"{next_head}"'},
            json={"name": "Another"},
        )
        assert stale.status_code == 412
        node = client.patch(
            endpoint + "/nodes/1",
            headers={**headers, "If-Match": f'"{head}"'},
            json={"note": "check"},
        )
        assert node.status_code == 200 and calls[-1][-1] == 1
        assert (
            client.patch(
                endpoint + "/nodes/0",
                headers={**headers, "If-Match": f'"{head}"'},
                json={"note": "check"},
            ).status_code
            == 422
        )
        restore = client.post(
            endpoint + "/restore",
            headers={**headers, "If-Match": f'"{head}"'},
            json={"revision": next_head},
        )
        assert restore.status_code == 200 and restore.json()["manifest"] == {"name": "Restored"}
        assert calls[-1] == (owner, "one.corpus", head, next_head)
