from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from corpora_api.corpus_hub_read import CorpusHubReader, CorpusHubReadError
from corpora_api.corpus_metadata import CorpusMetadataNotFound

REVISION = "a" * 40


class Metadata:
    def __init__(self, owner, corpus, *, state="ready", kind="dataset"):
        self.owner, self.corpus = owner, corpus
        self.row = SimpleNamespace(
            id=corpus,
            owner_id=owner,
            hf_repo_id=f"test-org/corpus-{corpus}",
            hf_repo_type=kind,
            state=state,
        )

    def get(self, actor, corpus):
        if actor != self.owner or corpus != self.corpus:
            raise CorpusMetadataNotFound("Corpus not found")
        return self.row


def stream_response(status=200, body=b"", headers=None):
    return httpx.Response(status, stream=httpx.ByteStream(body), headers=headers)


def reader_for(monkeypatch, handler, *, state="ready", kind="dataset"):
    owner, corpus = str(uuid4()), str(uuid4())
    metadata = Metadata(owner, corpus, state=state, kind=kind)
    monkeypatch.setenv("HF_CORPUS_READ_TOKEN", "test-read-token-placeholder")
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return CorpusHubReader(metadata, client), owner, corpus, metadata.row


def test_private_dataset_read_uses_owner_and_immutable_revision(monkeypatch):
    requests = []
    reader, owner, corpus, row = reader_for(monkeypatch, lambda request: respond(request))

    def respond(request):
        requests.append(request)
        assert request.extensions["timeout"]["read"] == 15.0
        assert request.headers["authorization"] == "Bearer test-read-token-placeholder"
        if request.url.path.startswith("/api/"):
            assert request.url.path == f"/api/datasets/{row.hf_repo_id}/revision/{REVISION}"
            return stream_response(
                body=json.dumps({"id": row.hf_repo_id, "sha": REVISION, "private": True}).encode()
            )
        assert request.url.path == f"/datasets/{row.hf_repo_id}/resolve/{REVISION}/tf/otype.tf"
        return stream_response(body=b"corpus data", headers={"content-length": "11"})

    with reader._open(owner, corpus, REVISION, "tf/otype.tf", max_bytes=20) as chunks:
        assert b"".join(chunks) == b"corpus data"
    assert len(requests) == 2
    with pytest.raises(CorpusMetadataNotFound):
        with reader._open(str(uuid4()), corpus, REVISION, "tf/otype.tf", max_bytes=20):
            pytest.fail("foreign user must not reach the Hub")
    assert len(requests) == 2


def test_redirect_does_not_forward_bearer_to_hf_cdn(monkeypatch):
    requests = []
    reader, owner, corpus, row = reader_for(monkeypatch, lambda request: respond(request))

    def respond(request):
        requests.append(request)
        if request.url.path.startswith("/api/"):
            return stream_response(
                body=json.dumps({"id": row.hf_repo_id, "sha": REVISION, "private": True}).encode()
            )
        if request.url.host == "huggingface.co":
            return stream_response(
                302, headers={"location": "https://cdn.hf.co/blob?signed=example"}
            )
        assert request.url.host == "cdn.hf.co"
        assert "authorization" not in request.headers
        return stream_response(body=b"cache")

    with reader._open(owner, corpus, REVISION, "book.cfr", max_bytes=10) as chunks:
        assert b"".join(chunks) == b"cache"
    assert len(requests) == 3


@pytest.mark.parametrize(
    "location",
    [
        "http://huggingface.co/file",
        "https://huggingface.co.attacker.example/file",
        "https://user@huggingface.co/file",
        "https://127.0.0.1/file",
        "https://huggingface.co:444/file",
    ],
)
def test_unsafe_redirect_never_receives_token(monkeypatch, location):
    seen = []
    reader, owner, corpus, row = reader_for(monkeypatch, lambda request: respond(request))

    def respond(request):
        seen.append(request.url.host)
        if request.url.path.startswith("/api/"):
            return stream_response(
                body=json.dumps({"id": row.hf_repo_id, "sha": REVISION, "private": True}).encode()
            )
        return stream_response(302, headers={"location": location})

    with pytest.raises(CorpusHubReadError):
        with reader._open(owner, corpus, REVISION, "book.cfr", max_bytes=10):
            pytest.fail("unsafe redirect must fail")
    assert seen == ["huggingface.co", "huggingface.co"]


@pytest.mark.parametrize(
    "path", ["", "/abs", "../private", "tf/../private", "tf//x", "x\\y", "x\n"]
)
def test_invalid_paths_are_rejected_before_network(monkeypatch, path):
    reader, owner, corpus, _ = reader_for(monkeypatch, lambda _: pytest.fail("network accessed"))
    with pytest.raises(CorpusHubReadError):
        with reader._open(owner, corpus, REVISION, path, max_bytes=10):
            pass


@pytest.mark.parametrize("bad", [False, None, "true"])
def test_repository_must_be_confirmed_private(monkeypatch, bad):
    calls = []
    reader, owner, corpus, row = reader_for(monkeypatch, lambda request: respond(request))

    def respond(request):
        calls.append(request)
        return stream_response(
            body=json.dumps({"id": row.hf_repo_id, "sha": REVISION, "private": bad}).encode()
        )

    with pytest.raises(CorpusHubReadError):
        with reader._open(owner, corpus, REVISION, "x.tf", max_bytes=10):
            pass
    assert len(calls) == 1


def test_stream_is_bounded_even_without_content_length(monkeypatch):
    reader, owner, corpus, row = reader_for(monkeypatch, lambda request: respond(request))

    def respond(request):
        if request.url.path.startswith("/api/"):
            return stream_response(
                body=json.dumps({"id": row.hf_repo_id, "sha": REVISION, "private": True}).encode()
            )
        return stream_response(body=b"too large")

    with pytest.raises(CorpusHubReadError, match="size limit"):
        with reader._open(owner, corpus, REVISION, "x.tf", max_bytes=3) as chunks:
            b"".join(chunks)


def test_unready_and_invalid_revision_fail_before_hub(monkeypatch):
    reader, owner, corpus, _ = reader_for(
        monkeypatch, lambda _: pytest.fail("network accessed"), state="provisioning"
    )
    for revision in (REVISION, "main", "a" * 39):
        with pytest.raises(CorpusHubReadError):
            with reader._open(owner, corpus, revision, "x.tf", max_bytes=10):
                pass


@pytest.mark.parametrize("field", ["id", "sha"])
def test_repository_identity_and_revision_must_match(monkeypatch, field):
    reader, owner, corpus, row = reader_for(monkeypatch, lambda request: respond(request))

    def respond(request):
        info = {"id": row.hf_repo_id, "sha": REVISION, "private": True}
        info[field] = "different"
        return stream_response(body=json.dumps(info).encode())

    with pytest.raises(CorpusHubReadError):
        with reader._open(owner, corpus, REVISION, "x.tf", max_bytes=10):
            pass


@pytest.mark.parametrize("headers", [{"content-length": "999"}, {"content-encoding": "gzip"}])
def test_oversize_or_encoded_response_fails_before_yield(monkeypatch, headers):
    reader, owner, corpus, row = reader_for(monkeypatch, lambda request: respond(request))

    def respond(request):
        if request.url.path.startswith("/api/"):
            return stream_response(
                body=json.dumps({"id": row.hf_repo_id, "sha": REVISION, "private": True}).encode()
            )
        return stream_response(body=b"file", headers=headers)

    with pytest.raises(CorpusHubReadError):
        with reader._open(owner, corpus, REVISION, "x.tf", max_bytes=3):
            pytest.fail("unsafe response must not be yielded")


def test_redirect_loop_has_a_bounded_number_of_requests(monkeypatch):
    requests = []
    reader, owner, corpus, row = reader_for(monkeypatch, lambda request: respond(request))

    def respond(request):
        requests.append(request)
        if request.url.path.startswith("/api/"):
            return stream_response(
                body=json.dumps({"id": row.hf_repo_id, "sha": REVISION, "private": True}).encode()
            )
        return stream_response(302, headers={"location": "/repeat"})

    with pytest.raises(CorpusHubReadError, match="redirect"):
        with reader._open(owner, corpus, REVISION, "x.tf", max_bytes=10):
            pass
    assert len(requests) == 6


def test_model_read_and_caller_exception_are_preserved(monkeypatch):
    reader, owner, corpus, row = reader_for(
        monkeypatch, lambda request: respond(request), kind="model"
    )

    def respond(request):
        if request.url.path.startswith("/api/"):
            assert request.url.path.startswith("/api/models/")
            return stream_response(
                body=json.dumps({"id": row.hf_repo_id, "sha": REVISION, "private": True}).encode()
            )
        assert request.url.path == f"/{row.hf_repo_id}/resolve/{REVISION}/book.cfr"
        return stream_response(body=b"cache")

    with pytest.raises(ValueError, match="caller"):
        with reader._open(owner, corpus, REVISION, "book.cfr", max_bytes=10):
            raise ValueError("caller")


def test_injected_client_credentials_are_not_forwarded_to_cdn(monkeypatch):
    requests = []
    reader, owner, corpus, row = reader_for(monkeypatch, lambda request: respond(request))
    reader.client.headers["authorization"] = "Bearer unexpected-client-token"
    reader.client.cookies.set("unexpected", "cookie", domain=".hf.co")
    reader.client.auth = ("unexpected", "password")

    def respond(request):
        requests.append(request)
        if request.url.path.startswith("/api/"):
            assert request.headers["authorization"] == "Bearer test-read-token-placeholder"
            return stream_response(
                body=json.dumps({"id": row.hf_repo_id, "sha": REVISION, "private": True}).encode()
            )
        if request.url.host == "huggingface.co":
            assert request.headers["authorization"] == "Bearer test-read-token-placeholder"
            return stream_response(302, headers={"location": "https://cdn.hf.co/file"})
        assert "authorization" not in request.headers
        assert "cookie" not in request.headers
        return stream_response(body=b"ok")

    with reader._open(owner, corpus, REVISION, "x.tf", max_bytes=10) as chunks:
        assert b"".join(chunks) == b"ok"
    assert len(requests) == 3


def test_signed_url_is_not_in_public_exception(monkeypatch):
    reader, owner, corpus, row = reader_for(monkeypatch, lambda request: respond(request))

    def respond(request):
        if request.url.path.startswith("/api/"):
            return stream_response(
                body=json.dumps({"id": row.hf_repo_id, "sha": REVISION, "private": True}).encode()
            )
        if request.url.host == "huggingface.co":
            return stream_response(
                302, headers={"location": "https://cdn.hf.co/file?token=example-only"}
            )
        raise httpx.ConnectError("https://cdn.hf.co/file?token=example-only", request=request)

    with pytest.raises(CorpusHubReadError) as error:
        with reader._open(owner, corpus, REVISION, "x.tf", max_bytes=10):
            pass
    assert "example-only" not in str(error.value)
    assert error.value.__suppress_context__
