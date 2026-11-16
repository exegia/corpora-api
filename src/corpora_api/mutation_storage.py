"""Private staged draft objects and owner-filtered mutation journal RPCs."""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any
from urllib.parse import quote
from uuid import UUID

import httpx
from fastapi import HTTPException
from pydantic import ValidationError

from corpora_api.drafts import BUCKET, DraftHead, DraftStore
from corpora_api.mutation_journal import CurationError, Intent, Receipt, Record, _validate_plan
from corpora_api.validation import MAX_VALIDATION_ARCHIVE_BYTES


def _uuid(value: str) -> str:
    try:
        return str(UUID(value))
    except (ValueError, TypeError, AttributeError) as error:
        raise CurationError(404, "Conversation document or change not found") from error


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

    def _candidate(self, head: DraftHead, revision: str, digest: str) -> DraftHead:
        revision_id = _uuid(revision)
        key = f"{head.owner}/ai/{head.document_id}/{revision_id}.corpus"
        try:
            return self.drafts._head(
                {
                    **head.model_dump(mode="json"),
                    "revision": revision_id,
                    "object_key": key,
                    "digest": digest,
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
