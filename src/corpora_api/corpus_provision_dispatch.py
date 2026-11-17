"""One-shot internal dispatch after session-bound provisioning admission.

An accepted request may continue after sign-out. The dispatcher never retries a
claim. It records a pinned baseline only after confirmed creation and a second
private-repository observation. It needs a reviewed production privilege boundary.
"""

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from corpora_api.corpus_hub_provision import (
    CorpusHubProvisionError,
    CorpusHubProvisionUncertain,
    VerifiedPrivateRepository,
)
from corpora_api.corpus_provision_finalize import (
    CorpusProvisionFinalizer,
    TrustedBootstrapAttestation,
)
from corpora_api.corpus_provision_observation import CorpusProvisionReconciler
from corpora_api.corpus_provision_uncertainty import CorpusProvisionUncertaintyRecorder
from corpora_api.session_corpus_provision import (
    ClaimedCorpusProvision,
    SessionBoundCorpusProvisionAdmission,
)
from corpora_api.timed_corpus_hub_provision import (
    _PROVISION_SLOTS,
    MAX_PROVISION_SECONDS,
    _provision_with_deadline,
    _timeout,
)


@dataclass(frozen=True)
class DispatchedProvision:
    claim: ClaimedCorpusProvision
    repository: VerifiedPrivateRepository
    bootstrap_sha: str


class CorpusProvisionDispatcher:
    """Admit a single attempt and dispatch it once in the same request."""

    def __init__(
        self,
        admission: SessionBoundCorpusProvisionAdmission,
        uncertainty: CorpusProvisionUncertaintyRecorder,
        reconciler: CorpusProvisionReconciler,
        finalizer: CorpusProvisionFinalizer,
        *,
        worker: Callable[..., VerifiedPrivateRepository] = _provision_with_deadline,
    ) -> None:
        self.admission = admission
        self.uncertainty = uncertainty
        self.reconciler = reconciler
        self.finalizer = finalizer
        self.worker = worker

    def dispatch_trusted(
        self,
        verified_claims: Mapping[str, Any],
        corpus_id: str,
        *,
        timeout: float = MAX_PROVISION_SECONDS,
    ) -> DispatchedProvision:
        duration = _timeout(timeout)
        claims = dict(verified_claims)
        if not _PROVISION_SLOTS.acquire(blocking=False):
            raise CorpusHubProvisionError("Corpus repository provisioning is at capacity")
        try:
            # A failed or ambiguous admission never starts the worker.
            claim = self.admission.claim(claims, corpus_id)
            try:
                if (
                    not isinstance(claim, ClaimedCorpusProvision)
                    or claim.corpus_id != UUID(corpus_id)
                    or claim.actor_id != UUID(claims["sub"])
                    or claim.session_id != UUID(claims["session_id"])
                    or claim.repo_type not in ("dataset", "model")
                ):
                    raise ValueError("Invalid claim")
                result = self.worker(str(claim.actor_id), str(claim.corpus_id), timeout=duration)
                if (
                    not isinstance(result, VerifiedPrivateRepository)
                    or result.corpus_id != claim.corpus_id
                    or result.repo_id != claim.repo_id
                    or result.repo_type != claim.repo_type
                    or type(result.metadata_revision) is not int
                    or result.metadata_revision < claim.metadata_revision
                    or not isinstance(result.observed_head_sha, str)
                    or not re.fullmatch(r"[0-9a-f]{40}", result.observed_head_sha)
                ):
                    raise ValueError("Invalid worker result")
                observed = self.reconciler.inspect_trusted(claim.operation_id)
                if (
                    observed.target.operation_id != claim.operation_id
                    or observed.target.claim_id != claim.claim_id
                    or observed.target.corpus_id != claim.corpus_id
                    or observed.target.repo_id != claim.repo_id
                    or observed.target.repo_type != claim.repo_type
                    or observed.target.state != "executing"
                    or observed.status != "private_repository_observed"
                    or observed.observed_head_sha != result.observed_head_sha
                ):
                    raise ValueError("Bootstrap observation did not match")
                candidate = TrustedBootstrapAttestation(
                    claim.corpus_id,
                    claim.operation_id,
                    claim.claim_id,
                    claim.repo_id,
                    claim.repo_type,
                    result.observed_head_sha,
                )
                try:
                    baseline = self.finalizer.finalize_trusted(candidate)
                except Exception:
                    # A commit acknowledgement may be lost. Check the exact
                    # immutable result; never invoke the Hub worker again.
                    baseline = self.finalizer.find_finalized_trusted(candidate)
                if baseline != result.observed_head_sha:
                    raise ValueError("Bootstrap finalization did not match")
                return DispatchedProvision(claim, result, baseline)
            except Exception:
                # A failed update can itself have committed; never redispatch.
                try:
                    self.uncertainty.record_trusted(claim.operation_id, claim.claim_id)
                except Exception:
                    pass
                raise CorpusHubProvisionUncertain(
                    "Repository creation needs reconciliation"
                ) from None
        finally:
            _PROVISION_SLOTS.release()
