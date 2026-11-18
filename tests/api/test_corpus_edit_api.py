import hashlib
import importlib
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from test_corpus_hub_publish import real_archive

from corpora_api import corpus_edit_api as edits

app = importlib.import_module("corpora_api.app")


@pytest.fixture
def configured(monkeypatch, tmp_path):
    for name, value in {
        "CORPORA_CORPUS_CREATION_ENABLED": "1",
        "SUPABASE_URL": "https://db.test",
        "SUPABASE_SERVICE_ROLE_KEY": "fixture-placeholder",
        "HF_CORPUS_ORGANIZATION": "fixture",
        "HF_CORPUS_PROVISION_TOKEN": "fixture-placeholder",
    }.items():
        monkeypatch.setenv(name, value)
    claims = {"sub": str(uuid4()), "session_id": str(uuid4()), "exp": int(time.time()) + 3600}
    monkeypatch.setattr(app, "verify_user_token", lambda _: claims)
    corpus, operation = uuid4(), uuid4()
    data = real_archive(tmp_path)
    head = SimpleNamespace(
        sequence=1,
        commit_sha="a" * 40,
        archive_size=len(data),
        archive_sha256=hashlib.sha256(data).hexdigest(),
    )
    row = SimpleNamespace(state="ready", revision=2)
    monkeypatch.setattr(edits, "read_access", lambda *_: SimpleNamespace(head=head, row=row))
    calls, dispatches = [], []

    async def transfer(*_args, sink, **_kwargs):
        calls.append("download")
        await sink(data)

    monkeypatch.setattr(edits.hub_reader, "transfer_authorized", transfer)

    def rpc(_self, name, params):
        calls.append(name)
        if name == "corpora_get_edit_intent":
            return {"status": "absent"}
        if name == "corpora_begin_corpus_edit":
            return {
                "status": "claimed",
                "corpus_id": str(corpus),
                "operation_id": str(operation),
                "claim_id": str(uuid4()),
                "actor_id": claims["sub"],
                "session_id": claims["session_id"],
                "expected_sequence": 1,
                "expected_parent_sha": head.commit_sha,
                "hub_parent_sha": head.commit_sha,
                "input_sha256": params["p_digest"],
                "input_format": "corpus",
                "repo_id": f"fixture/corpus-{corpus}",
                "repo_type": "dataset",
                "metadata_revision": 2,
                "retained_ref": "corpora-op-" + str(operation),
            }
        if name == "corpora_mark_version_save_uncertain":
            return True
        pytest.fail("Unexpected RPC " + name)

    monkeypatch.setattr(edits.CreationRPC, "call", rpc)
    monkeypatch.setattr(edits, "run_import_worker", lambda *args, **kwargs: dispatches.append(args))
    return SimpleNamespace(
        corpus=corpus,
        operation=operation,
        head=head,
        data=data,
        calls=calls,
        dispatches=dispatches,
        headers={"Authorization": "Bearer fixture"},
        body={"operation_id": str(operation), "expected_sequence": 1, "expected_commit": "a" * 40},
    )


def test_real_node_edit_dispatches_validated_changed_archive(configured):
    from corpus_validate import read_archive

    with TestClient(app.app) as api:
        result = api.patch(
            f"/v1/corpora/{configured.corpus}/nodes/1",
            headers=configured.headers,
            json={**configured.body, "features": {"text": "Changed "}},
        )
    assert result.status_code == 201, result.text
    assert result.json()["sequence"] == 2
    assert result.headers["cache-control"] == "no-store"
    assert configured.calls == ["corpora_get_edit_intent", "download", "corpora_begin_corpus_edit"]
    assert len(configured.dispatches) == 1
    assert read_archive(configured.dispatches[0][2], "node", node=1)["text"] == "Changed "


def test_completed_retry_never_downloads_or_dispatches(configured, monkeypatch):
    monkeypatch.setattr(
        edits.CreationRPC,
        "call",
        lambda *_: {
            "status": "existing",
            "corpus_id": str(configured.corpus),
            "operation_id": str(configured.operation),
            "state": "completed",
            "sequence": 2,
        },
    )
    with TestClient(app.app) as api:
        result = api.patch(
            f"/v1/corpora/{configured.corpus}/manifest",
            headers=configured.headers,
            json={**configured.body, "changes": {"name": "new"}},
        )
    assert result.status_code == 200
    assert not configured.calls and not configured.dispatches


def test_stale_or_unsigned_edit_never_dispatches(configured):
    path = f"/v1/corpora/{configured.corpus}/manifest"
    with TestClient(app.app) as api:
        assert (
            api.patch(path, json={**configured.body, "changes": {"name": "new"}}).status_code == 401
        )
        response = api.patch(
            path,
            headers=configured.headers,
            json={**configured.body, "expected_sequence": 2, "changes": {"name": "new"}},
        )
    assert response.status_code == 409
    assert configured.calls == ["corpora_get_edit_intent", "corpora_get_edit_intent"]
    assert not configured.dispatches


def test_failure_after_dispatch_records_uncertainty(configured, monkeypatch):
    def fail(*_args, **_kwargs):
        raise TimeoutError("uncertain")

    monkeypatch.setattr(edits, "run_import_worker", fail)
    with TestClient(app.app) as api:
        response = api.patch(
            f"/v1/corpora/{configured.corpus}/manifest",
            headers=configured.headers,
            json={**configured.body, "changes": {"name": "new"}},
        )
    assert response.status_code == 503
    assert configured.calls[-1] == "corpora_mark_version_save_uncertain"


def test_restore_saves_historical_bytes_as_a_new_version(configured, monkeypatch):
    original = edits.CreationRPC.call

    def rpc(self, name, params):
        if name == "corpora_get_restore_source":
            configured.calls.append(name)
            assert params["p_sequence"] == 1
            return {
                "status": "ok",
                "sequence": 1,
                "commit_sha": "b" * 40,
                "retained_ref": "corpora-op-" + str(uuid4()),
                "archive_size": len(configured.data),
                "archive_sha256": hashlib.sha256(configured.data).hexdigest(),
            }
        if name == "corpora_begin_corpus_edit":
            assert params["p_kind"] == "restore" and params["p_restore"] == 1
        return original(self, name, params)

    monkeypatch.setattr(edits.CreationRPC, "call", rpc)
    with TestClient(app.app) as api:
        result = api.post(
            f"/v1/corpora/{configured.corpus}/versions/1/restore",
            headers=configured.headers,
            json=configured.body,
        )
    assert result.status_code == 201, result.text
    assert result.json()["sequence"] == 2
    assert configured.calls == [
        "corpora_get_edit_intent",
        "corpora_get_restore_source",
        "download",
        "corpora_begin_corpus_edit",
    ]
    assert configured.dispatches[0][2] == configured.data


def test_corrupt_source_never_admits_a_save(configured, monkeypatch):
    async def corrupt(*_args, sink, **_kwargs):
        await sink(b"tampered")

    monkeypatch.setattr(edits.hub_reader, "transfer_authorized", corrupt)
    with TestClient(app.app) as api:
        response = api.patch(
            f"/v1/corpora/{configured.corpus}/manifest",
            headers=configured.headers,
            json={**configured.body, "changes": {"name": "new"}},
        )
    assert response.status_code == 503
    assert configured.calls == ["corpora_get_edit_intent"]
    assert not configured.dispatches


def test_same_operation_with_different_request_is_conflict(configured, monkeypatch):
    monkeypatch.setattr(edits.CreationRPC, "call", lambda *_: {"status": "conflict"})
    with TestClient(app.app) as api:
        response = api.patch(
            f"/v1/corpora/{configured.corpus}/manifest",
            headers=configured.headers,
            json={**configured.body, "changes": {"name": "new"}},
        )
    assert response.status_code == 409
    assert not configured.calls and not configured.dispatches


def test_concurrent_identical_retry_finds_completed_edit_after_head_moves(configured, monkeypatch):
    configured.head.sequence = 2
    configured.head.commit_sha = "b" * 40
    replies = iter(
        [
            {"status": "absent"},
            {
                "status": "existing",
                "corpus_id": str(configured.corpus),
                "operation_id": str(configured.operation),
                "state": "completed",
                "sequence": 2,
            },
        ]
    )
    monkeypatch.setattr(edits.CreationRPC, "call", lambda *_: next(replies))
    with TestClient(app.app) as api:
        result = api.patch(
            f"/v1/corpora/{configured.corpus}/manifest",
            headers=configured.headers,
            json={**configured.body, "changes": {"name": "new"}},
        )
    assert result.status_code == 200 and result.json()["sequence"] == 2
    assert not configured.calls and not configured.dispatches
