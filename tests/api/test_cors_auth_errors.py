"""Auth rejections must be readable by the allowed browser origins.

A 401/503 without ``Access-Control-Allow-Origin`` reaches the Web client as an
opaque CORS failure, so an expired session looks like a network error.
"""

import importlib
import time
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from corpora_api import auth
from corpora_api import project_corpora_api as corpora

app_module = importlib.import_module("corpora_api.app")

ALLOWED = "https://corpora.exegia.co"
UNTRUSTED = "https://untrusted.example"
PROJECT_CORPORA = f"/v1/projects/{uuid4()}/corpora"
DRAFTS = "/v1/ai/drafts"


def reject(_token: str) -> dict:
    raise auth.UserAuthError("invalid")


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(app_module, "verify_user_token", reject)
    monkeypatch.delenv("CORPORA_CORPUS_CREATION_ENABLED", raising=False)
    return TestClient(app_module.app)


def assert_cors(response, origin: str) -> None:
    assert response.headers["access-control-allow-origin"] == origin
    assert "origin" in response.headers["vary"].lower()


def test_missing_token_from_allowed_origin_is_readable(client):
    response = client.get(PROJECT_CORPORA, headers={"Origin": ALLOWED})
    assert response.status_code == 401
    assert_cors(response, ALLOWED)


def test_invalid_bearer_from_allowed_origin_is_readable(client):
    response = client.get(
        PROJECT_CORPORA, headers={"Origin": ALLOWED, "Authorization": "Bearer expired"}
    )
    assert response.status_code == 401
    assert_cors(response, ALLOWED)


@pytest.mark.parametrize(("unavailable", "status"), [(False, 401), (True, 503)])
def test_revoked_or_uncheckable_session_from_allowed_origin_is_readable(
    client, monkeypatch, unavailable, status
):
    def check(_token: str) -> bool:
        if unavailable:
            raise auth.SessionCheckUnavailable()
        return False

    monkeypatch.setattr(app_module, "verify_user_token", lambda _: {"sub": str(uuid4())})
    monkeypatch.setattr(app_module, "ingest_session_active", check)
    response = client.get(DRAFTS, headers={"Origin": ALLOWED, "Authorization": "Bearer revoked"})
    assert response.status_code == status
    assert_cors(response, ALLOWED)


def test_wrong_service_token_from_allowed_origin_is_readable(client, monkeypatch):
    monkeypatch.setenv("CORPORA_API_TOKEN", "service-token")
    response = client.post(
        "/v1/checks/structure",
        headers={"Origin": ALLOWED, "Authorization": "Bearer wrong"},
        json={"text": "x"},
    )
    assert response.status_code == 401
    assert_cors(response, ALLOWED)


@pytest.mark.parametrize("authorization", [None, "Bearer expired"])
def test_untrusted_origin_gets_no_cors_on_rejection(client, authorization):
    headers = {"Origin": UNTRUSTED} | ({"Authorization": authorization} if authorization else {})
    response = client.get(PROJECT_CORPORA, headers=headers)
    assert response.status_code == 401
    assert "access-control-allow-origin" not in response.headers


def test_preflight_is_unchanged(client):
    preflight = {
        "Access-Control-Request-Method": "GET",
        "Access-Control-Request-Headers": "authorization",
    }
    allowed = client.options(PROJECT_CORPORA, headers={"Origin": ALLOWED} | preflight)
    assert allowed.status_code == 200
    assert allowed.headers["access-control-allow-origin"] == ALLOWED
    assert "GET" in allowed.headers["access-control-allow-methods"]
    denied = client.options(PROJECT_CORPORA, headers={"Origin": UNTRUSTED} | preflight)
    assert denied.status_code == 400
    assert "access-control-allow-origin" not in denied.headers


def test_authenticated_request_still_succeeds_with_cors(client, monkeypatch):
    claims = {"sub": str(uuid4()), "session_id": str(uuid4()), "exp": int(time.time()) + 3600}
    monkeypatch.setattr(app_module, "verify_user_token", lambda _: claims)
    monkeypatch.setattr(corpora.CreationRPC, "call", lambda *_: {"status": "list", "rows": []})
    response = client.get(
        PROJECT_CORPORA, headers={"Origin": ALLOWED, "Authorization": "Bearer valid"}
    )
    assert response.status_code == 200
    assert_cors(response, ALLOWED)
