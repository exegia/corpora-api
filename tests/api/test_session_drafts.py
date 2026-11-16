from __future__ import annotations

from contextlib import nullcontext
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from corpora_api import app as app_module
from corpora_api import session_drafts
from corpora_api.session_read import SessionBoundRead


class Revoked(Exception):
    sqlstate = "P0001"
    diag = SimpleNamespace(message_primary="AUTH_SESSION_INVALID")


class Database:
    def __init__(self, revoke_on_check=0):
        self.checks = 0
        self.revoke_on_check = revoke_on_check

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def transaction(self):
        return nullcontext()

    def execute(self, query, *_args):
        if "corpora_stored_lock_session" in query:
            self.checks += 1
            if self.checks == self.revoke_on_check:
                raise Revoked()


def test_worker_result_is_released_only_after_final_session_check(monkeypatch):
    owner, session, draft = (str(uuid4()) for _ in range(3))
    claims = {"sub": owner, "session_id": session, "exp": 2_000_000_000}
    calls = []

    def worker(_store, actual_owner, actual_draft, view, params):
        calls.append((actual_owner, actual_draft, view, params))
        return {"draft": {"id": draft}, "data": {"name": "Mini"}}

    monkeypatch.setattr(session_drafts, "read_draft_timed", worker)
    db = Database()
    result = session_drafts.read_session_draft(
        SessionBoundRead(lambda: db), None, claims, draft, "manifest"
    )
    assert result["data"]["name"] == "Mini" and db.checks == 2
    assert calls == [(owner, draft, "manifest", {})]

    for revoke_on_check, expected_calls in ((1, 0), (2, 1)):
        calls.clear()
        db = Database(revoke_on_check)
        with pytest.raises(HTTPException) as denied:
            session_drafts.read_session_draft(
                SessionBoundRead(lambda db=db: db), None, claims, draft, "manifest"
            )
        assert denied.value.status_code == 401 and len(calls) == expected_calls


def test_all_draft_views_reject_a_revoked_session(monkeypatch):
    owner, session, draft = (str(uuid4()) for _ in range(3))
    claims = {"sub": owner, "session_id": session, "exp": 2_000_000_000}
    monkeypatch.setattr(app_module, "verify_user_token", lambda _token: claims)
    monkeypatch.setattr(
        app_module, "configured_draft_lease", lambda: SessionBoundRead(lambda: Database(1))
    )
    calls = []
    monkeypatch.setattr(session_drafts, "read_draft_timed", lambda *_args: calls.append(True))
    with TestClient(app_module.app) as api:
        for view in ("manifest", "index", "versions", "nodes/1", "sections", "content"):
            response = api.get(
                f"/v1/ai/drafts/{draft}/{view}", headers={"Authorization": "Bearer fixture"}
            )
            assert response.status_code == 401
    assert calls == []


def test_missing_database_configuration_fails_closed(monkeypatch):
    monkeypatch.delenv("CORPORA_SESSION_DB_HOST", raising=False)
    monkeypatch.setattr(app_module, "verify_user_token", lambda _token: {"sub": str(uuid4())})
    with TestClient(app_module.app) as api:
        response = api.get(
            f"/v1/ai/drafts/{uuid4()}/manifest", headers={"Authorization": "Bearer fixture"}
        )
    assert response.status_code == 503
