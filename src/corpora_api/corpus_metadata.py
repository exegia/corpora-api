"""Unwired owner-bound corpus metadata registry.

Callers must supply the subject of a verified, live session. Repository creation
and the transition to ready are deliberately not implemented here.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

import httpx
from huggingface_hub.utils import HFValidationError, validate_repo_id
from pydantic import BaseModel, ConfigDict, Field, ValidationError


class CorpusMetadataError(Exception):
    """The registry could not safely process a request."""


class CorpusMetadataNotFound(CorpusMetadataError):
    """The requested record is absent or not accessible to this actor."""


class CorpusMetadataConflict(CorpusMetadataError):
    """A metadata revision or repository identifier conflicts."""


class CorpusMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    owner_id: UUID
    title: str = Field(min_length=1, max_length=512)
    description: str | None
    language: str | None
    properties: dict[str, Any]
    hf_repo_id: str
    hf_repo_type: Literal["dataset", "model"]
    state: Literal["provisioning", "ready", "failed"]
    revision: int = Field(gt=0)
    created_at: datetime
    updated_at: datetime


def _same_json(left: Any, right: Any) -> bool:
    """Compare JSON while keeping booleans distinct from numeric values."""
    pending = [(left, right)]
    while pending:
        a, b = pending.pop()
        if isinstance(a, bool) or isinstance(b, bool):
            if type(a) is not type(b) or a != b:
                return False
        elif isinstance(a, dict) and isinstance(b, dict):
            if a.keys() != b.keys():
                return False
            pending.extend((a[key], b[key]) for key in a)
        elif isinstance(a, list) and isinstance(b, list):
            if len(a) != len(b):
                return False
            pending.extend(zip(a, b, strict=True))
        elif a != b:
            return False
    return True


def _normalize_changes(revision: int, fields: dict[str, Any]) -> dict[str, Any]:
    if (
        type(revision) is not int
        or not 1 <= revision < (1 << 63) - 1
        or not isinstance(fields, dict)
        or not fields
        or not fields.keys() <= {"title", "description", "language", "properties"}
    ):
        raise CorpusMetadataError("Invalid corpus metadata update")
    changes = dict(fields)
    if "title" in changes:
        title = changes["title"]
        if not isinstance(title, str) or not 1 <= len(title.strip()) <= 512:
            raise CorpusMetadataError("Invalid corpus metadata update")
        changes["title"] = title.strip()
    for field, limit in (("description", 10000), ("language", 128)):
        if field in changes:
            value = changes[field]
            if value is not None and (not isinstance(value, str) or len(value) > limit):
                raise CorpusMetadataError("Invalid corpus metadata update")
    try:
        encoded = json.dumps(changes, ensure_ascii=False, allow_nan=False).encode("utf-8")
        snapshot = json.loads(encoded)
        if snapshot != changes:
            raise ValueError("Invalid metadata values")
        changes = snapshot
        pending: list[Any] = [changes]
        while pending:
            item = pending.pop()
            if isinstance(item, str) and "\x00" in item:
                raise ValueError("Invalid metadata text")
            if isinstance(item, dict):
                pending.extend(item.keys())
                pending.extend(item.values())
            elif isinstance(item, list):
                pending.extend(item)
        if "properties" in changes:
            properties = changes["properties"]
            if not isinstance(properties, dict):
                raise ValueError("Invalid metadata properties")
            raw = json.dumps(properties, ensure_ascii=False, allow_nan=False).encode("utf-8")
            if len(raw) > 16384 or json.loads(raw) != properties:
                raise ValueError("Invalid metadata properties")
    except (ValueError, TypeError, UnicodeError, RecursionError) as error:
        raise CorpusMetadataError("Invalid corpus metadata update") from error
    return changes


class CorpusMetadataStore:
    """Use a server credential while enforcing ownership in every query."""

    def __init__(self, client: httpx.Client | None = None) -> None:
        self.client = client or httpx.Client(timeout=15)

    @staticmethod
    def _actor(value: str) -> str:
        try:
            return str(UUID(value))
        except (ValueError, TypeError, AttributeError) as error:
            raise CorpusMetadataError("Verified user identity is invalid") from error

    @staticmethod
    def _config() -> tuple[str, dict[str, str]]:
        project = os.getenv("PROJECT_REF")
        url = os.getenv("SUPABASE_URL") or (f"https://{project}.supabase.co" if project else "")
        key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
        if not url or not key:
            raise CorpusMetadataError("Corpus metadata registry is unavailable")
        return url.rstrip("/") + "/rest/v1/corpora_corpus_metadata", {
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Prefer": "return=representation",
        }

    @staticmethod
    def _is_superadmin(actor: str) -> bool:
        configured = os.getenv("CORPORA_SUPERADMIN_USER_ID")
        if not configured:
            return False
        try:
            return UUID(configured) == UUID(actor)
        except (ValueError, TypeError, AttributeError) as error:
            raise CorpusMetadataError("Superadmin configuration is invalid") from error

    @staticmethod
    def _check_link(row: CorpusMetadata) -> None:
        CorpusMetadataStore._check_link_in_org(row, os.getenv("HF_CORPUS_ORGANIZATION", ""))

    @staticmethod
    def _check_link_in_org(row: CorpusMetadata, organization: str) -> None:
        expected = f"{organization}/corpus-{row.id}"
        if not organization or row.hf_repo_id != expected:
            raise CorpusMetadataError("Corpus metadata has an invalid repository link")
        try:
            validate_repo_id(expected)
        except HFValidationError as error:
            raise CorpusMetadataError("Corpus metadata has an invalid repository link") from error

    def _request(self, method: str, **kwargs: Any) -> list[CorpusMetadata]:
        url, headers = self._config()
        try:
            response = self.client.request(method, url, headers=headers, **kwargs)
        except httpx.HTTPError as error:
            raise CorpusMetadataError("Corpus metadata registry is unavailable") from error
        if response.status_code == 409:
            raise CorpusMetadataConflict("Corpus metadata conflicts with an existing record")
        if method == "PATCH" and response.status_code in (400, 422):
            try:
                problem = response.json()
            except ValueError:
                problem = None
            if isinstance(problem, dict) and problem.get("code") == "23514":
                raise CorpusMetadataError("Invalid corpus metadata update")
        if response.status_code >= 400:
            raise CorpusMetadataError("Corpus metadata registry is unavailable")
        try:
            values = response.json()
            if not isinstance(values, list) or len(values) > 101:
                raise ValueError("Invalid result")
            return [CorpusMetadata.model_validate(value) for value in values]
        except (ValueError, TypeError, ValidationError) as error:
            raise CorpusMetadataError("Corpus metadata registry returned invalid data") from error

    def reserve(
        self,
        verified_user: str,
        corpus_id: str,
        title: str,
        *,
        repo_type: Literal["dataset", "model"],
    ) -> CorpusMetadata:
        """Reserve a stable link; this does not create or verify a Hub repository."""
        owner = self._actor(verified_user)
        organization = os.getenv("HF_CORPUS_ORGANIZATION", "")
        try:
            identifier = UUID(corpus_id)
            repo_id = f"{organization}/corpus-{identifier}"
            if not organization or "/" in organization:
                raise ValueError("Invalid organization")
            validate_repo_id(repo_id)
            if repo_type not in {"dataset", "model"} or not isinstance(title, str):
                raise ValueError("Invalid metadata")
            if not 1 <= len(title.strip()) <= 512:
                raise ValueError("Invalid title")
        except (ValueError, TypeError, AttributeError, HFValidationError) as error:
            raise CorpusMetadataError("Corpus metadata or organization is invalid") from error
        try:
            rows = self._request(
                "POST",
                json={
                    "id": str(identifier),
                    "owner_id": owner,
                    "title": title.strip(),
                    "hf_repo_id": repo_id,
                    "hf_repo_type": repo_type,
                },
            )
        except CorpusMetadataConflict as error:
            try:
                existing = self.get(owner, str(identifier))
            except CorpusMetadataNotFound:
                raise error from None
            if (
                existing.title != title.strip()
                or existing.hf_repo_id != repo_id
                or existing.hf_repo_type != repo_type
                or str(existing.owner_id) != owner
            ):
                raise error from None
            return existing
        if len(rows) != 1 or rows[0].id != identifier or str(rows[0].owner_id) != owner:
            raise CorpusMetadataError("Corpus metadata registry returned invalid data")
        if rows[0].hf_repo_id != repo_id or rows[0].hf_repo_type != repo_type:
            raise CorpusMetadataError("Corpus metadata registry returned invalid data")
        if (
            rows[0].state != "provisioning"
            or rows[0].title != title.strip()
            or rows[0].revision != 1
        ):
            raise CorpusMetadataError("Corpus metadata registry returned invalid data")
        return rows[0]

    def get(self, verified_user: str, corpus_id: str) -> CorpusMetadata:
        actor = self._actor(verified_user)
        try:
            identifier = str(UUID(corpus_id))
        except (ValueError, TypeError, AttributeError) as error:
            raise CorpusMetadataNotFound("Corpus not found") from error
        params = {"id": "eq." + identifier, "limit": "1"}
        if not self._is_superadmin(actor):
            params["owner_id"] = "eq." + actor
        rows = self._request("GET", params=params)
        if not rows:
            raise CorpusMetadataNotFound("Corpus not found")
        row = rows[0]
        if str(row.id) != identifier or (
            str(row.owner_id) != actor and not self._is_superadmin(actor)
        ):
            raise CorpusMetadataError("Corpus metadata registry returned invalid data")
        self._check_link(row)
        return row

    def list_owned(
        self,
        verified_user: str,
        *,
        owner_id: str | None = None,
        limit: int = 50,
        after: str | None = None,
    ) -> tuple[list[CorpusMetadata], str | None]:
        """List one owner's records with a stable, bounded corpus-ID cursor."""
        actor = self._actor(verified_user)
        owner = self._actor(owner_id) if owner_id is not None else actor
        if owner != actor and not self._is_superadmin(actor):
            raise CorpusMetadataNotFound("Corpus owner not found")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise CorpusMetadataError("Invalid corpus metadata limit")
        try:
            cursor = UUID(after) if after is not None else None
        except (ValueError, TypeError, AttributeError) as error:
            raise CorpusMetadataError("Invalid corpus metadata cursor") from error
        params = {"owner_id": "eq." + owner, "order": "id.asc", "limit": str(limit + 1)}
        if cursor is not None:
            params["id"] = "gt." + str(cursor)
        rows = self._request("GET", params=params)
        previous = cursor
        for row in rows:
            if str(row.owner_id) != owner or (previous is not None and row.id <= previous):
                raise CorpusMetadataError("Corpus metadata registry returned invalid data")
            self._check_link(row)
            previous = row.id
        if len(rows) > limit + 1:
            raise CorpusMetadataError("Corpus metadata registry returned invalid data")
        page = rows[:limit]
        return page, str(page[-1].id) if len(rows) > limit else None

    def update_title(
        self, verified_user: str, corpus_id: str, revision: int, title: str
    ) -> CorpusMetadata:
        """Update the title only if the actor can edit and the revision is current."""
        return self.update_metadata(verified_user, corpus_id, revision, {"title": title})

    def update_metadata(
        self, verified_user: str, corpus_id: str, revision: int, fields: dict[str, Any]
    ) -> CorpusMetadata:
        """Replace selected editable metadata; properties are replaced as a whole."""
        actor = self._actor(verified_user)
        changes = _normalize_changes(revision, fields)
        current = self.get(actor, corpus_id)
        params = {"id": "eq." + str(current.id), "revision": "eq." + str(revision)}
        if not self._is_superadmin(actor):
            params["owner_id"] = "eq." + actor
        rows = self._request("PATCH", params=params, json=changes)
        if not rows:
            # Do not reveal the existence of a record that is no longer accessible.
            self.get(actor, str(current.id))
            raise CorpusMetadataConflict("Corpus metadata changed; reload before editing")
        row = rows[0]
        if (
            len(rows) != 1
            or row.id != current.id
            or row.owner_id != current.owner_id
            or row.revision != revision + 1
            or row.hf_repo_id != current.hf_repo_id
            or row.hf_repo_type != current.hf_repo_type
            or any(not _same_json(getattr(row, key), value) for key, value in changes.items())
        ):
            raise CorpusMetadataError("Corpus metadata registry returned invalid data")
        self._check_link(row)
        return row
