from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from corpora_api import app as app_module
from corpora_api import corpus_version_history_api as history
from corpora_api.corpus_metadata import CorpusMetadataError


@pytest.fixture
def history_fixture(monkeypatch):
    corpus, actor, session, operation = (uuid4() for _ in range(4))
    claims = {"sub": str(actor), "session_id": str(session), "exp": 2_000_000_000}
    version = {
        "sequence": 1,
        "commit_sha": "a" * 40,
        "parent_sha": None,
        "operation_id": str(operation),
        "actor_id": str(actor),
        "created_at": datetime.now(UTC).isoformat(),
    }
    page = {
        "status": "ok",
        "corpus_id": str(corpus),
        "current_sequence": 1,
        "versions": [version],
        "next_before": None,
    }
    calls = []

    class RPC:
        def __init__(self, **_):
            pass

        def call(self, name, params):
            calls.append((name, params))
            return page

    monkeypatch.setattr(history, "CreationRPC", RPC)
    monkeypatch.setattr(history, "require_creation_config", lambda: None)
    monkeypatch.setattr(app_module, "verify_user_token", lambda _: claims)
    return corpus, claims, page, calls


def test_version_route_requires_user_and_passes_verified_session(history_fixture):
    corpus, claims, page, calls = history_fixture
    api = TestClient(app_module.app)
    url = f"/v1/corpora/{corpus}/versions"
    assert api.get(url).status_code == 401
    response = api.get(url, headers={"Authorization": "Bearer user-token"}, params={"limit": 2})
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["versions"][0]["commit_sha"] == page["versions"][0]["commit_sha"]
    assert calls == [
        (
            "corpora_list_corpus_versions",
            {
                "p_actor": claims["sub"],
                "p_session": claims["session_id"],
                "p_token_exp": claims["exp"],
                "p_corpus": str(corpus),
                "p_before": None,
                "p_limit": 2,
            },
        )
    ]


@pytest.mark.parametrize("limit", [0, 101])
def test_version_route_rejects_unbounded_pages(history_fixture, limit):
    corpus, _, _, calls = history_fixture
    response = TestClient(app_module.app).get(
        f"/v1/corpora/{corpus}/versions",
        headers={"Authorization": "Bearer user-token"},
        params={"limit": limit},
    )
    assert response.status_code == 422 and not calls


@pytest.mark.parametrize("fault", ["wrong_corpus", "bad_order", "bad_cursor", "bad_parent"])
def test_version_page_rejects_invalid_identity_and_history(history_fixture, fault):
    corpus, claims, page, _ = history_fixture
    if fault == "wrong_corpus":
        page["corpus_id"] = str(uuid4())
    elif fault == "bad_order":
        page["versions"].append(page["versions"][0].copy())
    elif fault == "bad_cursor":
        page["next_before"] = 2
    else:
        page["versions"][0]["parent_sha"] = "b" * 40
    with pytest.raises(CorpusMetadataError):
        history.list_versions(claims, corpus, None, 10)


def test_not_found_does_not_expose_other_corpus(history_fixture):
    corpus, _, page, _ = history_fixture
    page.clear()
    page["status"] = "not_found"
    response = TestClient(app_module.app).get(
        f"/v1/corpora/{corpus}/versions", headers={"Authorization": "Bearer user-token"}
    )
    assert response.status_code == 404
