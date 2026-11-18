import importlib
import json
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


def test_resolve_verifies_both_passages_before_recording_decision(monkeypatch):
    from uuid import UUID

    from corpora_api.project_link_models import LinkSave, draft_reference

    body, actor = sample()
    # Both endpoints use exact Text-Fabric selections for this operation.
    body["target"] = body["source"].copy()
    project = uuid4()
    claims = {"sub": str(actor), "session_id": str(uuid4()), "exp": int(time.time()) + 3600}
    monkeypatch.setattr(app_module, "verify_user_token", lambda _: claims)
    reference = draft_reference(LinkSave.model_validate(body), UUID(str(actor))).model_dump(
        mode="json"
    )
    history = {
        "status": "ok",
        "revision": {
            "version": 1,
            "actor_id": str(actor),
            "recorded_at": "2026-01-01T00:00:00Z",
            "reason": "draft",
            "action": "edit",
            "reference": reference,
        },
    }
    calls = []

    def rpc(_self, name, params):
        calls.append(name)
        if name == "corpora_current_project_link":
            return history
        assert len(resolved) == 2
        return {"status": "ok", "version": 2}

    resolved = []

    async def verify(actual_claims, *, ref):
        assert actual_claims == claims
        resolved.append(ref)
        return {"node": 1}

    monkeypatch.setattr(links.CreationRPC, "call", rpc)
    monkeypatch.setattr(links, "resolve_corpus_reference", verify)
    client = TestClient(app_module.app)
    path = f"/v1/projects/{project}/references/{body['reference_id']}/resolve"
    response = client.post(
        path,
        headers={"Authorization": "Bearer valid"},
        json={"expected_version": 1, "reason": "verified"},
    )
    assert response.status_code == 200 and response.json()["version"] == 2
    assert calls == ["corpora_current_project_link", "corpora_decide_project_link"]
    assert len(resolved) == 2
    history["revision"]["reference"]["target"] = sample()[0]["target"]
    resolved.clear()
    calls.clear()
    assert (
        client.post(
            path,
            headers={"Authorization": "Bearer valid"},
            json={"expected_version": 1, "reason": "verified"},
        ).status_code
        == 422
    )
    assert calls == ["corpora_current_project_link"]


def test_project_member_retrieves_both_reference_selections(monkeypatch):
    from corpora_api.project_link_models import LinkSave, draft_reference

    body, actor = sample()
    project = uuid4()
    claims = {"sub": str(actor), "session_id": str(uuid4()), "exp": int(time.time()) + 3600}
    monkeypatch.setattr(app_module, "verify_user_token", lambda _: claims)
    value = draft_reference(LinkSave.model_validate(body), actor).model_dump(mode="json")
    revision = {
        "version": 1,
        "actor_id": str(actor),
        "recorded_at": "2026-01-01T00:00:00Z",
        "reason": "draft",
        "action": "edit",
        "reference": value,
    }
    calls = []

    def rpc(_self, name, params):
        calls.append(name)
        assert name == "corpora_read_current_project_link" and params["p_actor"] == str(actor)
        return {"status": "ok", "revision": revision}

    async def corpus(actual, *, ref):
        assert actual == claims and ref == body["source"]["locators"][0]["anchor_id"]
        return {"node": 1, "text": "Text-Fabric selection"}

    async def document(actual, selected, kind):
        assert actual == claims and selected.document_id == "sources/book.pdf" and kind == "pdf"
        return "PDF selection"

    monkeypatch.setattr(links.CreationRPC, "call", rpc)
    monkeypatch.setattr(links, "resolve_corpus_reference", corpus)
    monkeypatch.setattr(links, "retrieve_document", document)
    client = TestClient(app_module.app)
    path = f"/v1/projects/{project}/references/{body['reference_id']}/retrieve"
    assert client.post(path, json={"side": "source"}).status_code == 401
    headers = {"Authorization": "Bearer valid"}
    source = client.post(path, headers=headers, json={"side": "source"})
    target = client.post(path, headers=headers, json={"side": "target"})
    assert source.status_code == target.status_code == 200
    assert source.json()["fragments"] == ["Text-Fabric selection"]
    assert target.json()["fragments"] == ["PDF selection"]
    assert source.json()["version"] == 1 and source.headers["cache-control"] == "no-store"
    assert client.post(path, headers=headers, json={"side": "other"}).status_code == 422
    assert calls == ["corpora_read_current_project_link"] * 2
    monkeypatch.setattr(links.CreationRPC, "call", lambda *_: {"status": "not_found"})
    assert client.post(path, headers=headers, json={"side": "target"}).status_code == 404


def test_retrieval_bounds_the_encoded_response(monkeypatch):
    from corpora_api.project_link_models import LinkSave, draft_reference

    body, actor = sample()
    claims = {"sub": str(actor), "session_id": str(uuid4()), "exp": int(time.time()) + 3600}
    monkeypatch.setattr(app_module, "verify_user_token", lambda _: claims)
    value = draft_reference(LinkSave.model_validate(body), actor).model_dump(mode="json")
    revision = {
        "version": 1,
        "actor_id": str(actor),
        "recorded_at": "2026-01-01T00:00:00Z",
        "reason": "draft",
        "action": "edit",
        "reference": value,
    }
    monkeypatch.setattr(
        links.CreationRPC, "call", lambda *_: {"status": "ok", "revision": revision}
    )

    async def corpus(_claims, *, ref):
        return {"node": 1, "text": "\u0001" * 700_000}

    monkeypatch.setattr(links, "resolve_corpus_reference", corpus)
    client = TestClient(app_module.app)
    path = f"/v1/projects/{uuid4()}/references/{body['reference_id']}/retrieve"
    response = client.post(path, headers={"Authorization": "Bearer valid"}, json={"side": "source"})
    assert response.status_code == 413


def test_export_preserves_current_reference_provenance_and_requires_complete_access(monkeypatch):
    from corpora_api.project_link_models import LinkSave, draft_reference

    body, actor = sample()
    project = uuid4()
    claims = {"sub": str(actor), "session_id": str(uuid4()), "exp": int(time.time()) + 3600}
    monkeypatch.setattr(app_module, "verify_user_token", lambda _: claims)
    reference = draft_reference(LinkSave.model_validate(body), actor).model_dump(mode="json")
    revision = {
        "version": 2,
        "actor_id": str(actor),
        "recorded_at": "2026-01-01T00:00:00Z",
        "reason": "review",
        "action": "reject",
        "reference": reference,
    }
    calls = []

    def rpc(_self, name, params):
        calls.append((name, params))
        return {"status": "ok", "revisions": [revision]}

    monkeypatch.setattr(links.CreationRPC, "call", rpc)
    client = TestClient(app_module.app)
    path = f"/v1/projects/{project}/references/export"
    data = {"reference_ids": [body["reference_id"]]}
    assert client.post(path, json=data).status_code == 401
    headers = {"Authorization": "Bearer valid"}
    result = client.post(path, headers=headers, json=data)
    assert result.status_code == 200
    assert result.json()["revisions"][0]["reference"]["provenance"]["agent_id"] == str(actor)
    assert result.headers["content-disposition"].startswith("attachment;")
    assert result.headers["cache-control"] == "no-store"
    assert calls[0][0] == "corpora_export_project_links"
    assert (
        client.post(
            path, headers=headers, json={"reference_ids": data["reference_ids"] * 2}
        ).status_code
        == 422
    )
    assert client.post(path, headers=headers, json={"reference_ids": []}).status_code == 422
    monkeypatch.setattr(links.CreationRPC, "call", lambda *_: {"status": "not_found"})
    assert client.post(path, headers=headers, json=data).status_code == 404
    monkeypatch.setattr(links.CreationRPC, "call", lambda *_: {"status": "ok", "revisions": []})
    assert client.post(path, headers=headers, json=data).status_code == 503


def test_import_preview_is_read_only_and_validates_authorization_and_receipts(monkeypatch):
    from corpora_api.project_link_models import LinkSave, draft_reference

    body, actor = sample()
    project, source_project = uuid4(), uuid4()
    claims = {"sub": str(actor), "session_id": str(uuid4()), "exp": int(time.time()) + 3600}
    monkeypatch.setattr(app_module, "verify_user_token", lambda _: claims)
    reference = draft_reference(LinkSave.model_validate(body), actor).model_dump(mode="json")
    payload = {
        "schema_version": "1",
        "project_id": str(source_project),
        "revisions": [
            {
                "version": 2,
                "actor_id": str(actor),
                "recorded_at": "2026-01-01T00:00:00Z",
                "reason": "exported",
                "action": "edit",
                "reference": reference,
            }
        ],
    }
    calls = []

    def rpc(_self, name, params):
        calls.append((name, params))
        assert name == "corpora_preview_project_link_import"
        assert params["p_actor"] == str(actor)
        assert params["p_items"][0]["id"] == body["reference_id"]
        return {
            "status": "ok",
            "items": [
                {"reference_id": body["reference_id"], "outcome": "new", "current_version": None}
            ],
        }

    monkeypatch.setattr(links.CreationRPC, "call", rpc)
    client = TestClient(app_module.app)
    path = f"/v1/projects/{project}/references/import-preview"
    assert client.post(path, json=payload).status_code == 401
    headers = {"Authorization": "Bearer valid"}
    response = client.post(path, headers=headers, json=payload)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["items"][0]["outcome"] == "new"
    assert len(calls) == 1
    duplicate = {**payload, "revisions": payload["revisions"] * 2}
    assert client.post(path, headers=headers, json=duplicate).status_code == 422
    short = reference.copy()
    short["target"] = {**reference["target"], "package_id": None}
    invalid = {**payload, "revisions": [{**payload["revisions"][0], "reference": short}]}
    assert client.post(path, headers=headers, json=invalid).status_code == 422
    monkeypatch.setattr(links.CreationRPC, "call", lambda *_: {"status": "not_found"})
    assert client.post(path, headers=headers, json=payload).status_code == 404
    monkeypatch.setattr(links.CreationRPC, "call", lambda *_: {"status": "ok", "items": []})
    assert client.post(path, headers=headers, json=payload).status_code == 503


def test_import_records_unverified_source_claims_and_requires_valid_receipts(monkeypatch):
    from corpora_api.project_link_models import LinkSave, draft_reference

    body, actor = sample()
    project, source_project, claimed_actor = uuid4(), uuid4(), uuid4()
    claims = {"sub": str(actor), "session_id": str(uuid4()), "exp": int(time.time()) + 3600}
    monkeypatch.setattr(app_module, "verify_user_token", lambda _: claims)
    original = draft_reference(LinkSave.model_validate(body), claimed_actor).model_dump(mode="json")
    original["review"] = "approved"
    original["publication"] = "published"
    payload = {
        "reason": "Import reviewed links",
        "export": {
            "schema_version": "1",
            "project_id": str(source_project),
            "revisions": [
                {
                    "version": 7,
                    "actor_id": str(claimed_actor),
                    "recorded_at": "2026-01-01T00:00:00Z",
                    "reason": "original",
                    "action": "publish",
                    "reference": original,
                }
            ],
        },
    }
    captured = []

    def rpc(_self, name, params):
        assert name == "corpora_import_project_links"
        captured.append(params)
        return {
            "status": "ok",
            "items": [{"reference_id": body["reference_id"], "outcome": "imported", "version": 1}],
        }

    monkeypatch.setattr(links.CreationRPC, "call", rpc)
    client = TestClient(app_module.app)
    path = f"/v1/projects/{project}/references/import"
    headers = {"Authorization": "Bearer valid"}
    assert client.post(path, json=payload).status_code == 401
    response = client.post(path, headers=headers, json=payload)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    imported = captured[0]["p_items"][0]["value"]
    assert (imported["resolution"], imported["review"], imported["publication"]) == (
        "unresolved",
        "pending",
        "draft",
    )
    provenance = imported["provenance"]
    assert provenance["agent_id"] == str(actor) and provenance["method"] == "project-import"
    prefix = "Unverified import claim: "
    assert provenance["evidence"][0].startswith(prefix)
    claim = json.loads(provenance["evidence"][0][len(prefix) :])
    assert claim["source_project_id"] == str(source_project)
    assert claim["actor_id"] == str(claimed_actor) and claim["version"] == 7
    assert claim["provenance"] == original["provenance"]
    assert claim["publication"] == "published" and claim["review"] == "approved"
    monkeypatch.setattr(
        links.CreationRPC,
        "call",
        lambda *_: {
            "status": "ok",
            "items": [{"reference_id": body["reference_id"], "outcome": "imported", "version": 2}],
        },
    )
    assert client.post(path, headers=headers, json=payload).status_code == 503
    monkeypatch.setattr(links.CreationRPC, "call", lambda *_: {"status": "conflict"})
    assert client.post(path, headers=headers, json=payload).status_code == 409
    monkeypatch.setattr(links.CreationRPC, "call", lambda *_: {"status": "ok", "items": []})
    assert client.post(path, headers=headers, json=payload).status_code == 503
