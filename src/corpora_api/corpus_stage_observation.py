"""Read-only inspection of an unreceipted save attempt.

An observation target does not renew a worker claim, prove the prior worker has
stopped, or authorize a Hub write, receipt insertion or publication.
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
class StageObservationTarget:
    corpus_id: UUID
    operation_id: UUID
    claim_id: UUID
    repo_id: str
    repo_type: str
    input_format: str
    input_sha256: str
    requested_base_sha: str
    observed_state: str


class CorpusStageObservationLookup:
    """Snapshot an existing unreceipted operation for read-only inspection."""

    def __init__(self, connect: Callable[[], Any], *, assume_service_role: bool = False) -> None:
        self.connect = connect
        self.assume_service_role = assume_service_role

    def load_trusted(self, operation_id: UUID) -> StageObservationTarget:
        if not isinstance(operation_id, UUID):
            raise CorpusMetadataError("Invalid stage observation identity")
        try:
            with self.connect() as db:
                with db.transaction():
                    if self.assume_service_role:
                        db.execute("SET LOCAL ROLE service_role")
                    db.execute("SELECT pg_catalog.set_config('statement_timeout','5000',true)")
                    identity = db.execute(
                        "SELECT corpus_id FROM public.corpora_corpus_write_ops WHERE id=%s::uuid",
                        (str(operation_id),),
                    ).fetchone()
                    if identity is None:
                        raise CorpusMetadataConflict("Stage observation is unavailable")
                    # Match recorder/finalizer lock order; no network I/O in the transaction.
                    db.execute(
                        "SELECT 1 FROM public.corpora_corpus_metadata WHERE id=%s::uuid FOR UPDATE",
                        (str(identity[0]),),
                    )
                    db.execute(
                        "SELECT 1 FROM public.corpora_corpus_write_ops "
                        "WHERE id=%s::uuid FOR UPDATE",
                        (str(operation_id),),
                    )
                    result = db.execute(
                        "SELECT row_to_json(o),row_to_json(m),b.commit_sha "
                        "FROM public.corpora_corpus_write_ops o "
                        "JOIN public.corpora_corpus_metadata m ON m.id=o.corpus_id "
                        "LEFT JOIN public.corpora_corpus_bootstrap b ON b.corpus_id=o.corpus_id "
                        "LEFT JOIN public.corpora_corpus_heads h ON h.corpus_id=o.corpus_id "
                        "WHERE o.id=%s::uuid AND o.state IN ('executing','uncertain') "
                        "AND o.worker_claim IS NOT NULL "
                        "AND NOT EXISTS (SELECT 1 FROM public.corpora_corpus_staged_inputs s "
                        "WHERE s.operation_id=o.id) AND ("
                        "(o.expected_sequence=0 AND h.corpus_id IS NULL "
                        "AND m.state='provisioning' AND b.commit_sha IS NOT NULL) OR "
                        "(o.expected_sequence>0 AND m.state='ready' "
                        "AND h.sequence=o.expected_sequence "
                        "AND h.commit_sha=o.expected_parent_sha))",
                        (str(operation_id),),
                    ).fetchone()
                    if result is None:
                        raise CorpusMetadataConflict("Stage observation is unavailable")
                    row, metadata_raw, bootstrap = result
                    metadata = CorpusMetadata.model_validate(metadata_raw)
                    CorpusMetadataStore._check_link(metadata)
                    if not isinstance(row, dict):
                        raise ValueError("Invalid operation")
                    corpus = UUID(row["corpus_id"])
                    claim = UUID(row["worker_claim"])
                    digest, kind, state, sequence = (
                        row["input_sha256"],
                        row["input_format"],
                        row["state"],
                        row["expected_sequence"],
                    )
                    base = bootstrap if sequence == 0 else row["expected_parent_sha"]
                    if (
                        UUID(row["id"]) != operation_id
                        or metadata.id != corpus
                        or type(sequence) is not int
                        or sequence < 0
                        or state not in ("executing", "uncertain")
                        or kind not in ("zip", "corpus")
                        or not isinstance(digest, str)
                        or not re.fullmatch(r"[0-9a-f]{64}", digest)
                        or not isinstance(base, str)
                        or not re.fullmatch(r"[0-9a-f]{40}", base)
                    ):
                        raise ValueError("Invalid operation snapshot")
                    snapshot = StageObservationTarget(
                        corpus,
                        operation_id,
                        claim,
                        metadata.hf_repo_id,
                        metadata.hf_repo_type,
                        kind,
                        digest,
                        base,
                        state,
                    )
            return snapshot
        except CorpusMetadataConflict:
            raise
        except Exception:
            raise CorpusMetadataError("Stage observation lookup failed") from None
