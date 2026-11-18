import asyncio
import importlib
import json
import time
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from corpora_api import corpus_access_api as access
from corpora_api.corpus_metadata import CorpusMetadataError

app_module = importlib.import_module("corpora_api.app")


@pytest.fixture
def configured(monkeypatch):
    for key, value in {
        "SUPABASE_URL": "https://db.example.test",
        "SUPABASE_SERVICE_ROLE_KEY": "test-placeholder",
        "HF_CORPUS_ORGANIZATION": "fixture",
        "HF_CORPUS_PROVISION_TOKEN": "test-placeholder",
        "CORPORA_CORPUS_CREATION_ENABLED": "1",
    }.items():
        monkeypatch.setenv(key, value)
    claims = {"sub": str(uuid4()), "session_id": str(uuid4()), "exp": int(time.time()) + 3600}
    monkeypatch.setattr(app_module, "verify_user_token", lambda _: claims)
    return uuid4(), uuid4()


def test_deletion_verifies_private_identity_then_delete_then_absence(configured):
    corpus, _ = configured
    calls = []

    def handler(request):
        calls.append(request.method)
        assert request.url.host == "huggingface.co"
        assert request.headers["authorization"] == "Bearer test-placeholder"
        if calls == ["GET"]:
            return httpx.Response(
                200,
                stream=httpx.ByteStream(
                    json.dumps({"id": f"fixture/corpus-{corpus}", "private": True}).encode()
                ),
            )
        if request.method == "DELETE":
            assert json.loads(request.content) == {
                "organization": "fixture",
                "name": f"corpus-{corpus}",
                "type": "dataset",
            }
            return httpx.Response(200, stream=httpx.ByteStream(b"{}"))
        return httpx.Response(404, stream=httpx.ByteStream(b"{}"))

    asyncio.run(
        access.PrivateHubLifecycle(lambda: httpx.MockTransport(handler)).operate(
            corpus, f"fixture/corpus-{corpus}", "dataset", delete=True
        )
    )
    assert calls == ["GET", "DELETE", "GET"]


@pytest.mark.parametrize(
    "failure",
    [
        "missing",
        "foreign",
        "public",
        "redirect",
        "compressed",
        "large",
        "delete",
        "still_present",
        "lost_ack",
    ],
)
def test_unconfirmed_deletion_is_not_retried(configured, failure):
    corpus, _ = configured
    calls = []

    def handler(request):
        calls.append(request.method)
        if request.method == "DELETE":
            if failure == "lost_ack":
                raise httpx.ReadTimeout("lost acknowledgement")
            return httpx.Response(
                500 if failure == "delete" else 200, stream=httpx.ByteStream(b"{}")
            )
        if len(calls) > 1:
            return httpx.Response(200, stream=httpx.ByteStream(b"{}"))
        value = {"id": f"fixture/corpus-{corpus}", "private": True}
        if failure == "foreign":
            value["id"] = "foreign/repo"
        if failure == "public":
            value["private"] = False
        code = {"missing": 404, "redirect": 302}.get(failure, 200)
        body = b"x" * 65537 if failure == "large" else json.dumps(value).encode()
        headers = {"content-encoding": "gzip"} if failure == "compressed" else {}
        return httpx.Response(code, headers=headers, stream=httpx.ByteStream(body))

    with pytest.raises(CorpusMetadataError):
        asyncio.run(
            access.PrivateHubLifecycle(lambda: httpx.MockTransport(handler)).operate(
                corpus, f"fixture/corpus-{corpus}", "dataset", delete=True
            )
        )
    assert calls.count("DELETE") <= 1
    assert len(calls) == (
        3 if failure == "still_present" else 2 if failure in {"delete", "lost_ack"} else 1
    )


def test_deadline_bounds_stalled_hub_response(configured):
    corpus, _ = configured
    closed = []

    class Stalled(httpx.AsyncByteStream):
        async def __aiter__(self):
            await asyncio.sleep(30)
            yield b"{}"

        async def aclose(self):
            closed.append(True)

    async def handler(_):
        return httpx.Response(200, stream=Stalled())

    started = time.monotonic()
    with pytest.raises(CorpusMetadataError):
        asyncio.run(
            access.PrivateHubLifecycle(lambda: httpx.MockTransport(handler), timeout=0.02).operate(
                corpus, f"fixture/corpus-{corpus}", "dataset", delete=True
            )
        )
    assert time.monotonic() - started < 1 and closed


@pytest.mark.parametrize("state", ["executing", "uncertain", "completed"])
def test_existing_deletion_never_dispatches_again(configured, monkeypatch, state):
    corpus, operation = configured
    monkeypatch.setattr(
        access.CreationRPC,
        "call",
        lambda *_: {
            "status": "existing",
            "corpus_id": str(corpus),
            "operation_id": str(operation),
            "state": state,
        },
    )
    monkeypatch.setattr(access, "PrivateHubLifecycle", lambda: pytest.fail("Repeated deletion"))
    with TestClient(app_module.app) as api:
        r = api.request(
            "DELETE",
            f"/v1/corpora/{corpus}",
            headers={"Authorization": "Bearer test"},
            json={"operation_id": str(operation), "expected_revision": 1},
        )
    assert r.status_code == (200 if state == "completed" else 202)


def test_lost_deletion_admission_ack_never_dispatches(configured, monkeypatch):
    corpus, operation = configured

    def lost(*_):
        raise CorpusMetadataError("Unconfirmed admission")

    monkeypatch.setattr(access.CreationRPC, "call", lost)
    monkeypatch.setattr(
        access, "PrivateHubLifecycle", lambda: pytest.fail("Unconfirmed admission dispatched")
    )
    with TestClient(app_module.app) as api:
        r = api.request(
            "DELETE",
            f"/v1/corpora/{corpus}",
            headers={"Authorization": "Bearer test"},
            json={"operation_id": str(operation), "expected_revision": 1},
        )
    assert r.status_code == 503


def test_mutation_and_deletion_status_routes_never_admit_anonymous(configured, monkeypatch):
    corpus, operation = configured
    monkeypatch.setattr(access.CreationRPC, "call", lambda *_: pytest.fail("Anonymous RPC"))
    with TestClient(app_module.app) as api:
        path = f"/v1/corpora/{corpus}"
        assert (
            api.patch(
                path + "/visibility", json={"visibility": "public", "expected_revision": 1}
            ).status_code
            == 401
        )
        assert (
            api.request(
                "DELETE", path, json={"operation_id": str(operation), "expected_revision": 1}
            ).status_code
            == 401
        )
        assert api.get(path + "/deletions/" + str(operation)).status_code == 401


@pytest.mark.parametrize("failure", [None, "extra", "pagination", "changed", "lost_ack"])
def test_uncertain_discard_only_deletes_an_unchanged_empty_private_repository(configured, failure):
    corpus, _ = configured
    sha = "a" * 40
    calls = []

    def handler(request):
        calls.append(request.method)
        if request.method == "DELETE":
            if failure == "lost_ack":
                raise httpx.ReadTimeout("lost acknowledgement")
            return httpx.Response(200, stream=httpx.ByteStream(b"{}"))
        if request.url.path.endswith("/tree/" + sha):
            tree = [{"path": ".gitattributes", "type": "file"}]
            if failure == "extra":
                tree.append({"path": "data.tf", "type": "file"})
            headers = (
                {"Link": '<https://example.test/next>; rel="next"'}
                if failure == "pagination"
                else {}
            )
            return httpx.Response(
                200, headers=headers, stream=httpx.ByteStream(json.dumps(tree).encode())
            )
        if "DELETE" in calls:
            return httpx.Response(404, stream=httpx.ByteStream(b"{}"))
        value = {
            "id": f"fixture/corpus-{corpus}",
            "private": True,
            "sha": "b" * 40 if failure == "changed" else sha,
        }
        return httpx.Response(200, stream=httpx.ByteStream(json.dumps(value).encode()))

    async def invoke():
        return await access.PrivateHubLifecycle(lambda: httpx.MockTransport(handler)).operate(
            corpus, f"fixture/corpus-{corpus}", "dataset", sha=sha, delete=True, empty_only=True
        )

    if failure:
        with pytest.raises(CorpusMetadataError):
            asyncio.run(invoke())
    else:
        assert asyncio.run(invoke()) == sha
        assert calls == ["GET", "GET", "DELETE", "GET"]
    assert calls.count("DELETE") == (1 if failure in {None, "lost_ack"} else 0)


@pytest.mark.parametrize("state", ["executing", "uncertain", "completed"])
def test_uncertain_discard_retry_never_contacts_hub(configured, monkeypatch, state):
    corpus, operation = configured
    monkeypatch.setattr(
        access.CreationRPC,
        "call",
        lambda *_: {
            "status": "existing",
            "corpus_id": str(corpus),
            "operation_id": str(operation),
            "state": state,
        },
    )

    async def unexpected(*args, **kwargs):
        raise AssertionError("A retry must never contact the Hub")

    monkeypatch.setattr(access.PrivateHubLifecycle, "operate", unexpected)
    result = asyncio.run(
        access.discard_uncertain_creation(
            {"sub": str(uuid4()), "session_id": str(uuid4()), "exp": int(time.time()) + 3600},
            corpus,
            access.DeletionRequest(operation_id=operation, expected_revision=1),
        )
    )
    assert result.state == state


def test_uncertain_discard_admits_superadmin_before_hub_and_completes_once(configured, monkeypatch):
    corpus, operation = configured
    claim_id = uuid4()
    sha = "a" * 40
    repo = f"fixture/corpus-{corpus}"
    calls = []

    def rpc(_, name, params):
        calls.append(name)
        if name == "corpora_uncertain_discard_status":
            return {"status": "absent"}
        if name == "corpora_uncertain_discard_target":
            return {
                "status": "ok",
                "corpus_id": str(corpus),
                "repo_id": repo,
                "repo_type": "dataset",
            }
        if name == "corpora_begin_uncertain_discard":
            assert params["p_observed_sha"] == sha
            return {
                "status": "claimed",
                "corpus_id": str(corpus),
                "operation_id": str(operation),
                "claim_id": str(claim_id),
                "repo_id": repo,
                "repo_type": "dataset",
                "observed_sha": sha,
            }
        if name == "corpora_finish_corpus_deletion":
            assert params["p_claim"] == str(claim_id) and params["p_completed"] is True
            return True
        raise AssertionError(name)

    async def hub(_, _corpus, _repo, _kind, **kwargs):
        calls.append("hub_delete" if kwargs.get("delete") else "hub_inspect")
        assert kwargs["empty_only"] is True
        if kwargs.get("delete"):
            assert kwargs["sha"] == sha
        return sha

    monkeypatch.setattr(access.CreationRPC, "call", rpc)
    monkeypatch.setattr(access.PrivateHubLifecycle, "operate", hub)
    claims = {"sub": str(uuid4()), "session_id": str(uuid4()), "exp": int(time.time()) + 3600}
    result = asyncio.run(
        access.discard_uncertain_creation(
            claims, corpus, access.DeletionRequest(operation_id=operation, expected_revision=1)
        )
    )
    assert result.state == "completed"
    assert calls == [
        "corpora_uncertain_discard_status",
        "corpora_uncertain_discard_target",
        "hub_inspect",
        "corpora_begin_uncertain_discard",
        "hub_delete",
        "corpora_finish_corpus_deletion",
    ]
