from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from corpora_api.corpus_metadata import CorpusMetadataConflict
from corpora_api.corpus_provision_observation import (
    CorpusProvisionHubObserver,
    CorpusProvisionReconciler,
    ProvisionObservationTarget,
)


@pytest.fixture
def target():
    corpus = uuid4()
    return ProvisionObservationTarget(
        uuid4(), uuid4(), corpus, f"fixture-org/corpus-{corpus}", "dataset", "uncertain"
    )


def test_observes_only_exact_private_repository_without_mutation(target):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"id": target.repo_id, "private": True, "sha": "a" * 40})

    client = httpx.Client(transport=httpx.MockTransport(respond), follow_redirects=True)
    result = CorpusProvisionHubObserver(client, "fixture-token").observe_trusted(target)
    assert result.status == "private_repository_observed"
    assert result.observed_head_sha == "a" * 40
    assert len(requests) == 1 and requests[0].method == "GET"
    assert str(requests[0].url) == f"https://huggingface.co/api/datasets/{target.repo_id}"


@pytest.mark.parametrize("status", [301, 401, 403, 404, 429, 500])
def test_http_failures_are_inconclusive_and_never_follow_redirects(target, status):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(status, headers={"location": "https://elsewhere.example/"})

    client = httpx.Client(transport=httpx.MockTransport(respond), follow_redirects=True)
    result = CorpusProvisionHubObserver(client, "fixture-token").observe_trusted(target)
    assert result.status == "inconclusive"
    assert result.observed_head_sha is None
    assert len(requests) == 1


@pytest.mark.parametrize(
    "info",
    [
        {"id": "wrong/name", "private": True, "sha": "a" * 40},
        {"id": "correct", "private": False, "sha": "a" * 40},
        {"id": "correct", "private": "true", "sha": "a" * 40},
    ],
)
def test_wrong_identity_or_privacy_is_a_mismatch(target, info):
    if info["id"] == "correct":
        info["id"] = target.repo_id
    client = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=info)))
    result = CorpusProvisionHubObserver(client, "fixture-token").observe_trusted(target)
    assert result.status == "identity_or_privacy_mismatch"
    assert result.observed_head_sha is None


def test_missing_head_and_network_error_are_inconclusive(target):
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={"id": target.repo_id, "private": True})
        )
    )
    assert (
        CorpusProvisionHubObserver(client, "fixture-token").observe_trusted(target).status
        == "inconclusive"
    )

    def fail(request):
        raise httpx.ReadTimeout("failed", request=request)

    client = httpx.Client(transport=httpx.MockTransport(fail))
    assert (
        CorpusProvisionHubObserver(client, "fixture-token").observe_trusted(target).status
        == "inconclusive"
    )


def test_reconciler_rejects_snapshot_changed_during_hub_read(target):
    snapshots = iter([target, replace(target, state="executing")])
    lookup = SimpleNamespace(load_trusted=lambda _: next(snapshots))
    client = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(404)))
    reconciler = CorpusProvisionReconciler(lookup, CorpusProvisionHubObserver(client, "token"))
    with pytest.raises(CorpusMetadataConflict):
        reconciler.inspect_trusted(target.operation_id)


def test_oversized_response_is_inconclusive(target):
    client = httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=b"x" * 65537))
    )
    assert (
        CorpusProvisionHubObserver(client, "fixture-token").observe_trusted(target).status
        == "inconclusive"
    )
