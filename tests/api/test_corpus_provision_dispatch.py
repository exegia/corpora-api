from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException

from corpora_api.corpus_hub_provision import CorpusHubProvisionUncertain, VerifiedPrivateRepository
from corpora_api.corpus_provision_dispatch import CorpusProvisionDispatcher
from corpora_api.corpus_provision_observation import (
    ProvisionObservation,
    ProvisionObservationTarget,
)
from corpora_api.session_corpus_provision import ClaimedCorpusProvision


@pytest.fixture
def setup():
    operation, worker_claim, corpus, actor, session = (uuid4() for _ in range(5))
    claim = ClaimedCorpusProvision(
        operation, worker_claim, corpus, actor, session, f"fixture/corpus-{corpus}", "dataset", 1
    )
    result = VerifiedPrivateRepository(corpus, 1, claim.repo_id, "dataset", "a" * 40)
    return claim, result, {"sub": str(actor), "session_id": str(session)}


def dependencies(claim, result):
    target = ProvisionObservationTarget(
        claim.operation_id,
        claim.claim_id,
        claim.corpus_id,
        claim.repo_id,
        claim.repo_type,
        "executing",
    )
    observation = ProvisionObservation(
        target, "private_repository_observed", result.observed_head_sha
    )
    return (
        SimpleNamespace(inspect_trusted=lambda _: observation),
        SimpleNamespace(finalize_trusted=lambda candidate: candidate.commit_sha),
    )


def test_admits_once_then_dispatches_once_and_returns_claim(setup):
    claim, result, claims = setup
    calls = []
    admission = SimpleNamespace(claim=lambda c, i: calls.append(("claim", c, i)) or claim)
    recorder = SimpleNamespace(record_trusted=lambda *args: calls.append(("uncertain", args)))

    def worker(actor, corpus, *, timeout):
        calls.append(("worker", actor, corpus, timeout))
        return result

    reconciler, finalizer = dependencies(claim, result)
    dispatched = CorpusProvisionDispatcher(
        admission, recorder, reconciler, finalizer, worker=worker
    ).dispatch_trusted(claims, str(claim.corpus_id), timeout=5)
    assert dispatched.claim == claim and dispatched.repository == result
    assert dispatched.bootstrap_sha == result.observed_head_sha
    assert [call[0] for call in calls] == ["claim", "worker"]


def test_rejected_or_ambiguous_admission_never_dispatches(setup):
    claim, _, claims = setup
    calls = []

    def reject(*args):
        raise HTTPException(401, "session invalid")

    admission = SimpleNamespace(claim=reject)
    recorder = SimpleNamespace(record_trusted=lambda *args: calls.append("uncertain"))
    dispatcher = CorpusProvisionDispatcher(
        admission, recorder, None, None, worker=lambda *args, **kwargs: calls.append("worker")
    )
    with pytest.raises(HTTPException) as error:
        dispatcher.dispatch_trusted(claims, str(claim.corpus_id))
    assert error.value.status_code == 401
    assert calls == []


@pytest.mark.parametrize("failure", ["exception", "wrong_repo", "bad_head"])
def test_failed_or_mismatched_worker_result_records_uncertainty_without_retry(setup, failure):
    claim, result, claims = setup
    calls = []
    admission = SimpleNamespace(claim=lambda *_: claim)
    recorder = SimpleNamespace(record_trusted=lambda *args: calls.append(("uncertain", args)))

    def worker(*args, **kwargs):
        calls.append(("worker",))
        if failure == "exception":
            raise RuntimeError("sensitive upstream text")
        if failure == "wrong_repo":
            return replace(result, repo_id="fixture/wrong")
        return replace(result, observed_head_sha="bad")

    dispatcher = CorpusProvisionDispatcher(admission, recorder, None, None, worker=worker)
    with pytest.raises(CorpusHubProvisionUncertain, match="reconciliation") as error:
        dispatcher.dispatch_trusted(claims, str(claim.corpus_id))
    assert "sensitive" not in str(error.value)
    assert calls == [("worker",), ("uncertain", (claim.operation_id, claim.claim_id))]


def test_recorder_failure_cannot_cause_retry(setup):
    claim, _, claims = setup
    calls = []

    def fail(*args, **kwargs):
        calls.append("called")
        raise RuntimeError("unavailable")

    dispatcher = CorpusProvisionDispatcher(
        SimpleNamespace(claim=lambda *_: claim),
        SimpleNamespace(record_trusted=fail),
        None,
        None,
        worker=fail,
    )
    with pytest.raises(CorpusHubProvisionUncertain):
        dispatcher.dispatch_trusted(claims, str(claim.corpus_id))
    assert calls == ["called", "called"]


def test_mismatched_admission_never_invokes_worker(setup):
    claim, _, claims = setup
    calls = []
    admission = SimpleNamespace(claim=lambda *_: replace(claim, session_id=uuid4()))
    recorder = SimpleNamespace(record_trusted=lambda *args: calls.append(("uncertain", args)))
    dispatcher = CorpusProvisionDispatcher(
        admission, recorder, None, None, worker=lambda *a, **k: calls.append("worker")
    )
    with pytest.raises(CorpusHubProvisionUncertain):
        dispatcher.dispatch_trusted(claims, str(claim.corpus_id))
    assert calls == [("uncertain", (claim.operation_id, claim.claim_id))]


def test_lost_finalizer_ack_is_recovered_without_another_worker(setup):
    claim, result, claims = setup
    calls = []
    reconciler, _ = dependencies(claim, result)

    def lost_ack(candidate):
        calls.append("finalize")
        raise RuntimeError("ack lost")

    def find(candidate):
        calls.append("lookup")
        return candidate.commit_sha

    def worker(*args, **kwargs):
        calls.append("worker")
        return result

    finalizer = SimpleNamespace(finalize_trusted=lost_ack, find_finalized_trusted=find)
    dispatcher = CorpusProvisionDispatcher(
        SimpleNamespace(claim=lambda *_: claim), None, reconciler, finalizer, worker=worker
    )
    assert dispatcher.dispatch_trusted(claims, str(claim.corpus_id)).bootstrap_sha == "a" * 40
    assert calls == ["worker", "finalize", "lookup"]


def test_changed_head_does_not_finalize_or_retry(setup):
    claim, result, claims = setup
    calls = []
    reconciler, finalizer = dependencies(claim, replace(result, observed_head_sha="b" * 40))
    dispatcher = CorpusProvisionDispatcher(
        SimpleNamespace(claim=lambda *_: claim),
        SimpleNamespace(record_trusted=lambda *args: calls.append("uncertain")),
        reconciler,
        finalizer,
        worker=lambda *args, **kwargs: result,
    )
    with pytest.raises(CorpusHubProvisionUncertain):
        dispatcher.dispatch_trusted(claims, str(claim.corpus_id))
    assert calls == ["uncertain"]
