"""Unwired staging must never silently retry remote writes."""

import hashlib
import io
import zipfile
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from corpora_api.corpus_hub_stage import CorpusHubStager, CorpusStageError, CorpusStageUncertain
from corpora_api.corpus_save_claim import ClaimedCorpusSave

CHECKS = {
    key: True
    for key in (
        "text_fabric_load",
        "context_fabric_load",
        "cfm_mmap_reload",
        "stats_match",
        "samples_match",
    )
}


def archive():
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("tf/otype.tf", b"fake for transport test")
    return b.getvalue()


class Validator:
    def validate(self, name, data):
        return {"path": name, "valid": True, "checks": CHECKS}


class FakeHub:
    endpoint = "https://huggingface.co"

    def __init__(self, corpus):
        self.repo_id = "fixture-org/corpus-" + str(corpus)
        self.calls = []
        self.parent = "1" * 40
        self.commit = "2" * 40
        self.private = True
        self.private_after = True
        self.fail = None
        self.ref_sha = None

    def repo_info(self, repo_id, **kwargs):
        self.calls.append(("info", kwargs["revision"]))
        if self.fail == "info":
            raise OSError("secret must not surface")
        return SimpleNamespace(
            id=self.repo_id,
            private=(self.private if kwargs["revision"] == self.parent else self.private_after),
            sha=(self.parent if kwargs["revision"] == self.parent else self.ref_sha or self.commit),
        )

    def create_branch(self, repo_id, **kwargs):
        self.calls.append(("branch", kwargs["branch"]))
        assert kwargs["revision"] == self.parent and kwargs["exist_ok"] is False
        if self.fail == "branch":
            raise OSError("secret must not surface")

    def create_commit(self, repo_id, operations, **kwargs):
        self.calls.append(("commit", kwargs["revision"]))
        assert kwargs["parent_commit"] == self.parent and len(operations) == 1
        if self.fail == "commit":
            raise OSError("secret must not surface")
        return SimpleNamespace(oid=self.commit)


def setup(monkeypatch, responder=None):
    monkeypatch.setenv("HF_CORPUS_ORGANIZATION", "fixture-org")
    data = archive()
    corpus, operation, worker = uuid4(), uuid4(), uuid4()
    claim = ClaimedCorpusSave(
        operation,
        worker,
        corpus,
        uuid4(),
        uuid4(),
        0,
        None,
        "1" * 40,
        hashlib.sha256(data).hexdigest(),
        "zip",
        f"fixture-org/corpus-{corpus}",
        "dataset",
        1,
        "corpora-op-" + str(operation),
    )
    hub = FakeHub(corpus)
    requests = []

    def respond(request):
        requests.append(request)
        return (
            responder(request, data)
            if responder
            else httpx.Response(200, stream=httpx.ByteStream(data))
        )

    client = httpx.Client(transport=httpx.MockTransport(respond))
    return claim, data, hub, client, requests


def test_stage_creates_unique_branch_and_verifies_pinned_bytes(monkeypatch):
    claim, data, hub, client, requests = setup(monkeypatch)
    result = CorpusHubStager(hub, client, "test-token").stage(claim, "mini.zip", data, Validator())
    assert result.stage_ref == "corpora-stage-" + str(claim.operation_id)
    assert result.stage_path == f".corpora-staging/{claim.operation_id}/{claim.claim_id}.zip"
    assert result.archive_sha256 == claim.input_sha256 and result.archive_size == len(data)
    assert result.stage_sha == "2" * 40 and result.requested_base_sha == "1" * 40
    assert not hasattr(result, "parent_sha")
    assert [v[0] for v in hub.calls] == ["info", "branch", "commit", "info"]
    assert result.stage_sha in str(requests[0].url)


@pytest.mark.parametrize(
    "failure", ["info", "branch", "commit", "private", "public-after", "no-op", "moved", "bad-sha"]
)
def test_failure_never_retries_or_returns_a_receipt(monkeypatch, failure):
    claim, data, hub, client, requests = setup(monkeypatch)
    if failure == "private":
        hub.private = False
    elif failure == "no-op":
        hub.commit = hub.parent
    elif failure == "moved":
        hub.ref_sha = "3" * 40
    elif failure == "bad-sha":
        hub.commit = "not-a-sha"
    elif failure == "public-after":
        hub.private_after = False
    else:
        hub.fail = failure
    with pytest.raises(CorpusStageUncertain) as error:
        CorpusHubStager(hub, client, "test-token").stage(claim, "mini.zip", data, Validator())
    assert "secret" not in str(error.value)
    assert sum(c[0] == "branch" for c in hub.calls) <= 1
    assert sum(c[0] == "commit" for c in hub.calls) <= 1
    if failure == "branch":
        assert not any(c[0] == "commit" for c in hub.calls)
    assert not requests


def test_redirect_to_cdn_strips_token_and_preserves_hash(monkeypatch):
    def respond(request, data):
        if request.url.host == "huggingface.co":
            assert request.headers["authorization"] == "Bearer test-token"
            return httpx.Response(302, headers={"location": "https://cdn.hf.co/payload"})
        assert request.url.host == "cdn.hf.co" and "authorization" not in request.headers
        return httpx.Response(200, stream=httpx.ByteStream(data))

    claim, data, hub, client, requests = setup(monkeypatch, respond)
    assert CorpusHubStager(hub, client, "test-token").stage(claim, "mini.zip", data, Validator())
    assert len(requests) == 2


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(302, headers={"location": "https://evil.example/steal"}),
        httpx.Response(200, content=b"wrong"),
        httpx.Response(200, content=b"x" * 1000),
    ],
)
def test_bad_redirect_or_readback_needs_reconciliation(monkeypatch, response):
    claim, data, hub, client, _ = setup(monkeypatch, lambda *_: response)
    with pytest.raises(CorpusStageUncertain):
        CorpusHubStager(hub, client, "test-token").stage(claim, "mini.zip", data, Validator())


def test_invalid_claim_or_digest_never_sends_remote_request(monkeypatch):
    claim, data, hub, client, requests = setup(monkeypatch)
    with pytest.raises(CorpusStageError):
        CorpusHubStager(hub, client, "test-token").stage(
            replace(claim, repo_id="other/bad"), "mini.zip", data, Validator()
        )
    with pytest.raises(ValueError):
        CorpusHubStager(hub, client, "test-token").stage(
            replace(claim, input_sha256="0" * 64), "mini.zip", data, Validator()
        )
    assert not hub.calls and not requests
