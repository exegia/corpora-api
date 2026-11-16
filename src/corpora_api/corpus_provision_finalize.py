"""Unwired database finalization of independently verified repository creation.

The attestation is an internal assertion, not a verifier. Never accept it from
clients or treat an observed repository head as proof of the creation commit.
The caller must verify the confirmed creation, repository identity, privacy,
observed baseline and worker liveness before invoking this database adapter.
Publishing a corpus version also requires complete tree and content validation.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from corpora_api.corpus_metadata import (
    CorpusMetadata,
    CorpusMetadataConflict,
    CorpusMetadataError,
    CorpusMetadataStore,
)


@dataclass(frozen=True)
class TrustedBootstrapAttestation:
    """Internal assertion only; constructing it does not verify the Hub."""

    corpus_id: UUID
    operation_id: UUID
    claim_id: UUID
    repo_id: str
    repo_type: str
    commit_sha: str


class CorpusProvisionFinalizer:
    """Record a trusted bootstrap and complete its claim in one transaction."""

    def __init__(self, connect: Callable[[], Any], *, assume_service_role: bool = False) -> None:
        self.connect = connect
        self.assume_service_role = assume_service_role

    def find_finalized_trusted(self, candidate: TrustedBootstrapAttestation) -> str | None:
        """Read the exact immutable result after an ambiguous commit acknowledgement."""
        if (
            not isinstance(candidate, TrustedBootstrapAttestation)
            or not all(
                isinstance(value, UUID)
                for value in (candidate.corpus_id, candidate.operation_id, candidate.claim_id)
            )
            or not isinstance(candidate.repo_id, str)
            or candidate.repo_type not in ("dataset", "model")
            or not isinstance(candidate.commit_sha, str)
            or not re.fullmatch(r"[0-9a-f]{40}", candidate.commit_sha)
        ):
            raise CorpusMetadataError("Invalid trusted bootstrap candidate")
        try:
            with self.connect() as db:
                with db.transaction():
                    if self.assume_service_role:
                        db.execute("SET LOCAL ROLE service_role")
                    db.execute("SELECT pg_catalog.set_config('statement_timeout','5000',true)")
                    row = db.execute(
                        "SELECT row_to_json(m), b.commit_sha "
                        "FROM public.corpora_corpus_bootstrap b "
                        "JOIN public.corpora_corpus_provision_ops o ON o.id=b.operation_id "
                        "JOIN public.corpora_corpus_metadata m ON m.id=b.corpus_id "
                        "WHERE b.corpus_id=%s::uuid AND b.operation_id=%s::uuid "
                        "AND b.worker_claim=%s::uuid AND b.commit_sha=%s "
                        "AND o.corpus_id=b.corpus_id AND o.worker_claim=b.worker_claim "
                        "AND o.repo_id=%s AND o.repo_type=%s AND o.state='completed'",
                        (
                            str(candidate.corpus_id),
                            str(candidate.operation_id),
                            str(candidate.claim_id),
                            candidate.commit_sha,
                            candidate.repo_id,
                            candidate.repo_type,
                        ),
                    ).fetchone()
                    if row is None:
                        return None
                    metadata = CorpusMetadata.model_validate(row[0])
                    CorpusMetadataStore._check_link(metadata)
                    if (
                        metadata.id != candidate.corpus_id
                        or metadata.hf_repo_id != candidate.repo_id
                        or metadata.hf_repo_type != candidate.repo_type
                        or row[1] != candidate.commit_sha
                    ):
                        raise ValueError("Mismatched finalized bootstrap")
                    return candidate.commit_sha
        except Exception:
            raise CorpusMetadataError("Finalized bootstrap lookup failed") from None

    def finalize_trusted(self, candidate: TrustedBootstrapAttestation) -> str:
        if (
            not isinstance(candidate, TrustedBootstrapAttestation)
            or not all(
                isinstance(v, UUID)
                for v in (candidate.corpus_id, candidate.operation_id, candidate.claim_id)
            )
            or not isinstance(candidate.commit_sha, str)
            or not re.fullmatch(r"[0-9a-f]{40}", candidate.commit_sha)
            or not isinstance(candidate.repo_id, str)
            or candidate.repo_type not in ("dataset", "model")
        ):
            raise CorpusMetadataError("Invalid trusted bootstrap candidate")
        try:
            with self.connect() as db:
                with db.transaction():
                    if self.assume_service_role:
                        db.execute("SET LOCAL ROLE service_role")
                    db.execute("SELECT pg_catalog.set_config('statement_timeout','5000',true)")
                    raw = db.execute(
                        "SELECT row_to_json(corpora_corpus_metadata) "
                        "FROM public.corpora_corpus_metadata WHERE id=%s::uuid FOR UPDATE",
                        (str(candidate.corpus_id),),
                    ).fetchone()
                    if raw is None:
                        raise CorpusMetadataConflict("Corpus bootstrap candidate conflicts")
                    metadata = CorpusMetadata.model_validate(raw[0])
                    CorpusMetadataStore._check_link(metadata)
                    if (
                        metadata.id != candidate.corpus_id
                        or metadata.hf_repo_id != candidate.repo_id
                        or metadata.hf_repo_type != candidate.repo_type
                    ):
                        raise CorpusMetadataConflict("Corpus bootstrap candidate conflicts")
                    raw_op = db.execute(
                        "SELECT row_to_json(corpora_corpus_provision_ops) "
                        "FROM public.corpora_corpus_provision_ops WHERE id=%s::uuid FOR UPDATE",
                        (str(candidate.operation_id),),
                    ).fetchone()
                    if raw_op is None or not isinstance(raw_op[0], dict):
                        raise CorpusMetadataConflict("Corpus bootstrap candidate conflicts")
                    op = raw_op[0]
                    try:
                        if (
                            UUID(op["id"]) != candidate.operation_id
                            or UUID(op["corpus_id"]) != candidate.corpus_id
                            or UUID(op["worker_claim"]) != candidate.claim_id
                            or op["repo_id"] != candidate.repo_id
                            or op["repo_type"] != candidate.repo_type
                        ):
                            raise ValueError("candidate mismatch")
                    except (ValueError, TypeError, KeyError, AttributeError) as error:
                        raise CorpusMetadataConflict(
                            "Corpus bootstrap candidate conflicts"
                        ) from error
                    if op["state"] == "completed":
                        row = db.execute(
                            "SELECT operation_id,worker_claim,commit_sha "
                            "FROM public.corpora_corpus_bootstrap WHERE corpus_id=%s::uuid",
                            (str(candidate.corpus_id),),
                        ).fetchone()
                        if row != (
                            candidate.operation_id,
                            candidate.claim_id,
                            candidate.commit_sha,
                        ):
                            raise CorpusMetadataConflict("Corpus bootstrap candidate conflicts")
                    else:
                        if op["state"] != "executing" or metadata.state != "provisioning":
                            raise CorpusMetadataConflict("Corpus bootstrap requires reconciliation")
                        if db.execute(
                            "SELECT 1 FROM public.corpora_corpus_heads WHERE corpus_id=%s::uuid",
                            (str(candidate.corpus_id),),
                        ).fetchone():
                            raise CorpusMetadataConflict("Corpus bootstrap candidate conflicts")
                        db.execute(
                            "INSERT INTO public.corpora_corpus_bootstrap "
                            "(corpus_id,operation_id,worker_claim,commit_sha) "
                            "VALUES (%s::uuid,%s::uuid,%s::uuid,%s)",
                            (
                                str(candidate.corpus_id),
                                str(candidate.operation_id),
                                str(candidate.claim_id),
                                candidate.commit_sha,
                            ),
                        )
                        completed = db.execute(
                            "UPDATE public.corpora_corpus_provision_ops SET state='completed' "
                            "WHERE id=%s::uuid AND worker_claim=%s::uuid AND state='executing' "
                            "RETURNING id",
                            (str(candidate.operation_id), str(candidate.claim_id)),
                        ).fetchone()
                        if completed is None:
                            raise CorpusMetadataConflict("Corpus bootstrap candidate conflicts")
            return candidate.commit_sha
        except CorpusMetadataConflict:
            raise
        except Exception:
            raise CorpusMetadataError(
                "Corpus bootstrap finalization failed; reconcile before retrying"
            ) from None
