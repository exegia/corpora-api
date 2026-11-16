"""Private staged draft objects and owner-filtered mutation journal RPCs."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any
from urllib.parse import quote
from uuid import UUID

import httpx
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from corpora_api.corpus_process import MAX_CORPUS_WORKER_RESPONSE_BYTES
from corpora_api.drafts import BUCKET, DraftHead, DraftStore
from corpora_api.mutation_journal import (
    CurationError,
    Intent,
    Receipt,
    Record,
    _operation_id,
    _validate_plan,
)
from corpora_api.validation import MAX_VALIDATION_ARCHIVE_BYTES

MAX_JOURNAL_WITNESS_BYTES = 16_000_000


def _uuid(value: str) -> str:
    try:
        return str(UUID(value))
    except (ValueError, TypeError, AttributeError) as error:
        raise CurationError(404, "Conversation document or change not found") from error


class RevisionFact(BaseModel):
    """Trusted registry facts, never an object path supplied by a caller."""

    model_config = ConfigDict(extra="forbid")
    revision: UUID
    digest: str
    version: str = Field(min_length=1)
    operations: list[UUID] = Field(max_length=2)


class MutationStorage:
    """Use the existing server-only RPC and bounded immutable object store."""

    def __init__(self, drafts: DraftStore) -> None:
        self.drafts = drafts

    @staticmethod
    def _convert(error: HTTPException) -> CurationError:
        return CurationError(error.status_code, str(error.detail))

    def rpc(self, action: str, owner: str, payload: dict[str, Any]) -> Any:
        try:
            return self.drafts._rpc(action, _uuid(owner), payload)
        except HTTPException as error:
            raise self._convert(error) from error

    def resolve(self, owner: str, corpus: str) -> DraftHead:
        if not corpus.startswith("draft:"):
            raise CurationError(404, "Working draft not found")
        try:
            return self.drafts.resolve(_uuid(owner), _uuid(corpus.removeprefix("draft:")))
        except HTTPException as error:
            raise self._convert(error) from error

    def _candidate(
        self, head: DraftHead, revision: str, digest: str, version: str | None = None
    ) -> DraftHead:
        revision_id = _uuid(revision)
        key = f"{head.owner}/ai/{head.document_id}/{revision_id}.corpus"
        try:
            return self.drafts._head(
                {
                    **head.model_dump(mode="json"),
                    "revision": revision_id,
                    "object_key": key,
                    "digest": digest,
                    "version": version if version is not None else head.version,
                },
                str(head.owner),
                str(head.document_id),
            )
        except HTTPException as error:
            raise CurationError(422, "Invalid immutable draft location") from error

    def download(self, head: DraftHead) -> bytes:
        try:
            return self.drafts._download(head)
        except HTTPException as error:
            raise self._convert(error) from error

    def _revision_rpc(self, function: str, payload: dict[str, Any], limit: int) -> Any:
        try:
            url, headers = self.drafts._config()
            with self.drafts.client.stream(
                "POST", url + "/rest/v1/rpc/" + function, headers=headers, json=payload
            ) as response:
                data = bytearray()
                for chunk in response.iter_bytes():
                    data.extend(chunk)
                    if len(data) > limit:
                        raise CurationError(503, "Revision registry returned oversized data")
                value = json.loads(data)
                if response.status_code >= 400:
                    message = value.get("message") if isinstance(value, dict) else None
                    status = {"AI_NOT_FOUND": 404, "AI_CONFLICT": 409}.get(message, 503)
                    raise CurationError(status, "Draft revision is unavailable")
        except HTTPException as error:
            raise self._convert(error) from error
        except (httpx.HTTPError, ValueError) as error:
            raise CurationError(503, "Revision registry is unavailable") from error
        return value

    def download_revision(self, owner: str, corpus: str, revision: str) -> tuple[DraftHead, bytes]:
        """Read a journal-bound private snapshot and verify its immutable bytes.

        Publishing still requires a conditional write protocol. Imported archive
        history and object retention are outside this lookup's guarantees.
        """
        owner, revision = _uuid(owner), _uuid(revision)
        head = self.resolve(owner, corpus)
        value = self._revision_rpc(
            "corpora_ai_revision_lookup",
            {"p_owner": owner, "p_document": str(head.document_id), "p_revision": revision},
            MAX_CORPUS_WORKER_RESPONSE_BYTES,
        )
        try:
            fact = RevisionFact.model_validate(value)
            if str(fact.revision) != revision or len(set(fact.operations)) != len(fact.operations):
                raise ValueError("invalid revision identity")
            if not fact.operations and revision != str(head.revision):
                raise ValueError("historical revision has no applied witness")
            if revision == str(head.revision) and (
                fact.digest != head.digest or fact.version != head.version
            ):
                raise ValueError("head changed during revision lookup")
        except (ValidationError, ValueError) as error:
            raise CurationError(503, "Revision registry returned invalid data") from error

        for operation in fact.operations:
            operation_id = str(operation)
            record_value = self._revision_rpc(
                "corpora_ai_storage",
                {"p_action": "get", "p_owner": owner, "p_payload": {"id": operation_id}},
                MAX_JOURNAL_WITNESS_BYTES,
            )
            receipt_value = self._revision_rpc(
                "corpora_ai_storage",
                {"p_action": "receipt", "p_owner": owner, "p_payload": {"id": operation_id}},
                MAX_CORPUS_WORKER_RESPONSE_BYTES,
            )
            try:
                record = Record.model_validate(record_value)
                receipt = Receipt.model_validate(receipt_value)
                intent = record.intent
                if (
                    intent.owner != owner
                    or intent.id != operation_id
                    or intent.id != _operation_id(owner, intent.suggestion.id, intent.reverts)
                    or intent.suggestion.scope.corpus != head.corpus
                    or record.applied_at is None
                    or record.applied_at != receipt.applied_at
                    or intent.proof != receipt.proof
                ):
                    raise ValueError("invalid publication witness")
                _validate_plan(intent.suggestion, intent.plan)
                if not any(
                    snapshot.revision == revision
                    and snapshot.digest == fact.digest
                    and snapshot.version == fact.version
                    for snapshot in (intent.plan.before, intent.plan.after)
                ):
                    raise ValueError("revision does not match witness")
            except (ValidationError, ValueError, CurationError) as error:
                raise CurationError(503, "Revision registry returned invalid data") from error
        try:
            candidate = self._candidate(head, revision, fact.digest, fact.version)
        except CurationError as error:
            raise CurationError(503, "Revision registry returned invalid data") from error
        return candidate, self.download(candidate)

    def stage(self, head: DraftHead, revision: str, data: bytes, digest: str) -> str:
        candidate = self._candidate(head, revision, digest)
        if len(data) > MAX_VALIDATION_ARCHIVE_BYTES:
            raise CurationError(413, "Working draft archive exceeds size limits")
        if "sha256:" + hashlib.sha256(data).hexdigest() != digest:
            raise CurationError(409, "Prepared draft digest mismatch")
        try:
            url, headers = self.drafts._config()
            result = self.drafts.client.post(
                f"{url}/storage/v1/object/{BUCKET}/{quote(candidate.object_key, safe='/')}",
                headers={**headers, "Content-Type": "application/zip", "x-upsert": "false"},
                content=data,
            )
        except (httpx.HTTPError, HTTPException) as error:
            raise CurationError(503, "Staged draft upload failed") from error
        if result.status_code not in (200, 201, 400, 409):
            raise CurationError(503, "Staged draft upload failed")
        self.download(
            candidate
        )  # A retry or successful upload is valid only after digest readback.
        return candidate.object_key

    def receipt(self, owner: str, operation_id: str) -> Receipt | None:
        value = self.rpc("receipt", owner, {"id": _uuid(operation_id)})
        try:
            return Receipt.model_validate(value) if value is not None else None
        except ValidationError as error:
            raise CurationError(503, "Change storage returned invalid data") from error

    def publish(self, intent: Intent) -> bool:
        head = self.resolve(intent.owner, intent.suggestion.scope.corpus)
        if head.state != "draft":
            raise CurationError(423, "Corpus is published or locked")
        candidate = self._candidate(head, intent.plan.after.revision, intent.plan.after.digest)
        self.download(candidate)
        result = self.rpc(
            "publish",
            intent.owner,
            {"id": _uuid(intent.id), "proof": intent.proof, "object_key": candidate.object_key},
        )
        if type(result) is not bool:
            raise CurationError(503, "Change storage returned invalid data")
        return result


class SupabaseMutationJournal:
    def __init__(self, storage: MutationStorage) -> None:
        self.storage = storage

    @staticmethod
    def _record(value: Any, owner: str, operation_id: str) -> Record:
        try:
            record = Record.model_validate(value)
            if record.intent.owner != _uuid(owner) or record.intent.id != _uuid(operation_id):
                raise ValueError("invalid owner or operation")
            return record
        except (ValidationError, ValueError) as error:
            raise CurationError(503, "Change storage returned invalid data") from error

    def get(self, owner: str, operation_id: str) -> Record | None:
        value = self.storage.rpc("get", owner, {"id": _uuid(operation_id)})
        return self._record(value, owner, operation_id) if value is not None else None

    def insert(self, intent: Intent) -> Record:
        _validate_plan(intent.suggestion, intent.plan)
        value = self.storage.rpc(
            "insert",
            intent.owner,
            {
                "intent": intent.model_dump(mode="json"),
                "proof": intent.proof,
                "entries": [
                    entry.model_dump(mode="json") for entry in intent.entries(intent.created_at)
                ],
            },
        )
        return self._record(value, intent.owner, intent.id)

    def finish(self, owner: str, operation_id: str, applied_at: datetime) -> Record:
        # The durable receipt, not the caller's timestamp, controls finalization.
        value = self.storage.rpc("finish", owner, {"id": _uuid(operation_id)})
        return self._record(value, owner, operation_id)
