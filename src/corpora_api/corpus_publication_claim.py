"""Unwired one-shot publication claims for a previously staged save.

The token coordinates trusted internal workers; by itself it does not isolate
credentials or stop remote work. A lost acknowledgement is never reissued.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from corpora_api.corpus_hub_stage import StagedCorpusArchive
from corpora_api.corpus_metadata import (
    CorpusMetadata,
    CorpusMetadataConflict,
    CorpusMetadataError,
    CorpusMetadataStore,
)


@dataclass(frozen=True)
class CorpusPublicationClaim:
    stage: StagedCorpusArchive
    publication_claim: UUID


class CorpusPublicationClaimer:
    """Claim publication once, after a matching durable staging receipt."""

    def __init__(self, connect: Callable[[], Any], *, assume_service_role: bool = False) -> None:
        self.connect = connect
        self.assume_service_role = assume_service_role

    def claim(self, operation_id: UUID) -> CorpusPublicationClaim | None:
        if not isinstance(operation_id, UUID):
            raise CorpusMetadataError("Invalid corpus publication operation")
        token = uuid4()
        result = None
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
                        raise CorpusMetadataConflict("Corpus publication is unavailable")
                    db.execute(
                        "SELECT 1 FROM public.corpora_corpus_metadata WHERE id=%s::uuid FOR UPDATE",
                        (str(identity[0]),),
                    )
                    db.execute(
                        "SELECT 1 FROM public.corpora_corpus_write_ops "
                        "WHERE id=%s::uuid FOR UPDATE",
                        (str(operation_id),),
                    )
                    raw = db.execute(
                        "SELECT row_to_json(s),row_to_json(m) "
                        "FROM public.corpora_corpus_staged_inputs s "
                        "JOIN public.corpora_corpus_write_ops o "
                        "ON o.id=s.operation_id AND o.corpus_id=s.corpus_id "
                        "AND o.worker_claim=s.worker_claim "
                        "JOIN public.corpora_corpus_metadata m ON m.id=o.corpus_id "
                        "LEFT JOIN public.corpora_corpus_bootstrap b ON b.corpus_id=o.corpus_id "
                        "LEFT JOIN public.corpora_corpus_heads h ON h.corpus_id=o.corpus_id "
                        "WHERE s.operation_id=%s::uuid AND o.state='executing' "
                        "AND s.repo_id=m.hf_repo_id AND s.repo_type=m.hf_repo_type "
                        "AND s.input_sha256=o.input_sha256 AND s.input_format=o.input_format AND ("
                        "(o.expected_sequence=0 AND h.corpus_id IS NULL "
                        "AND m.state='provisioning' AND s.requested_base_sha=b.commit_sha) OR "
                        "(o.expected_sequence>0 AND m.state='ready' "
                        "AND h.sequence=o.expected_sequence AND h.commit_sha=o.expected_parent_sha "
                        "AND s.requested_base_sha=o.expected_parent_sha))",
                        (str(operation_id),),
                    ).fetchone()
                    if raw is None:
                        raise CorpusMetadataConflict("Corpus publication is unavailable")
                    row, metadata_raw = raw
                    metadata = CorpusMetadata.model_validate(metadata_raw)
                    CorpusMetadataStore._check_link(metadata)
                    if not isinstance(row, dict):
                        raise ValueError("Invalid stage receipt")
                    stage = StagedCorpusArchive(
                        corpus_id=UUID(row["corpus_id"]),
                        operation_id=UUID(row["operation_id"]),
                        claim_id=UUID(row["worker_claim"]),
                        repo_id=row["repo_id"],
                        repo_type=row["repo_type"],
                        input_format=row["input_format"],
                        archive_size=row["archive_size"],
                        archive_sha256=row["input_sha256"],
                        requested_base_sha=row["requested_base_sha"],
                        stage_ref=row["stage_ref"],
                        stage_sha=row["stage_sha"],
                        stage_path=row["stage_path"],
                    )
                    if (
                        stage.operation_id != operation_id
                        or stage.corpus_id != metadata.id
                        or stage.repo_id != metadata.hf_repo_id
                        or stage.repo_type != metadata.hf_repo_type
                    ):
                        raise ValueError("Mismatched stage receipt")
                    inserted = db.execute(
                        "INSERT INTO public.corpora_corpus_publication_claims "
                        "(operation_id,corpus_id,stage_claim,publication_claim) "
                        "VALUES (%s::uuid,%s::uuid,%s::uuid,%s::uuid) "
                        "ON CONFLICT DO NOTHING RETURNING publication_claim",
                        (str(operation_id), str(stage.corpus_id), str(stage.claim_id), str(token)),
                    ).fetchone()
                    if inserted is not None:
                        if UUID(str(inserted[0])) != token:
                            raise ValueError("Mismatched publication claim")
                        result = CorpusPublicationClaim(stage, token)
            return result
        except CorpusMetadataConflict:
            raise
        except Exception:
            raise CorpusMetadataError(
                "Publication claim failed; reconcile before retrying"
            ) from None
