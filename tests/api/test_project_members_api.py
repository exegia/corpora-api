import importlib
import time
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from corpora_api import project_members_api as membership

app_module = importlib.import_module("corpora_api.app")


@pytest.fixture
def context(monkeypatch):
    owner, session, project, member = (uuid4() for _ in range(4))
    claims = {"sub": str(owner), "session_id": str(session), "exp": int(time.time()) + 3600}
    monkeypatch.setattr(app_module, "verify_user_token", lambda _: claims)
    return TestClient(app_module.app), project, member, claims


def test_membership_routes_bind_the_verified_user_and_project(context, monkeypatch):
    client, project, member, claims = context
    calls = []

    def rpc(_self, name, params):
        calls.append((name, params))
        assert params["p_actor"] == claims["sub"] and params["p_session"] == claims["session_id"]
        assert params["p_project"] == str(project) and params["p_member"] == str(member)
        return {
            "status": "ok",
            "project_id": str(project),
            "user_id": str(member),
            "present": params["p_present"],
        }

    monkeypatch.setattr(membership.CreationRPC, "call", rpc)
    path = f"/v1/projects/{project}/members/{member}"
    assert client.put(path).status_code == 401
    headers = {"Authorization": "Bearer valid"}
    added = client.put(path, headers=headers)
    assert added.status_code == 200 and added.json()["present"] is True
    assert added.headers["cache-control"] == "no-store"
    removed = client.delete(path, headers=headers)
    assert removed.status_code == 200 and removed.json()["present"] is False
    assert [params["p_present"] for _, params in calls] == [True, False]


def test_membership_pages_are_bounded_and_reject_invalid_receipts(context, monkeypatch):
    client, project, member, _ = context
    second = uuid4()
    first, last = sorted((member, second), key=lambda v: v.int)
    records = [{"user_id": str(x), "created_at": "2026-01-01T00:00:00Z"} for x in (first, last)]
    result = {"status": "ok", "project_id": str(project), "members": records}
    monkeypatch.setattr(membership.CreationRPC, "call", lambda *_: result)
    path = f"/v1/projects/{project}/members"
    headers = {"Authorization": "Bearer valid"}
    page = client.get(path, headers=headers, params={"limit": 1})
    assert page.status_code == 200 and page.json()["next_after"] == str(first)
    result["members"] = list(reversed(records))
    assert client.get(path, headers=headers, params={"limit": 1}).status_code == 503
    result["status"] = "not_found"
    result.pop("project_id")
    result.pop("members")
    assert client.get(path, headers=headers).status_code == 404
    assert client.get(path, headers=headers, params={"limit": 101}).status_code == 422


def test_member_email_lookup_is_bound_to_the_verified_owner(context, monkeypatch):
    client, project, member, claims = context

    def rpc(_self, name, params):
        assert name == "corpora_add_project_member_by_email"
        assert params["p_actor"] == claims["sub"] and params["p_project"] == str(project)
        assert params["p_email"] == "member@example.org"
        return {"status": "ok", "project_id": str(project), "user_id": str(member), "present": True}

    monkeypatch.setattr(membership.CreationRPC, "call", rpc)
    path = f"/v1/projects/{project}/members"
    headers = {"Authorization": "Bearer valid"}
    result = client.post(path, headers=headers, json={"email": " member@example.org "})
    assert result.status_code == 200 and result.json()["user_id"] == str(member)
    assert client.post(path, headers=headers, json={"email": "not-an-email"}).status_code == 422
