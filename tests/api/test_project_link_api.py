import importlib
import time
from uuid import uuid4

from fastapi.testclient import TestClient
from test_project_link_models import sample

from corpora_api import project_link_api as links

app_module = importlib.import_module("corpora_api.app")


def test_reference_routes_use_session_and_validate_receipts(monkeypatch):
    body, actor = sample()
    project, session = uuid4(), uuid4()
    claims = {"sub": str(actor), "session_id": str(session), "exp": int(time.time()) + 3600}
    monkeypatch.setattr(app_module, "verify_user_token", lambda _: claims)
    client = TestClient(app_module.app)
    reference = body["reference_id"]
    path = f"/v1/projects/{project}/references"
    headers = {"Authorization": "Bearer valid"}
    assert client.post(path, json=body).status_code == 401
    recorded = []

    def rpc(_self, name, params):
        recorded.append((name, params))
        assert params["p_actor"] == str(actor) and params["p_session"] == str(session)
        if name == "corpora_save_project_link":
            assert params["p_value"]["provenance"]["agent_id"] == str(actor)
            return {"status": "ok", "version": 1}
        if name == "corpora_decide_project_link":
            return {"status": "ok", "version": 2}
        return {"status": "ok", "revisions": []}

    monkeypatch.setattr(links.CreationRPC, "call", rpc)
    saved = client.post(path, headers=headers, json=body)
    assert saved.status_code == 200 and saved.json()["version"] == 1
    assert saved.headers["cache-control"] == "no-store"
    rejected = client.post(
        path + "/" + reference + "/reject",
        headers=headers,
        json={"expected_version": 1, "reason": "needs work"},
    )
    assert rejected.status_code == 200 and rejected.json()["version"] == 2
    history = client.get(path + "/" + reference + "/history", headers=headers)
    assert history.status_code == 200 and history.json()["revisions"] == []
    assert [entry[0] for entry in recorded] == [
        "corpora_save_project_link",
        "corpora_decide_project_link",
        "corpora_project_link_history",
    ]


def test_reference_routes_map_denial_conflict_and_invalid_transition(monkeypatch):
    body, actor = sample()
    project = uuid4()
    claims = {"sub": str(actor), "session_id": str(uuid4()), "exp": int(time.time()) + 3600}
    monkeypatch.setattr(app_module, "verify_user_token", lambda _: claims)
    client = TestClient(app_module.app)
    path = f"/v1/projects/{project}/references"
    headers = {"Authorization": "Bearer valid"}
    for status, code in (("not_found", 404), ("conflict", 409), ("invalid_transition", 409)):
        monkeypatch.setattr(links.CreationRPC, "call", lambda *_, result=status: {"status": result})
        assert client.post(path, headers=headers, json=body).status_code == code
