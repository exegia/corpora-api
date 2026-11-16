"""Unwired, session-authorized admission of corpus save operations.

The caller must supply signature-verified claims and a hash of the complete
validated save input. No Hub I/O occurs while the session lock is held. A
committed admission records permission to continue after sign-out, but a
receipt is not an exclusive worker claim. This module does not implement the
dispatcher, Hub worker, reconciliation, or content-version finalization.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from uuid import UUID

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
class AdmittedCorpusSave:
    operation_id: UUID
    corpus_id: UUID
    actor_id: UUID
    session_id: UUID
    expected_sequence: int
    expected_parent_sha: str | None
    input_sha256: str
    state: str
    retained_ref: str


class SessionBoundCorpusSaveAdmission:
    """Admit a save, or let the same actor reattach from a new live session.

    Reattachment never changes the originally admitted session, and the
    returned receipt alone must not dispatch a worker.
    """

    def __init__(self, lease: MetadataSessionTransaction) -> None:
        self.lease = lease

    def admit(
        self,
        verified_claims: Mapping[str, Any],
        corpus_id: str,
        operation_id: str,
        *,
        expected_sequence: int,
        expected_parent_sha: str | None,
        input_sha256: str,
    ) -> AdmittedCorpusSave:
        def operation(actor: str, db: Any) -> bytes:
            try:
                corpus = UUID(corpus_id)
                identifier = UUID(operation_id)
                session = UUID(verified_claims["session_id"])
                if type(expected_sequence) is not int or not 0 <= expected_sequence < 2**63 - 1:
                    raise ValueError("Invalid expected sequence")
                if (expected_sequence == 0) != (expected_parent_sha is None):
                    raise ValueError("Invalid expected parent")
                if expected_parent_sha is not None and not re.fullmatch(
                    r"[0-9a-f]{40}", expected_parent_sha
                ):
                    raise ValueError("Invalid expected parent")
                if not isinstance(input_sha256, str) or not re.fullmatch(
                    r"[0-9a-f]{64}", input_sha256
                ):
                    raise ValueError("Invalid input hash")
            except (ValueError, TypeError, KeyError, AttributeError) as error:
                raise HTTPException(422, "Invalid corpus save admission") from error

            admin = CorpusMetadataStore._is_superadmin(actor)
            result = db.execute(
                "SELECT row_to_json(corpora_corpus_metadata) "
                "FROM public.corpora_corpus_metadata "
                "WHERE id=%s::uuid AND (owner_id=%s::uuid OR %s::boolean) FOR UPDATE",
                (str(corpus), actor, admin),
            ).fetchone()
            if result is None:
                return b'{"status":"not_found"}'
            try:
                metadata = CorpusMetadata.model_validate(result[0])
                if metadata.id != corpus:
                    raise ValueError("Mismatched corpus")
                CorpusMetadataStore._check_link(metadata)
            except (ValueError, TypeError, ValidationError, CorpusMetadataError) as error:
                raise HTTPException(503, "Corpus save admission failed") from error
            if str(metadata.owner_id) != actor and not admin:
                return b'{"status":"not_found"}'

            existing = db.execute(
                "SELECT row_to_json(corpora_corpus_write_ops) "
                "FROM public.corpora_corpus_write_ops WHERE id=%s::uuid",
                (str(identifier),),
            ).fetchone()
            if existing is not None:
                return _receipt(
                    existing[0],
                    corpus,
                    identifier,
                    actor,
                    expected_sequence,
                    expected_parent_sha,
                    input_sha256,
                )

            head = db.execute(
                "SELECT sequence,commit_sha FROM public.corpora_corpus_heads "
                "WHERE corpus_id=%s::uuid",
                (str(corpus),),
            ).fetchone()
            actual = (head[0], head[1]) if head else (0, None)
            if actual != (expected_sequence, expected_parent_sha):
                return b'{"status":"conflict"}'
            if (expected_sequence == 0 and metadata.state != "provisioning") or (
                expected_sequence > 0 and metadata.state != "ready"
            ):
                return b'{"status":"conflict"}'

            inserted = db.execute(
                "INSERT INTO public.corpora_corpus_write_ops "
                "(id,corpus_id,actor_id,session_id,expected_sequence,"
                "expected_parent_sha,input_sha256) "
                "VALUES (%s::uuid,%s::uuid,%s::uuid,%s::uuid,%s,%s,%s) "
                "ON CONFLICT DO NOTHING RETURNING row_to_json(corpora_corpus_write_ops)",
                (
                    str(identifier),
                    str(corpus),
                    actor,
                    str(session),
                    expected_sequence,
                    expected_parent_sha,
                    input_sha256,
                ),
            ).fetchone()
            if inserted is None:
                return b'{"status":"conflict"}'
            return _receipt(
                inserted[0],
                corpus,
                identifier,
                actor,
                expected_sequence,
                expected_parent_sha,
                input_sha256,
            )

        data = self.lease.run(verified_claims, operation)
        try:
            value = json.loads(data)
            if value == {"status": "not_found"}:
                raise CorpusMetadataNotFound("Corpus not found")
            if value == {"status": "conflict"}:
                raise CorpusMetadataConflict(
                    "Corpus save conflicts with an existing operation or version"
                )
            if not isinstance(value, dict) or value.get("status") != "ok":
                raise ValueError("Invalid admission receipt")
            row = value["row"]
            return AdmittedCorpusSave(
                operation_id=UUID(row["id"]),
                corpus_id=UUID(row["corpus_id"]),
                actor_id=UUID(row["actor_id"]),
                session_id=UUID(row["session_id"]),
                expected_sequence=row["expected_sequence"],
                expected_parent_sha=row["expected_parent_sha"],
                input_sha256=row["input_sha256"],
                state=row["state"],
                retained_ref="corpora-op-" + str(UUID(row["id"])),
            )
        except (ValueError, TypeError, KeyError, AttributeError) as error:
            raise CorpusMetadataError("Corpus save admission returned invalid data") from error


def _receipt(
    row: Any,
    corpus: UUID,
    identifier: UUID,
    actor: str,
    sequence: int,
    parent: str | None,
    input_hash: str,
) -> bytes:
    try:
        if not isinstance(row, dict) or (
            UUID(row["id"]) != identifier
            or UUID(row["corpus_id"]) != corpus
            or str(UUID(row["actor_id"])) != actor
            or type(row["expected_sequence"]) is not int
            or row["expected_sequence"] != sequence
            or row["expected_parent_sha"] != parent
            or row["input_sha256"] != input_hash
            or row["state"] not in {"admitted", "executing", "uncertain", "completed", "failed"}
        ):
            return b'{"status":"conflict"}'
        UUID(row["session_id"])
        return json.dumps({"status": "ok", "row": row}, default=str, separators=(",", ":")).encode()
    except (ValueError, TypeError, KeyError, AttributeError) as error:
        raise HTTPException(503, "Corpus save admission returned invalid data") from error
