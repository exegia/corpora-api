"""Unwired session-bound admission of one repository-provisioning attempt.

A claim records an authorized database decision, not proof of Hub creation or
permission to dispatch after logout. Do not call a remote worker until its
session, worker-liveness and reconciliation policy has been reviewed.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from fastapi import HTTPException
from pydantic import ValidationError

from corpora_api.corpus_metadata import (
    CorpusMetadata,
    CorpusMetadataConflict,
    CorpusMetadataError,
    CorpusMetadataNotFound,
    CorpusMetadataStore,
)
from corpora_api.session_corpus_metadata import MetadataSessionTransaction


@dataclass(frozen=True)
class ClaimedCorpusProvision:
    operation_id: UUID
    claim_id: UUID
    corpus_id: UUID
    actor_id: UUID
    session_id: UUID
    repo_id: str
    repo_type: str
    metadata_revision: int


class SessionBoundCorpusProvisionAdmission:
    """Create and claim one attempt in a short live-session transaction.

    An ambiguous commit must never be interpreted as a dispatch authorization.
    There is no automatic retry, transfer, recovery or remote work here.
    """

    def __init__(self, lease: MetadataSessionTransaction) -> None:
        self.lease = lease

    def claim(self, verified_claims: Mapping[str, Any], corpus_id: str) -> ClaimedCorpusProvision:
        trusted_claims = dict(verified_claims)
        operation_id, claim_id = uuid4(), uuid4()

        def operation(actor: str, db: Any) -> bytes:
            try:
                corpus = UUID(corpus_id)
                session = UUID(trusted_claims["session_id"])
            except (ValueError, TypeError, KeyError, AttributeError) as error:
                raise HTTPException(422, "Invalid corpus provisioning request") from error
            admin = CorpusMetadataStore._is_superadmin(actor)
            result = db.execute(
                "SELECT row_to_json(corpora_corpus_metadata) FROM public.corpora_corpus_metadata "
                "WHERE id=%s::uuid AND (owner_id=%s::uuid OR %s::boolean) FOR UPDATE",
                (str(corpus), actor, admin),
            ).fetchone()
            if result is None:
                return b'{"status":"not_found"}'
            try:
                metadata = CorpusMetadata.model_validate(result[0])
                if metadata.id != corpus or (str(metadata.owner_id) != actor and not admin):
                    raise ValueError("Mismatched metadata")
                CorpusMetadataStore._check_link(metadata)
            except (ValueError, TypeError, ValidationError, CorpusMetadataError) as error:
                raise HTTPException(503, "Corpus provisioning admission failed") from error
            if metadata.state != "provisioning":
                return b'{"status":"conflict"}'
            if db.execute(
                "SELECT 1 FROM public.corpora_corpus_heads WHERE corpus_id=%s::uuid "
                "UNION ALL SELECT 1 FROM public.corpora_corpus_bootstrap WHERE corpus_id=%s::uuid",
                (str(corpus), str(corpus)),
            ).fetchone():
                return b'{"status":"conflict"}'
            inserted = db.execute(
                "INSERT INTO public.corpora_corpus_provision_ops "
                "(id,corpus_id,actor_id,session_id,repo_id,repo_type) "
                "VALUES (%s::uuid,%s::uuid,%s::uuid,%s::uuid,%s,%s) "
                "ON CONFLICT DO NOTHING RETURNING id",
                (
                    str(operation_id),
                    str(corpus),
                    actor,
                    str(session),
                    metadata.hf_repo_id,
                    metadata.hf_repo_type,
                ),
            ).fetchone()
            if inserted is None:
                return b'{"status":"conflict"}'
            claimed = db.execute(
                "UPDATE public.corpora_corpus_provision_ops SET state='executing', "
                "worker_claim=%s::uuid WHERE id=%s::uuid AND state='admitted' "
                "RETURNING worker_claim",
                (str(claim_id), str(operation_id)),
            ).fetchone()
            if claimed is None or UUID(str(claimed[0])) != claim_id:
                raise HTTPException(503, "Corpus provisioning claim failed")
            return json.dumps(
                {
                    "status": "ok",
                    "repo_id": metadata.hf_repo_id,
                    "repo_type": metadata.hf_repo_type,
                    "revision": metadata.revision,
                }
            ).encode()

        data = self.lease.run(trusted_claims, operation)
        try:
            value = json.loads(data)
            if value == {"status": "not_found"}:
                raise CorpusMetadataNotFound("Corpus not found")
            if value == {"status": "conflict"}:
                raise CorpusMetadataConflict(
                    "Corpus provisioning is already claimed or unavailable"
                )
            if not isinstance(value, dict) or value.get("status") != "ok":
                raise ValueError("Invalid receipt")
            actor = UUID(trusted_claims["sub"])
            corpus = UUID(corpus_id)
            session = UUID(trusted_claims["session_id"])
            if (
                not isinstance(value["repo_id"], str)
                or value["repo_type"] not in ("dataset", "model")
                or type(value["revision"]) is not int
                or value["revision"] < 1
            ):
                raise ValueError("Invalid receipt")
            return ClaimedCorpusProvision(
                operation_id,
                claim_id,
                corpus,
                actor,
                session,
                value["repo_id"],
                value["repo_type"],
                value["revision"],
            )
        except (ValueError, TypeError, KeyError, AttributeError) as error:
            raise CorpusMetadataError("Corpus provisioning returned invalid data") from error
