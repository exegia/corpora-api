"""Unwired session-bound admission of one repository-provisioning attempt.

A claim records an authorized database decision, not proof of Hub creation.
An accepted creation may proceed after logout, but a claim must never be
automatically redispatched after an ambiguous outcome.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from fastapi import HTTPException

from corpora_api.corpus_metadata import (
    CorpusMetadataConflict,
    CorpusMetadataError,
    CorpusMetadataNotFound,
    _normalize_reservation,
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

        def operation(actor: str, db: Any) -> bytes:
            try:
                corpus = UUID(corpus_id)
                session = UUID(trusted_claims["session_id"])
            except (ValueError, TypeError, KeyError, AttributeError) as error:
                raise HTTPException(422, "Invalid corpus provisioning request") from error
            try:
                result = db.execute(
                    "SELECT corpora_session_guard.admit_provision(%s::uuid,%s::uuid,%s,%s::uuid)",
                    (actor, str(session), trusted_claims["exp"], str(corpus)),
                ).fetchone()
                value = result[0]
                if value in ({"status": "not_found"}, {"status": "conflict"}):
                    return json.dumps(value).encode()
                if (
                    not isinstance(value, dict)
                    or set(value)
                    != {"status", "operation_id", "claim_id", "repo_id", "repo_type", "revision"}
                    or value["status"] != "ok"
                ):
                    raise ValueError("Invalid admission receipt")
                UUID(value["operation_id"])
                UUID(value["claim_id"])
                _, _, expected_repo = _normalize_reservation(
                    corpus_id, "provision", value["repo_type"]
                )
                if (
                    value["repo_id"] != expected_repo
                    or type(value["revision"]) is not int
                    or value["revision"] < 1
                ):
                    raise ValueError("Mismatched admission receipt")
                return json.dumps(value).encode()
            except (ValueError, TypeError, KeyError, AttributeError, CorpusMetadataError) as error:
                raise HTTPException(503, "Corpus provisioning admission failed") from error

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
                UUID(value["operation_id"]),
                UUID(value["claim_id"]),
                corpus,
                actor,
                session,
                value["repo_id"],
                value["repo_type"],
                value["revision"],
            )
        except (ValueError, TypeError, KeyError, AttributeError) as error:
            raise CorpusMetadataError("Corpus provisioning returned invalid data") from error
