import importlib
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from test_corpus_hub_publish import real_archive

from corpora_api import corpus_import_api as imports
from corpora_api import corpus_import_worker as worker
from corpora_api.corpus_metadata import CorpusMetadataNotFound

app_module = importlib.import_module("corpora_api.app")


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
    monkeypatch.setattr(app_module, "verify_user_token", lambda _: claims)
    monkeypatch.setattr(imports, "read_metadata", lambda *_: SimpleNamespace(revision=1))
    corpus, operation = uuid4(), uuid4()
    data = real_archive(tmp_path)
    calls, dispatches = [], []

    def rpc(self, name, params):
        calls.append(name)
        if name == "corpora_begin_import":
            return {
                "status": "claimed",
                "corpus_id": str(corpus),
                "operation_id": str(operation),
                "claim_id": str(uuid4()),
                "actor_id": claims["sub"],
                "session_id": claims["session_id"],
                "expected_sequence": 0,
                "expected_parent_sha": None,
                "hub_parent_sha": "a" * 40,
                "input_sha256": params["p_digest"],
                "input_format": "corpus",
                "repo_id": f"fixture/corpus-{corpus}",
                "repo_type": "dataset",
                "metadata_revision": 1,
                "retained_ref": "corpora-op-" + str(operation),
            }
        if name == "corpora_mark_import_uncertain":
            return True
        if name == "corpora_get_import":
            return {
                "status": "existing",
                "corpus_id": str(corpus),
                "operation_id": str(operation),
                "state": "completed",
                "sequence": 1,
            }
        pytest.fail("Unexpected RPC")

    monkeypatch.setattr(imports.CreationRPC, "call", rpc)
    monkeypatch.setattr(
        worker, "run_import_worker", lambda *args, **kwargs: dispatches.append(args)
    )
    return SimpleNamespace(
        corpus=corpus,
        operation=operation,
        data=data,
        calls=calls,
        dispatches=dispatches,
        path=f"/v1/corpora/{corpus}/imports",
        headers={"Authorization": "Bearer fixture"},
    )


def upload(api, fixture, *, data=None, name="tiny.corpus", headers=None):
    return api.post(
        fixture.path,
        headers=fixture.headers if headers is None else headers,
        data={"operation_id": str(fixture.operation), "expected_revision": "1"},
        files={"file": (name, fixture.data if data is None else data)},
    )


def test_validated_initial_import_dispatches_exact_bytes_once(configured):
    with TestClient(app_module.app) as api:
        result = upload(api, configured)
        status = api.get(
            configured.path + "/" + str(configured.operation), headers=configured.headers
        )
    assert result.status_code == 201 and result.json()["sequence"] == 1
    assert result.json() == status.json()
    assert result.headers["cache-control"] == "no-store"
    assert len(configured.dispatches) == 1
    assert configured.dispatches[0][1:] == ("tiny.corpus", configured.data)


@pytest.mark.parametrize(
    "kind,status", [("auth", 401), ("format", 415), ("invalid", 422), ("size", 413)]
)
def test_rejected_upload_never_admits_or_dispatches(configured, kind, status):
    kwargs = (
        {"headers": {}}
        if kind == "auth"
        else (
            {"name": "tiny.zip"}
            if kind == "format"
            else ({"data": b"broken"} if kind == "invalid" else {"data": b"x" * 3_000_001})
        )
    )
    with TestClient(app_module.app) as api:
        assert upload(api, configured, **kwargs).status_code == status
    assert configured.calls == configured.dispatches == []


def test_foreign_corpus_is_denied_before_validation(configured, monkeypatch):
    def deny(*_):
        raise CorpusMetadataNotFound("Corpus not found")

    monkeypatch.setattr(imports, "read_metadata", deny)
    monkeypatch.setattr(
        imports, "prepare_corpus_archive", lambda *_args, **_kw: pytest.fail("Validation")
    )
    with TestClient(app_module.app) as api:
        assert upload(api, configured).status_code == 404
    assert not configured.calls


def test_existing_import_does_not_dispatch_again(configured, monkeypatch):
    monkeypatch.setattr(
        imports.CreationRPC,
        "call",
        lambda *_: {
            "status": "existing",
            "corpus_id": str(configured.corpus),
            "operation_id": str(configured.operation),
            "state": "executing",
            "sequence": None,
        },
    )
    with TestClient(app_module.app) as api:
        assert upload(api, configured).status_code == 202
    assert not configured.dispatches


def test_lost_admission_acknowledgement_cannot_dispatch(configured, monkeypatch):
    from corpora_api.corpus_metadata import CorpusMetadataError

    def lost(*_):
        raise CorpusMetadataError("Lost acknowledgement")

    monkeypatch.setattr(imports.CreationRPC, "call", lost)
    with TestClient(app_module.app) as api:
        assert upload(api, configured).status_code == 503
    assert not configured.dispatches


def test_worker_failure_records_uncertainty_without_retry(configured, monkeypatch):
    def fail(*_args, **_kwargs):
        raise TimeoutError("Unknown remote outcome")

    monkeypatch.setattr(worker, "run_import_worker", fail)
    with TestClient(app_module.app) as api:
        response = upload(api, configured)
    assert response.status_code == 503
    assert configured.calls == ["corpora_begin_import", "corpora_mark_import_uncertain"]


def test_capacity_is_reserved_before_admission(configured):
    with worker.ImportSlot(), worker.ImportSlot(), TestClient(app_module.app) as api:
        assert upload(api, configured).status_code == 503
    assert not configured.calls and not configured.dispatches
