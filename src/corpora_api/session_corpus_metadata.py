"""Unwired metadata writes in the same transaction as a verified-session lock.

The caller must verify JWT signatures before passing claims. This adapter does
not configure the connection or grant access to the session table.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any, Protocol
from uuid import UUID

from fastapi import HTTPException
from pydantic import ValidationError

from corpora_api.corpus_metadata import (
    CorpusMetadata,
    CorpusMetadataConflict,
    CorpusMetadataError,
    CorpusMetadataNotFound,
    CorpusMetadataStore,
    _normalize_changes,
    _normalize_reservation,
    _same_json,
)


class MetadataSessionTransaction(Protocol):
    def run(
        self, verified_claims: Mapping[str, Any], operation: Callable[[str, Any], bytes]
    ) -> bytes: ...


class SessionBoundMetadataTransaction:
    """Hold the live session lock and commit a bounded database-only operation."""

    def __init__(self, connect: Callable[[], Any], *, assume_service_role: bool = False) -> None:
        self.connect = connect
        self.assume_service_role = assume_service_role

    def run(
        self, verified_claims: Mapping[str, Any], operation: Callable[[str, Any], bytes]
    ) -> bytes:
        try:
            owner = UUID(verified_claims["sub"])
            session = UUID(verified_claims["session_id"])
            expiry = verified_claims["exp"]
            if type(expiry) is not int or not 0 < expiry < 2**63:
                raise ValueError("invalid expiration")
        except (KeyError, TypeError, ValueError, AttributeError) as error:
            raise HTTPException(401, "Verified session is required") from error
        try:
            with self.connect() as db:
                with db.transaction():
                    if self.assume_service_role:
                        db.execute("SET LOCAL ROLE service_role")
                    db.execute("SELECT pg_catalog.set_config('statement_timeout','5000',true)")
                    params = (str(owner), str(session), expiry)
                    db.execute("SELECT public.corpora_stored_lock_session(%s,%s,%s)", params)
                    result = operation(str(owner), db)
                    if not isinstance(result, bytes) or len(result) > 131_072:
                        raise HTTPException(503, "Corpus metadata update returned invalid data")
                    db.execute("SELECT public.corpora_stored_lock_session(%s,%s,%s)", params)
            return result
        except HTTPException:
            raise
        except Exception as error:
            primary = getattr(getattr(error, "diag", None), "message_primary", None)
            if getattr(error, "sqlstate", None) == "P0001" and primary == "AUTH_SESSION_INVALID":
                raise HTTPException(401, "Verified session is unavailable") from error
            raise HTTPException(503, "Session-bound metadata update is unavailable") from error


class SessionBoundCorpusMetadata:
    """Reserve and edit metadata while the transaction holds the session lock."""

    def __init__(self, lease: MetadataSessionTransaction) -> None:
        self.lease = lease

    def reserve(
        self,
        verified_claims: Mapping[str, Any],
        corpus_id: str,
        title: str,
        *,
        repo_type: str,
    ) -> CorpusMetadata:
        """Reserve a stable link; this does not create a Hub repository."""

        def operation(actor: str, db: Any) -> bytes:
            try:
                identifier, clean_title, repo_id = _normalize_reservation(
                    corpus_id, title, repo_type
                )
            except CorpusMetadataError as error:
                raise HTTPException(422, "Invalid corpus metadata reservation") from error
            result = db.execute(
                "INSERT INTO public.corpora_corpus_metadata "
                "(id,owner_id,title,hf_repo_id,hf_repo_type) "
                "VALUES (%s::uuid,%s::uuid,%s,%s,%s) ON CONFLICT DO NOTHING "
                "RETURNING row_to_json(corpora_corpus_metadata)",
                (str(identifier), actor, clean_title, repo_id, repo_type),
            ).fetchone()
            inserted = result is not None
            if result is None:
                result = db.execute(
                    "SELECT row_to_json(corpora_corpus_metadata) "
                    "FROM public.corpora_corpus_metadata WHERE id=%s::uuid AND owner_id=%s::uuid",
                    (str(identifier), actor),
                ).fetchone()
                if result is None:
                    return b'{"status":"reservation_conflict"}'
            try:
                row = CorpusMetadata.model_validate(result[0])
                if (
                    row.id != identifier
                    or str(row.owner_id) != actor
                    or row.hf_repo_id != repo_id
                    or row.hf_repo_type != repo_type
                    or row.title != clean_title
                ):
                    if not inserted:
                        return b'{"status":"reservation_conflict"}'
                    raise ValueError("Mismatched metadata")
                if inserted and (row.state != "provisioning" or row.revision != 1):
                    raise ValueError("Invalid initial state")
                CorpusMetadataStore._check_link(row)
                return json.dumps(
                    {"status": "ok", "row": row.model_dump(mode="json")}, ensure_ascii=False
                ).encode("utf-8")
            except (ValueError, TypeError, ValidationError, CorpusMetadataError) as error:
                raise HTTPException(503, "Corpus metadata reservation failed") from error

        return self._run(verified_claims, operation)

    def update(
        self,
        verified_claims: Mapping[str, Any],
        corpus_id: str,
        revision: int,
        fields: dict[str, Any],
    ) -> CorpusMetadata:
        def operation(actor: str, db: Any) -> bytes:
            try:
                identifier = str(UUID(corpus_id))
            except (ValueError, TypeError, AttributeError):
                return b'{"status":"not_found"}'
            try:
                changes = _normalize_changes(revision, fields)
            except CorpusMetadataError as error:
                raise HTTPException(422, "Invalid corpus metadata update") from error
            admin = CorpusMetadataStore._is_superadmin(actor)
            params = (
                "title" in changes,
                changes.get("title"),
                "description" in changes,
                changes.get("description"),
                "language" in changes,
                changes.get("language"),
                "properties" in changes,
                json.dumps(changes.get("properties"), ensure_ascii=False),
                identifier,
                revision,
                actor,
                admin,
            )
            try:
                result = db.execute(
                    "UPDATE public.corpora_corpus_metadata SET "
                    "title=CASE WHEN %s THEN %s::text ELSE title END, "
                    "description=CASE WHEN %s THEN %s::text ELSE description END, "
                    "language=CASE WHEN %s THEN %s::text ELSE language END, "
                    "properties=CASE WHEN %s THEN %s::jsonb ELSE properties END "
                    "WHERE id=%s::uuid AND revision=%s AND (owner_id=%s::uuid OR %s::boolean) "
                    "RETURNING row_to_json(corpora_corpus_metadata)",
                    params,
                ).fetchone()
            except Exception as error:
                if getattr(error, "sqlstate", None) == "23514":
                    raise HTTPException(422, "Invalid corpus metadata update") from error
                raise
            if result is None:
                visible = db.execute(
                    "SELECT 1 FROM public.corpora_corpus_metadata "
                    "WHERE id=%s::uuid AND (owner_id=%s::uuid OR %s::boolean)",
                    (identifier, actor, admin),
                ).fetchone()
                return b'{"status":"conflict"}' if visible else b'{"status":"not_found"}'
            try:
                row = CorpusMetadata.model_validate(result[0])
                if (
                    str(row.id) != identifier
                    or (str(row.owner_id) != actor and not admin)
                    or row.revision != revision + 1
                    or any(
                        not _same_json(getattr(row, key), value) for key, value in changes.items()
                    )
                ):
                    raise ValueError("Mismatched metadata")
                CorpusMetadataStore._check_link(row)
                encoded = json.dumps(
                    {"status": "ok", "row": row.model_dump(mode="json")}, ensure_ascii=False
                )
            except (ValueError, TypeError, ValidationError, CorpusMetadataError) as error:
                # An invalid result must abort the transaction, not commit a write.
                raise HTTPException(503, "Corpus metadata update failed") from error
            return encoded.encode("utf-8")

        return self._run(verified_claims, operation)

    def _run(
        self, verified_claims: Mapping[str, Any], operation: Callable[[str, Any], bytes]
    ) -> CorpusMetadata:
        try:
            response = self.lease.run(verified_claims, operation)
        except HTTPException as error:
            if error.status_code == 422 and error.detail in {
                "Invalid corpus metadata update",
                "Invalid corpus metadata reservation",
            }:
                raise CorpusMetadataError(error.detail) from error
            raise
        try:
            result = json.loads(response)
            if result == {"status": "not_found"}:
                raise CorpusMetadataNotFound("Corpus not found")
            if result == {"status": "conflict"}:
                raise CorpusMetadataConflict("Corpus metadata changed; reload before editing")
            if result == {"status": "reservation_conflict"}:
                raise CorpusMetadataConflict("Corpus metadata conflicts with an existing record")
            if (
                not isinstance(result, dict)
                or set(result) != {"status", "row"}
                or result["status"] != "ok"
            ):
                raise ValueError("Invalid result")
            return CorpusMetadata.model_validate(result["row"])
        except (CorpusMetadataNotFound, CorpusMetadataConflict):
            raise
        except (ValueError, TypeError, KeyError, ValidationError) as error:
            raise CorpusMetadataError("Corpus metadata registry returned invalid data") from error
