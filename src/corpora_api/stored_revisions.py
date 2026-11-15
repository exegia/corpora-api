"""Unwired server-only immutable objects for the proposed archive registry.

The caller must authenticate the owner, validate archive contents, and coordinate
legacy readers, imports and deletes before these operations can be enabled.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import quote
from uuid import UUID

import httpx
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from corpora_api.storage import StoredCorpusNotFound, archive_name
from corpora_api.storage_detail import MAX_STORED_ARCHIVE_BYTES

BUCKET = "corpus-stored-revisions"
MAX_REGISTRY_RESPONSE = 65_536


class RegistryConflict(HTTPException):
    def __init__(self, reason: str) -> None:
        super().__init__(409, "Stored revision is unavailable")
        self.reason = reason


class Revision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    owner: UUID
    filename: str
    revision: UUID
    digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    version: str = Field(min_length=1, max_length=128)


class HistoryFact(Revision):
    created_at: datetime


@dataclass(frozen=True)
class HistoryCursor:
    created_at: datetime
    revision: UUID


@dataclass(frozen=True)
class HistoryPage:
    entries: list[HistoryFact]
    next_cursor: HistoryCursor | None


@dataclass(frozen=True)
class Snapshot:
    fact: Revision
    data: bytes


class StoredRevisions:
    """Stage immutable bytes and publish facts through the trusted registry."""

    def __init__(self, client: httpx.Client | None = None) -> None:
        self.client = client or httpx.Client(timeout=30)

    @staticmethod
    def _identity(
        owner: str, filename: str, revision: str | None = None
    ) -> tuple[str, str, str | None]:
        try:
            owner = str(UUID(owner))
            if archive_name(filename) != filename:
                raise ValueError("canonical archive filename required")
            revision = str(UUID(revision)) if revision is not None else None
        except (ValueError, TypeError, AttributeError, StoredCorpusNotFound) as error:
            raise HTTPException(404, "Stored revision not found") from error
        return owner, filename, revision

    @staticmethod
    def _config() -> tuple[str, dict[str, str]]:
        project = os.getenv("PROJECT_REF")
        url = os.getenv("SUPABASE_URL") or (f"https://{project}.supabase.co" if project else "")
        key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
        if not url or not key:
            raise HTTPException(503, "Stored revision service unavailable")
        return url.rstrip("/"), {"Authorization": f"Bearer {key}", "apikey": key}

    @staticmethod
    def _key(fact: Revision) -> str:
        name_digest = hashlib.sha256(fact.filename.encode("utf-8")).hexdigest()
        return f"{fact.owner}/{name_digest}/{fact.revision}.corpus"

    def _rpc(self, name: str, payload: dict[str, object]) -> object:
        url, headers = self._config()
        try:
            with self.client.stream(
                "POST", f"{url}/rest/v1/rpc/{name}", headers=headers, json=payload
            ) as response:
                data = bytearray()
                for chunk in response.iter_bytes():
                    data.extend(chunk)
                    if len(data) > MAX_REGISTRY_RESPONSE:
                        raise HTTPException(503, "Stored revision registry returned oversized data")
                if response.status_code >= 400:
                    try:
                        value = json.loads(data)
                    except ValueError:
                        value = None
                    message = value.get("message") if isinstance(value, dict) else None
                    code = value.get("code") if isinstance(value, dict) else None
                    if message == "ARCHIVE_CONFLICT" or code == "23505":
                        raise RegistryConflict("duplicate" if code == "23505" else "stale")
                    status = 404 if message == "ARCHIVE_NOT_FOUND" else 503
                    raise HTTPException(status, "Stored revision is unavailable")
                return json.loads(data) if data else None
        except HTTPException:
            raise
        except (httpx.HTTPError, ValueError) as error:
            raise HTTPException(503, "Stored revision registry is unavailable") from error

    def _download(self, fact: Revision) -> bytes:
        url, headers = self._config()
        path = quote(self._key(fact), safe="/")
        try:
            with self.client.stream(
                "GET", f"{url}/storage/v1/object/{BUCKET}/{path}", headers=headers
            ) as response:
                if response.status_code != 200:
                    raise HTTPException(503, "Stored revision object unavailable")
                data = bytearray()
                for chunk in response.iter_bytes():
                    data.extend(chunk)
                    if len(data) > MAX_STORED_ARCHIVE_BYTES:
                        raise HTTPException(413, "Stored revision exceeds size limit")
        except httpx.HTTPError as error:
            raise HTTPException(503, "Stored revision object unavailable") from error
        result = bytes(data)
        if "sha256:" + hashlib.sha256(result).hexdigest() != fact.digest:
            raise HTTPException(409, "Stored revision digest mismatch")
        return result

    def get(self, owner: str, filename: str, revision: str | None = None) -> Snapshot:
        owner, filename, revision = self._identity(owner, filename, revision)
        result = self._rpc(
            "corpora_stored_get", {"p_owner": owner, "p_filename": filename, "p_revision": revision}
        )
        if result is None:
            raise HTTPException(404, "Stored revision not found")
        try:
            fact = Revision.model_validate(result)
            if (
                str(fact.owner) != owner
                or fact.filename != filename
                or (revision and str(fact.revision) != revision)
            ):
                raise ValueError("registry identity mismatch")
        except (ValidationError, ValueError) as error:
            raise HTTPException(503, "Stored revision registry returned invalid data") from error
        return Snapshot(fact, self._download(fact))

    def history(
        self, owner: str, filename: str, limit: int, cursor: HistoryCursor | None = None
    ) -> HistoryPage:
        """Enumerate registry facts; object availability and legacy history need separate checks."""
        owner, filename, _ = self._identity(owner, filename)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise HTTPException(422, "Invalid history page size")
        if cursor is not None and (
            not isinstance(cursor, HistoryCursor)
            or not isinstance(cursor.created_at, datetime)
            or cursor.created_at.utcoffset() is None
            or not isinstance(cursor.revision, UUID)
        ):
            raise HTTPException(422, "Invalid history cursor")
        value = self._rpc(
            "corpora_stored_history",
            {
                "p_owner": owner,
                "p_filename": filename,
                "p_limit": limit,
                "p_before": cursor.created_at.isoformat() if cursor else None,
                "p_before_revision": str(cursor.revision) if cursor else None,
            },
        )
        try:
            if not isinstance(value, list) or len(value) > limit + 1:
                raise ValueError("invalid history page")
            entries = [HistoryFact.model_validate(row) for row in value]
            boundary = (cursor.created_at, cursor.revision.int) if cursor else None
            for entry in entries:
                if (
                    str(entry.owner) != owner
                    or entry.filename != filename
                    or entry.created_at.utcoffset() is None
                ):
                    raise ValueError("invalid history identity")
                key = (entry.created_at, entry.revision.int)
                if boundary is not None and key >= boundary:
                    raise ValueError("invalid history order")
                boundary = key
        except (ValidationError, ValueError, TypeError) as error:
            raise HTTPException(503, "Stored revision registry returned invalid history") from error
        more = len(entries) > limit
        entries = entries[:limit]
        next_cursor = HistoryCursor(entries[-1].created_at, entries[-1].revision) if more else None
        return HistoryPage(entries, next_cursor)

    def facts(self, owner: str, filename: str, revisions: list[UUID]) -> dict[UUID, Revision]:
        """Resolve at most 100 registry IDs without inferring object availability."""
        owner, filename, _ = self._identity(owner, filename)
        if not 1 <= len(revisions) <= 100 or any(not isinstance(v, UUID) for v in revisions):
            raise HTTPException(422, "Invalid revision selection")
        requested = set(revisions)
        if len(requested) != len(revisions):
            raise HTTPException(422, "Invalid revision selection")
        value = self._rpc(
            "corpora_stored_revision_facts",
            {
                "p_owner": owner,
                "p_filename": filename,
                "p_revisions": [str(revision) for revision in revisions],
            },
        )
        try:
            if not isinstance(value, list) or len(value) > len(requested):
                raise ValueError("invalid revision facts")
            result: dict[UUID, Revision] = {}
            for item in value:
                fact = Revision.model_validate(item)
                if (
                    str(fact.owner) != owner
                    or fact.filename != filename
                    or fact.revision not in requested
                    or fact.revision in result
                ):
                    raise ValueError("invalid revision binding")
                result[fact.revision] = fact
            return result
        except (ValidationError, ValueError, TypeError) as error:
            raise HTTPException(503, "Stored revision registry returned invalid facts") from error

    def stage(self, fact: Revision, data: bytes) -> None:
        self._identity(str(fact.owner), fact.filename, str(fact.revision))
        if len(data) > MAX_STORED_ARCHIVE_BYTES:
            raise HTTPException(413, "Stored revision exceeds size limit")
        if (
            not re.fullmatch(r"sha256:[0-9a-f]{64}", fact.digest)
            or "sha256:" + hashlib.sha256(data).hexdigest() != fact.digest
        ):
            raise HTTPException(409, "Stored revision digest mismatch")
        url, headers = self._config()
        path = quote(self._key(fact), safe="/")
        try:
            response = self.client.post(
                f"{url}/storage/v1/object/{BUCKET}/{path}",
                headers={**headers, "Content-Type": "application/zip", "x-upsert": "false"},
                content=data,
            )
        except httpx.HTTPError as error:
            raise HTTPException(503, "Stored revision staging failed") from error
        if response.status_code not in (200, 201, 400, 409):
            raise HTTPException(503, "Stored revision staging failed")
        self._download(fact)

    def publish(self, fact: Revision, data: bytes, expected: str | None) -> None:
        if expected is not None:
            _, _, expected = self._identity(str(fact.owner), fact.filename, expected)
        self.stage(fact, data)
        payload: dict[str, object] = {
            "p_owner": str(fact.owner),
            "p_filename": fact.filename,
            "p_revision": str(fact.revision),
            "p_digest": fact.digest,
            "p_version": fact.version,
        }
        if expected is None:
            self._rpc("corpora_stored_create", payload)
        else:
            self._rpc("corpora_stored_publish", {**payload, "p_expected": expected})
