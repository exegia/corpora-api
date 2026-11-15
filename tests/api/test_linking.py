"""The reference adapter is authenticated and fails closed without an authority."""

from uuid import uuid4

import psycopg
from corpora_linking import Endpoint, Provenance, Reference, TextLocator
from fastapi.testclient import TestClient

from corpora_api import app as app_module
from corpora_api.app import app
from corpora_api.auth import UserAuthError
from corpora_api.linking_store import SQLiteReferenceStore, VersionConflictError


def test_linking_requires_user_and_explicit_configuration(monkeypatch):
    owner = str(uuid4())

    def verify(token):
        if token != "valid-user-token":
            raise UserAuthError("invalid")
        return {"sub": owner}

    monkeypatch.setattr(app_module, "verify_user_token", verify)
    monkeypatch.setenv("CORPORA_API_TOKEN", "service-token-placeholder")
    monkeypatch.delenv("LINKING_ENABLED", raising=False)
    monkeypatch.delattr(app.state, "linking_runtime", raising=False)
    path = f"/v1/linking/{uuid4()}/references/{uuid4()}"
    with TestClient(app) as client:
        assert client.get(path).status_code == 401
        assert (
            client.get(
                path, headers={"Authorization": "Bearer service-token-placeholder"}
            ).status_code
            == 401
        )
        response = client.get(path, headers={"Authorization": "Bearer valid-user-token"})
        assert response.status_code == 503
        assert response.json() == {"detail": "Reference linking is unavailable"}
        assert response.headers["cache-control"] == "private, no-store"
        schema = client.get("/v1/openapi.json").json()
    for route, method in (
        ("/v1/linking/{space_id}/references", "post"),
        ("/v1/linking/{space_id}/references/{reference_id}", "get"),
        ("/v1/linking/{space_id}/references/{reference_id}/{action}", "post"),
        ("/v1/linking/{space_id}/retrieve", "post"),
        ("/v1/linking/{space_id}/export", "post"),
        ("/v1/linking/{space_id}/publication/{reference_id}/{action}", "post"),
        ("/v1/linking/{space_id}/browser-selection", "post"),
    ):
        assert schema["paths"][route][method]["security"] == [{"SupabaseUserJWT": []}]


def test_linking_sanitizes_authority_failures(monkeypatch):
    monkeypatch.setattr(app_module, "verify_user_token", lambda _token: {"sub": str(uuid4())})

    class Runtime:
        def store(self, space_id, token):
            errors = {
                "session": UserAuthError("secret session diagnostic"),
                "grant": PermissionError("secret resource diagnostic"),
                "stale": VersionConflictError("secret version diagnostic"),
                "database": psycopg.OperationalError("secret database diagnostic"),
            }
            raise errors[token]

    monkeypatch.setattr(app.state, "linking_runtime", Runtime(), raising=False)
    path = f"/v1/linking/{uuid4()}/references/{uuid4()}"
    with TestClient(app) as client:
        for token, status in (("session", 401), ("grant", 403), ("stale", 409), ("database", 503)):
            response = client.get(path, headers={"Authorization": "Bearer " + token})
            assert response.status_code == status
            assert "secret" not in response.text


def test_reference_route_preserves_history_and_verified_creator(monkeypatch, tmp_path):
    actor = str(uuid4())
    monkeypatch.setattr(app_module, "verify_user_token", lambda _token: {"sub": actor})

    class Runtime:
        def store(self, _space_id, _token):
            return SQLiteReferenceStore(tmp_path / "references.sqlite3", actor_id=actor)

    monkeypatch.setattr(app.state, "linking_runtime", Runtime(), raising=False)
    selected = Endpoint(
        work_id="essay",
        edition_id="e",
        package_id="p",
        revision="1",
        document_id="d",
        locators=(TextLocator(stream_id="body", start=0, end=5, exact="Quote", suffix=" tail"),),
    )
    reference = Reference(
        source=selected,
        target=Endpoint(work_id="book"),
        provenance=Provenance(origin="manual", agent_id="forged", method="fixture"),
    )
    path = f"/v1/linking/{uuid4()}/references"
    body = {"reference": reference.model_dump(mode="json"), "reason": "create"}
    headers = {"Authorization": "Bearer valid-user-token"}
    with TestClient(app) as client:
        created = client.post(path, json=body, headers=headers)
        assert created.status_code == 200
        assert created.json()["reference"]["provenance"]["agent_id"] == actor
        reference_id = created.json()["reference"]["id"]
        rejected = client.post(
            f"{path}/{reference_id}/reject",
            json={"expected_version": 1, "reason": "review"},
            headers=headers,
        )
        assert rejected.status_code == 200 and rejected.json()["version"] == 2
        assert (
            client.post(path, json={**body, "expected_version": 1}, headers=headers).status_code
            == 409
        )
        history = client.get(f"{path}/{reference_id}", headers=headers)
        assert [row["version"] for row in history.json()["history"]] == [1, 2]
        assert history.headers["cache-control"] == "private, no-store"
