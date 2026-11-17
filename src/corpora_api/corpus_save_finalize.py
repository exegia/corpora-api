"""Unwired database finalization for a trusted, independently verified Hub result.

This module does not verify Hub commits, refs, input bytes, or worker liveness.
It must not be called with client-supplied attestations or before a trusted
verifier has established those facts and ruled out conflicting in-flight work.
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
class TrustedCandidateAttestation:
    """Internal assertion only; constructing this object is not verification."""

    operation_id: UUID
    claim_id: UUID
    publication_claim: UUID
    corpus_id: UUID
    repo_id: str
    repo_type: str
    parent_sha: str | None
    hub_parent_sha: str
    commit_sha: str
    input_sha256: str
    input_format: str
    retained_ref: str


@dataclass(frozen=True)
class FinalizedCorpusVersion:
    corpus_id: UUID
    operation_id: UUID
    sequence: int
    commit_sha: str
    parent_sha: str | None
    retained_ref: str


class CorpusSaveFinalizer:
    """Commit a trusted candidate to the version registry, without Hub I/O."""

    def __init__(self, connect: Callable[[], Any], *, assume_service_role: bool = False) -> None:
        self.connect = connect
        self.assume_service_role = assume_service_role

    def finalize_trusted(self, candidate: TrustedCandidateAttestation) -> FinalizedCorpusVersion:
        if (
            not isinstance(candidate, TrustedCandidateAttestation)
            or not all(
                isinstance(value, UUID)
                for value in (
                    candidate.operation_id,
                    candidate.claim_id,
                    candidate.publication_claim,
                    candidate.corpus_id,
                )
            )
            or not isinstance(candidate.commit_sha, str)
            or not re.fullmatch(r"[0-9a-f]{40}", candidate.commit_sha)
            or not isinstance(candidate.hub_parent_sha, str)
            or not re.fullmatch(r"[0-9a-f]{40}", candidate.hub_parent_sha)
            or candidate.hub_parent_sha == candidate.commit_sha
            or not isinstance(candidate.input_sha256, str)
            or not re.fullmatch(r"[0-9a-f]{64}", candidate.input_sha256)
            or candidate.input_format not in ("zip", "corpus")
            or candidate.retained_ref != "corpora-op-" + str(candidate.operation_id)
            or (
                candidate.parent_sha is not None
                and (
                    not isinstance(candidate.parent_sha, str)
                    or not re.fullmatch(r"[0-9a-f]{40}", candidate.parent_sha)
                    or candidate.parent_sha == candidate.commit_sha
                )
            )
        ):
            raise CorpusMetadataError("Invalid trusted corpus candidate")
        result = None
        try:
            with self.connect() as db:
                with db.transaction():
                    if self.assume_service_role:
                        db.execute("SET LOCAL ROLE service_role")
                    db.execute("SELECT pg_catalog.set_config('statement_timeout','5000',true)")
                    # Match admission's metadata-before-operation lock order.
                    raw_metadata = db.execute(
                        "SELECT row_to_json(corpora_corpus_metadata) "
                        "FROM public.corpora_corpus_metadata WHERE id=%s::uuid FOR UPDATE",
                        (str(candidate.corpus_id),),
                    ).fetchone()
                    if raw_metadata is None:
                        raise CorpusMetadataConflict("Corpus save candidate conflicts")
                    metadata = CorpusMetadata.model_validate(raw_metadata[0])
                    CorpusMetadataStore._check_link(metadata)
                    if (
                        metadata.id != candidate.corpus_id
                        or metadata.hf_repo_id != candidate.repo_id
                        or metadata.hf_repo_type != candidate.repo_type
                    ):
                        raise CorpusMetadataConflict("Corpus save candidate conflicts")
                    raw_op = db.execute(
                        "SELECT row_to_json(corpora_corpus_write_ops) "
                        "FROM public.corpora_corpus_write_ops WHERE id=%s::uuid FOR UPDATE",
                        (str(candidate.operation_id),),
                    ).fetchone()
                    if raw_op is None or not isinstance(raw_op[0], dict):
                        raise CorpusMetadataConflict("Corpus save candidate conflicts")
                    op = raw_op[0]
                    try:
                        sequence = op["expected_sequence"]
                        if (
                            UUID(op["id"]) != candidate.operation_id
                            or UUID(op["corpus_id"]) != candidate.corpus_id
                            or UUID(op["worker_claim"]) != candidate.claim_id
                            or type(sequence) is not int
                            or not 0 <= sequence < 2**63 - 1
                            or op["expected_parent_sha"] != candidate.parent_sha
                            or op["input_sha256"] != candidate.input_sha256
                            or op["input_format"] != candidate.input_format
                            or (sequence == 0) != (candidate.parent_sha is None)
                        ):
                            raise ValueError("Candidate mismatch")
                    except (TypeError, ValueError, KeyError, AttributeError) as error:
                        raise CorpusMetadataConflict("Corpus save candidate conflicts") from error
                    next_sequence = sequence + 1
                    if sequence == 0:
                        bootstrap = db.execute(
                            "SELECT commit_sha FROM public.corpora_corpus_bootstrap "
                            "WHERE corpus_id=%s::uuid",
                            (str(candidate.corpus_id),),
                        ).fetchone()
                        if bootstrap is None or bootstrap[0] != candidate.hub_parent_sha:
                            raise CorpusMetadataConflict("Corpus save candidate conflicts")
                    elif candidate.parent_sha != candidate.hub_parent_sha:
                        raise CorpusMetadataConflict("Corpus save candidate conflicts")
                    if op["state"] not in ("executing", "completed"):
                        raise CorpusMetadataConflict("Corpus save requires reconciliation")
                    if not db.execute(
                        "SELECT 1 FROM public.corpora_corpus_publication_claims "
                        "WHERE operation_id=%s::uuid AND corpus_id=%s::uuid "
                        "AND stage_claim=%s::uuid AND publication_claim=%s::uuid",
                        (
                            str(candidate.operation_id),
                            str(candidate.corpus_id),
                            str(candidate.claim_id),
                            str(candidate.publication_claim),
                        ),
                    ).fetchone():
                        raise CorpusMetadataConflict("Corpus save publication claim conflicts")
                    if op["state"] == "completed":
                        version = db.execute(
                            "SELECT sequence,commit_sha,parent_sha,retained_ref "
                            "FROM public.corpora_corpus_versions WHERE operation_id=%s::uuid "
                            "AND corpus_id=%s::uuid",
                            (str(candidate.operation_id), str(candidate.corpus_id)),
                        ).fetchone()
                        if version != (
                            next_sequence,
                            candidate.commit_sha,
                            candidate.parent_sha,
                            candidate.retained_ref,
                        ):
                            raise CorpusMetadataConflict("Corpus save candidate conflicts")
                    else:
                        # An uncertain worker requires a separate reconciliation
                        # path; a database claim cannot stop a remote request.
                        if op["state"] != "executing":
                            raise CorpusMetadataConflict("Corpus save requires reconciliation")
                        staged = db.execute(
                            "SELECT stage_sha FROM public.corpora_corpus_staged_inputs "
                            "WHERE operation_id=%s::uuid AND corpus_id=%s::uuid "
                            "AND worker_claim=%s::uuid AND input_sha256=%s AND input_format=%s",
                            (
                                str(candidate.operation_id),
                                str(candidate.corpus_id),
                                str(candidate.claim_id),
                                candidate.input_sha256,
                                candidate.input_format,
                            ),
                        ).fetchone()
                        if not staged:
                            raise CorpusMetadataConflict("Corpus save has no staged input receipt")
                        if staged[0] == candidate.commit_sha:
                            raise CorpusMetadataConflict("Staging commit cannot be published")
                        raw_head = db.execute(
                            "SELECT sequence,commit_sha FROM public.corpora_corpus_heads "
                            "WHERE corpus_id=%s::uuid FOR UPDATE",
                            (str(candidate.corpus_id),),
                        ).fetchone()
                        head = (raw_head[0], raw_head[1]) if raw_head else (0, None)
                        if (
                            head != (sequence, candidate.parent_sha)
                            or (sequence == 0 and metadata.state != "provisioning")
                            or (sequence > 0 and metadata.state != "ready")
                        ):
                            raise CorpusMetadataConflict("Corpus save candidate conflicts")
                        db.execute(
                            "INSERT INTO public.corpora_corpus_versions "
                            "(corpus_id,sequence,commit_sha,parent_sha,operation_id,"
                            "publication_claim,retained_ref) "
                            "VALUES (%s::uuid,%s,%s,%s,%s::uuid,%s::uuid,%s)",
                            (
                                str(candidate.corpus_id),
                                next_sequence,
                                candidate.commit_sha,
                                candidate.parent_sha,
                                str(candidate.operation_id),
                                str(candidate.publication_claim),
                                candidate.retained_ref,
                            ),
                        )
                        if sequence == 0:
                            db.execute(
                                "INSERT INTO public.corpora_corpus_heads "
                                "(corpus_id,sequence,commit_sha) "
                                "VALUES (%s::uuid,%s,%s)",
                                (str(candidate.corpus_id), next_sequence, candidate.commit_sha),
                            )
                            ready = db.execute(
                                "UPDATE public.corpora_corpus_metadata SET state='ready' "
                                "WHERE id=%s::uuid AND state='provisioning' RETURNING id",
                                (str(candidate.corpus_id),),
                            ).fetchone()
                            if ready is None:
                                raise CorpusMetadataConflict("Corpus save candidate conflicts")
                        else:
                            moved = db.execute(
                                "UPDATE public.corpora_corpus_heads SET sequence=%s,commit_sha=%s "
                                "WHERE corpus_id=%s::uuid AND sequence=%s AND commit_sha=%s "
                                "RETURNING corpus_id",
                                (
                                    next_sequence,
                                    candidate.commit_sha,
                                    str(candidate.corpus_id),
                                    sequence,
                                    candidate.parent_sha,
                                ),
                            ).fetchone()
                            if moved is None:
                                raise CorpusMetadataConflict("Corpus save candidate conflicts")
                        completed = db.execute(
                            "UPDATE public.corpora_corpus_write_ops SET state='completed' "
                            "WHERE id=%s::uuid AND worker_claim=%s::uuid AND state='executing' "
                            "RETURNING id",
                            (str(candidate.operation_id), str(candidate.claim_id)),
                        ).fetchone()
                        if completed is None:
                            raise CorpusMetadataConflict("Corpus save candidate conflicts")
                    result = FinalizedCorpusVersion(
                        corpus_id=candidate.corpus_id,
                        operation_id=candidate.operation_id,
                        sequence=next_sequence,
                        commit_sha=candidate.commit_sha,
                        parent_sha=candidate.parent_sha,
                        retained_ref=candidate.retained_ref,
                    )
            return result
        except CorpusMetadataConflict:
            raise
        except Exception:
            raise CorpusMetadataError(
                "Corpus save finalization failed; reconcile before retrying"
            ) from None
