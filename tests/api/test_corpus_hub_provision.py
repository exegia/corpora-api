from __future__ import annotations

import json
import time
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from corpora_api.corpus_hub_provision import (
    CorpusHubProvisionConflict,
    CorpusHubProvisioner,
    CorpusHubProvisionError,
    CorpusHubProvisionUncertain,
)
from corpora_api.corpus_metadata import (
    CorpusMetadata,
    CorpusMetadataError,
    CorpusMetadataNotFound,
    CorpusMetadataStore,
)


@pytest.fixture
def corpus(monkeypatch):
    monkeypatch.setenv("HF_CORPUS_ORGANIZATION", "fixture-org")
    monkeypatch.setenv("HF_CORPUS_PROVISION_TOKEN", "test-hub-token-placeholder")
    owner, identifier = str(uuid4()), str(uuid4())
    record = CorpusMetadata.model_validate(
        {
            "id": identifier,
            "owner_id": owner,
            "title": "Mini",
            "description": None,
            "language": None,
            "properties": {},
            "hf_repo_id": f"fixture-org/corpus-{identifier}",
            "hf_repo_type": "dataset",
            "state": "provisioning",
            "revision": 1,
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
        }
    )

    def get(actor, requested):
        if actor != owner or requested != identifier:
            raise CorpusMetadataNotFound("Corpus not found")
        return record

    metadata = SimpleNamespace(get=get, _check_link=CorpusMetadataStore._check_link)
    return owner, identifier, record, metadata


@pytest.mark.parametrize("repo_type", ["dataset", "model"])
def test_creates_private_repository_and_verifies_identity(corpus, repo_type):
    owner, identifier, record, metadata = corpus
    record.hf_repo_type = repo_type
    requests = []

    def respond(request):
        requests.append(request)
        assert request.url.host == "huggingface.co"
        assert request.headers["authorization"] == "Bearer test-hub-token-placeholder"
        if request.url.path == "/api/organizations/fixture-org/overview":
            return httpx.Response(200, json={"name": "fixture-org"})
        if request.method == "POST":
            assert request.url.path == "/api/repos/create"
            assert json.loads(request.content) == {
                "name": f"corpus-{identifier}",
                "organization": "fixture-org",
                "type": repo_type,
                "visibility": "private",
            }
            prefix = "/datasets/" if repo_type == "dataset" else "/"
            return httpx.Response(
                200, json={"url": "https://huggingface.co" + prefix + record.hf_repo_id}
            )
        kind = "datasets" if repo_type == "dataset" else "models"
        assert request.url.path == f"/api/{kind}/{record.hf_repo_id}"
        return httpx.Response(200, json={"id": record.hf_repo_id, "private": True})

    client = httpx.Client(transport=httpx.MockTransport(respond), follow_redirects=True)
    receipt = CorpusHubProvisioner(metadata, client)._create_private(owner, identifier)
    assert (str(receipt.corpus_id), receipt.repo_id, receipt.repo_type) == (
        identifier,
        record.hf_repo_id,
        repo_type,
    )
    assert len(requests) == 3 and receipt.metadata_revision == 1


def test_existing_repository_is_not_adopted_or_modified(corpus):
    owner, identifier, _, metadata = corpus
    requests = []

    def respond(request):
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(200, json={"name": "fixture-org"})
        return httpx.Response(409, json={"error": "existing"})

    client = httpx.Client(transport=httpx.MockTransport(respond))
    with pytest.raises(CorpusHubProvisionConflict):
        CorpusHubProvisioner(metadata, client)._create_private(owner, identifier)
    assert [request.method for request in requests] == ["GET", "POST"]


def test_unexpected_creation_url_is_uncertain_and_not_followed(corpus):
    owner, identifier, _, metadata = corpus
    requests = []

    def respond(request):
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(200, json={"name": "fixture-org"})
        return httpx.Response(200, json={"url": "https://other.example/wrong"})

    client = httpx.Client(transport=httpx.MockTransport(respond), follow_redirects=True)
    with pytest.raises(CorpusHubProvisionUncertain):
        CorpusHubProvisioner(metadata, client)._create_private(owner, identifier)
    assert [request.method for request in requests] == ["GET", "POST"]


def test_non_provisioning_record_does_not_call_hub(corpus):
    owner, identifier, record, metadata = corpus
    record.state = "ready"
    requests = []
    client = httpx.Client(transport=httpx.MockTransport(lambda request: requests.append(request)))
    with pytest.raises(CorpusHubProvisionError, match="not awaiting"):
        CorpusHubProvisioner(metadata, client)._create_private(owner, identifier)
    assert not requests


def test_deadline_expired_after_organization_lookup_never_sends_create(corpus):
    owner, identifier, _, metadata = corpus
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"name": "fixture-org"})

    client = httpx.Client(transport=httpx.MockTransport(respond))
    with pytest.raises(CorpusHubProvisionUncertain):
        CorpusHubProvisioner(metadata, client)._create_private(
            owner, identifier, deadline=time.monotonic() - 1
        )
    assert [request.method for request in requests] == ["GET"]


@pytest.mark.parametrize(
    "bad_info",
    [
        {"id": "fixture-org/wrong", "private": True},
        {"id": "fixture-org/placeholder", "private": False},
        {"id": "fixture-org/placeholder", "private": "true"},
        {"id": "fixture-org/placeholder"},
    ],
)
def test_privacy_and_identity_must_be_verified(corpus, bad_info):
    owner, identifier, record, metadata = corpus
    if bad_info.get("id") == "fixture-org/placeholder":
        bad_info["id"] = record.hf_repo_id

    def respond(request):
        if request.url.path == "/api/organizations/fixture-org/overview":
            return httpx.Response(200, json={"name": "fixture-org"})
        if request.method == "POST":
            return httpx.Response(
                200, json={"url": "https://huggingface.co/datasets/" + record.hf_repo_id}
            )
        return httpx.Response(200, json=bad_info)

    client = httpx.Client(transport=httpx.MockTransport(respond))
    with pytest.raises(CorpusHubProvisionUncertain):
        CorpusHubProvisioner(metadata, client)._create_private(owner, identifier)


def test_lost_create_response_is_uncertain_and_not_retried(corpus):
    owner, identifier, _, metadata = corpus
    requests = []

    def respond(request):
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(200, json={"name": "fixture-org"})
        raise httpx.ReadTimeout("simulated", request=request)

    client = httpx.Client(transport=httpx.MockTransport(respond))
    with pytest.raises(CorpusHubProvisionUncertain):
        CorpusHubProvisioner(metadata, client)._create_private(owner, identifier)
    assert [request.method for request in requests] == ["GET", "POST"]


@pytest.mark.parametrize("status", [301, 307, 400, 408, 429, 500])
def test_non_success_may_have_committed_and_is_not_retried(corpus, status):
    owner, identifier, _, metadata = corpus
    requests = []

    def respond(request):
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(200, json={"name": "fixture-org"})
        return httpx.Response(status, headers={"location": "https://other.example/path"})

    client = httpx.Client(transport=httpx.MockTransport(respond), follow_redirects=True)
    with pytest.raises(CorpusHubProvisionUncertain):
        CorpusHubProvisioner(metadata, client)._create_private(owner, identifier)
    assert len(requests) == 2 and all(r.url.host == "huggingface.co" for r in requests)


@pytest.mark.parametrize("status,name", [(404, None), (200, "someone-else"), (200, None)])
def test_unverified_organization_prevents_creation(corpus, status, name):
    owner, identifier, _, metadata = corpus
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(status, json={"name": name})

    client = httpx.Client(transport=httpx.MockTransport(respond))
    with pytest.raises(CorpusHubProvisionError, match="organization"):
        CorpusHubProvisioner(metadata, client)._create_private(owner, identifier)
    assert [request.method for request in requests] == ["GET"]


def test_missing_credential_wrong_owner_or_bad_link_fail_before_hub_call(corpus, monkeypatch):
    owner, identifier, record, metadata = corpus
    requests = []
    client = httpx.Client(transport=httpx.MockTransport(lambda request: requests.append(request)))
    provisioner = CorpusHubProvisioner(metadata, client)
    with pytest.raises(CorpusMetadataNotFound):
        provisioner._create_private(str(uuid4()), identifier)
    monkeypatch.delenv("HF_CORPUS_PROVISION_TOKEN")
    with pytest.raises(CorpusHubProvisionError):
        provisioner._create_private(owner, identifier)
    monkeypatch.setenv("HF_CORPUS_PROVISION_TOKEN", "placeholder")
    record.hf_repo_id = "other-org/corpus-" + identifier
    with pytest.raises(CorpusMetadataError, match="invalid repository link"):
        provisioner._create_private(owner, identifier)
    assert not requests
