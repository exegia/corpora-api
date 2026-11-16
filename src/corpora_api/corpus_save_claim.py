"""Unwired, one-shot claims for already-admitted corpus save operations.

Only a trusted internal dispatcher may call this adapter. The claim is not a
lease and is never automatically retried or transferred. A lost commit response
must be reconciled without dispatching work from that failed response.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from corpora_api.corpus_metadata import CorpusMetadata, CorpusMetadataError, CorpusMetadataStore


@dataclass(frozen=True)
class ClaimedCorpusSave:
    operation_id: UUID
    claim_id: UUID
    corpus_id: UUID
    actor_id: UUID
    session_id: UUID
    expected_sequence: int
    expected_parent_sha: str | None
    hub_parent_sha: str
    input_sha256: str
    repo_id: str
    repo_type: str
    metadata_revision: int
    retained_ref: str


class CorpusSaveClaimer:
    """Claim an admitted operation exactly once before any remote work."""

    def __init__(self, connect: Callable[[], Any], *, assume_service_role: bool = False) -> None:
        self.connect = connect
        self.assume_service_role = assume_service_role

    def claim(self, operation_id: str) -> ClaimedCorpusSave | None:
        try:
            identifier = UUID(operation_id)
        except (TypeError, ValueError, AttributeError) as error:
            raise CorpusMetadataError("Invalid corpus save operation") from error
        claim_id = uuid4()
        result = None
        try:
            with self.connect() as db:
                with db.transaction():
                    if self.assume_service_role:
                        db.execute("SET LOCAL ROLE service_role")
                    db.execute("SELECT pg_catalog.set_config('statement_timeout','5000',true)")
                    saved = db.execute(
                        "UPDATE public.corpora_corpus_write_ops "
                        "SET state='executing',worker_claim=%s::uuid "
                        "WHERE id=%s::uuid AND state='admitted' "
                        "RETURNING row_to_json(corpora_corpus_write_ops)",
                        (str(claim_id), str(identifier)),
                    ).fetchone()
                    if saved is not None:
                        row = saved[0]
                        if not isinstance(row, dict):
                            raise ValueError("Invalid operation")
                        corpus = UUID(row["corpus_id"])
                        actor = UUID(row["actor_id"])
                        session = UUID(row["session_id"])
                        sequence, parent, digest = (
                            row["expected_sequence"],
                            row["expected_parent_sha"],
                            row["input_sha256"],
                        )
                        if (
                            UUID(row["id"]) != identifier
                            or UUID(row["worker_claim"]) != claim_id
                            or row["state"] != "executing"
                            or not row["claimed_at"]
                            or type(sequence) is not int
                            or not 0 <= sequence < 2**63 - 1
                            or (sequence == 0) != (parent is None)
                            or (parent is not None and not re.fullmatch(r"[0-9a-f]{40}", parent))
                            or not isinstance(digest, str)
                            or not re.fullmatch(r"[0-9a-f]{64}", digest)
                        ):
                            raise ValueError("Invalid operation")
                        raw_metadata = db.execute(
                            "SELECT row_to_json(corpora_corpus_metadata) "
                            "FROM public.corpora_corpus_metadata WHERE id=%s::uuid",
                            (str(corpus),),
                        ).fetchone()
                        if raw_metadata is None:
                            raise ValueError("Missing corpus")
                        metadata = CorpusMetadata.model_validate(raw_metadata[0])
                        if metadata.id != corpus:
                            raise ValueError("Mismatched corpus")
                        CorpusMetadataStore._check_link(metadata)
                        head = db.execute(
                            "SELECT sequence,commit_sha FROM public.corpora_corpus_heads "
                            "WHERE corpus_id=%s::uuid",
                            (str(corpus),),
                        ).fetchone()
                        actual = (head[0], head[1]) if head else (0, None)
                        if (
                            actual != (sequence, parent)
                            or (sequence == 0 and metadata.state != "provisioning")
                            or (sequence > 0 and metadata.state != "ready")
                        ):
                            raise ValueError("Corpus head is no longer claimable")
                        if sequence == 0:
                            bootstrap = db.execute(
                                "SELECT commit_sha FROM public.corpora_corpus_bootstrap "
                                "WHERE corpus_id=%s::uuid",
                                (str(corpus),),
                            ).fetchone()
                            if bootstrap is None:
                                raise ValueError("Corpus bootstrap is not recorded")
                            hub_parent = bootstrap[0]
                        else:
                            hub_parent = parent
                        if not isinstance(hub_parent, str) or not re.fullmatch(
                            r"[0-9a-f]{40}", hub_parent
                        ):
                            raise ValueError("Invalid Hub parent")
                        result = ClaimedCorpusSave(
                            operation_id=identifier,
                            claim_id=claim_id,
                            corpus_id=corpus,
                            actor_id=actor,
                            session_id=session,
                            expected_sequence=sequence,
                            expected_parent_sha=parent,
                            hub_parent_sha=hub_parent,
                            input_sha256=digest,
                            repo_id=metadata.hf_repo_id,
                            repo_type=metadata.hf_repo_type,
                            metadata_revision=metadata.revision,
                            retained_ref="corpora-op-" + str(identifier),
                        )
            return result
        except Exception:
            raise CorpusMetadataError(
                "Corpus save claim failed; reconcile before retrying"
            ) from None
