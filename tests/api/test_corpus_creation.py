import importlib
import time
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from corpora_api import corpus_creation as creation
from corpora_api.corpus_hub_provision import VerifiedPrivateRepository
from corpora_api.corpus_metadata import CorpusMetadataError
from corpora_api.corpus_provision_dispatch import CorpusProvisionDispatcher

app_module = importlib.import_module("corpora_api.app")


@pytest.fixture
def configured(monkeypatch):
    for key, value in {
        "CORPORA_CORPUS_CREATION_ENABLED": "1",
        "HF_CORPUS_ORGANIZATION": "fixture",
        "HF_CORPUS_PROVISION_TOKEN": "test-token-placeholder",
        "SUPABASE_URL": "https://db.test",
        "SUPABASE_SERVICE_ROLE_KEY": "test-key-placeholder",
    }.items():
        monkeypatch.setenv(key, value)
    claims = {"sub": str(uuid4()), "session_id": str(uuid4()), "exp": int(time.time()) + 3600}
    monkeypatch.setattr(app_module, "verify_user_token", lambda _: claims)
    return claims


@pytest.mark.parametrize(
    "state,code",
    [("repository_created", 200), ("provisioning", 202), ("needs_reconciliation", 200)],
)
def test_http_retry_returns_existing_result_without_worker(monkeypatch, configured, state, code):
    corpus = str(uuid4())
    calls = []

    def rpc(_self, name, params):
        calls.append(name)
        assert params["p_actor"] == configured["sub"]
        return {
            "status": "existing",
            "id": corpus,
            "title": "Example",
            "state": state,
            "bootstrap_sha": "a" * 40 if state == "repository_created" else None,
        }

    monkeypatch.setattr(creation.CreationRPC, "call", rpc)
    with TestClient(app_module.app) as client:
        response = client.post(
            "/v1/corpora",
            headers={"Authorization": "Bearer fixture"},
            json={"id": corpus, "title": "Example"},
        )
    assert response.status_code == code
    assert response.json()["state"] == state
    assert set(response.json()) == {"id", "title", "state", "bootstrap_sha"}
    assert response.headers["cache-control"] == "no-store"
    assert calls == ["corpora_begin_creation"]


@pytest.mark.parametrize(
    "result,code", [({"status": "conflict"}, 409), ({"status": "not_found"}, 404)]
)
def test_http_domain_errors_are_generic(monkeypatch, configured, result, code):
    monkeypatch.setattr(creation.CreationRPC, "call", lambda *_: result)
    with TestClient(app_module.app) as client:
        response = client.post(
            "/v1/corpora",
            headers={"Authorization": "Bearer fixture"},
            json={"id": str(uuid4()), "title": "Example"},
        )
    assert response.status_code == code
    assert "repo" not in response.text


def test_missing_auth_is_rejected_before_body_validation():
    with TestClient(app_module.app) as client:
        assert client.post("/v1/corpora", json={}).status_code == 401
        assert client.get(f"/v1/corpora/{uuid4()}").status_code == 401


def test_disabled_creation_has_no_database_side_effect(monkeypatch, configured):
    monkeypatch.delenv("CORPORA_CORPUS_CREATION_ENABLED")
    monkeypatch.setattr(creation.CreationRPC, "call", lambda *_: pytest.fail("RPC called"))
    with TestClient(app_module.app) as client:
        response = client.post(
            "/v1/corpora",
            headers={"Authorization": "Bearer fixture"},
            json={"id": str(uuid4()), "title": "Example"},
        )
    assert response.status_code == 503


def test_client_cannot_supply_owner_repository_or_claim(monkeypatch, configured):
    monkeypatch.setattr(creation.CreationRPC, "call", lambda *_: pytest.fail("RPC called"))
    with TestClient(app_module.app) as client:
        for field in ("owner_id", "repo_id", "claim_id", "session_id"):
            response = client.post(
                "/v1/corpora",
                headers={"Authorization": "Bearer fixture"},
                json={"id": str(uuid4()), "title": "Example", field: "foreign"},
            )
            assert response.status_code == 422


def test_status_requires_current_session(monkeypatch, configured):
    def revoked(*_):
        raise HTTPException(401, "Session is no longer active")

    monkeypatch.setattr(creation.CreationRPC, "call", revoked)
    with TestClient(app_module.app) as client:
        response = client.get(f"/v1/corpora/{uuid4()}", headers={"Authorization": "Bearer fixture"})
    assert response.status_code == 401


@pytest.mark.parametrize("bad", [None, {}, {"status": "claimed"}])
def test_bad_admission_never_dispatches(monkeypatch, configured, bad):
    class RPC:
        def call(self, *_):
            return bad

    def worker(*_, **__):
        pytest.fail("Ambiguous or malformed admission dispatched a worker")

    dispatcher = CorpusProvisionDispatcher(
        creation.CreationAdmission(RPC(), "Example"), None, None, None, worker=worker
    )
    with pytest.raises(CorpusMetadataError):
        dispatcher.dispatch_trusted(configured, str(uuid4()))


def test_dispatch_uses_claim_once_and_returns_verified_bootstrap(monkeypatch, configured):
    corpus, operation, claim = uuid4(), uuid4(), uuid4()
    repo = f"fixture/corpus-{corpus}"
    calls = []
    metadata = {
        "id": str(corpus),
        "owner_id": configured["sub"],
        "title": "Example",
        "description": None,
        "language": None,
        "properties": {},
        "hf_repo_id": repo,
        "hf_repo_type": "dataset",
        "state": "provisioning",
        "revision": 1,
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
    }

    class RPC:
        def call(self, name, params):
            calls.append(name)
            if name == "corpora_begin_creation":
                return {
                    "status": "claimed",
                    "id": str(corpus),
                    "title": "Example",
                    "operation_id": str(operation),
                    "claim_id": str(claim),
                    "repo_id": repo,
                    "repo_type": "dataset",
                    "revision": 1,
                }
            if name == "corpora_observe_provision":
                return {
                    "metadata": metadata,
                    "operation": {
                        "id": str(operation),
                        "corpus_id": str(corpus),
                        "worker_claim": str(claim),
                        "repo_id": repo,
                        "repo_type": "dataset",
                        "state": "executing",
                    },
                }
            if name == "corpora_finalize_creation":
                raise CorpusMetadataError("lost commit acknowledgement")
            if name == "corpora_find_creation_result":
                return "a" * 40
            pytest.fail(name)

    def worker(*_, **__):
        calls.append("worker")
        return VerifiedPrivateRepository(corpus, 1, repo, "dataset", "a" * 40)

    rpc = RPC()
    outcomes = creation.CreationOutcomes(rpc)
    observer = creation.CorpusProvisionHubObserver(
        "test-token-placeholder",
        transport_factory=lambda: httpx.MockTransport(
            lambda _: httpx.Response(200, json={"id": repo, "private": True, "sha": "a" * 40})
        ),
    )
    dispatcher = CorpusProvisionDispatcher(
        creation.CreationAdmission(rpc, "Example"),
        outcomes,
        creation.CorpusProvisionReconciler(outcomes, observer),
        outcomes,
        worker=worker,
    )
    result = dispatcher.dispatch_trusted(configured, str(corpus))
    assert result.bootstrap_sha == "a" * 40
    assert calls.count("worker") == 1
    assert calls[-1] == "corpora_find_creation_result"


def test_rpc_maps_revoked_session_without_leaking_database_error(configured):
    rpc = creation.CreationRPC(
        lambda: httpx.MockTransport(
            lambda _: httpx.Response(
                400, json={"code": "P0001", "message": "AUTH_SESSION_INVALID", "details": "private"}
            )
        )
    )
    with pytest.raises(HTTPException) as caught:
        rpc.call("corpora_get_creation", {})
    assert caught.value.status_code == 401
    assert "private" not in caught.value.detail
