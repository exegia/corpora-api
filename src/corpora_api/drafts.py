"""Owner-bound working drafts using the legacy Supabase draft registry."""

from __future__ import annotations

import hashlib
import json
import os
import re
import zipfile
from typing import Any, Literal
from urllib.parse import quote
from uuid import NAMESPACE_URL, UUID, uuid5

import httpx
from corpus_validate import read_archive
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, ValidationError

from corpora_api.validation import MAX_VALIDATION_ARCHIVE_BYTES

BUCKET = "corpus-ai-drafts"


class DraftHead(BaseModel):
    model_config = ConfigDict(extra="forbid")
    document_id: UUID
    owner: UUID
    corpus: str
    bucket: str
    object_key: str
    revision: UUID
    digest: str
    version: str
    state: Literal["draft", "locked", "published"] = "draft"


class DraftStore:
    def __init__(self, client: httpx.Client | None = None) -> None:
        self.client = client or httpx.Client(timeout=30)

    def _config(self) -> tuple[str, dict[str, str]]:
        project = os.getenv("PROJECT_REF")
        url = os.getenv("SUPABASE_URL") or (f"https://{project}.supabase.co" if project else "")
        key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
        if not url or not key:
            raise HTTPException(status_code=503, detail="Working draft storage is unavailable")
        return url.rstrip("/"), {"Authorization": f"Bearer {key}", "apikey": key}

    def _rpc(self, action: str, owner: str, payload: dict[str, Any]) -> Any:
        url, headers = self._config()
        try:
            response = self.client.post(
                url + "/rest/v1/rpc/corpora_ai_storage",
                headers=headers,
                json={"p_action": action, "p_owner": owner, "p_payload": payload},
            )
            value = response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise HTTPException(
                status_code=503, detail="Working draft storage is unavailable"
            ) from error
        if response.status_code >= 400:
            message = value.get("message") if isinstance(value, dict) else None
            status = {
                "AI_NOT_FOUND": 404,
                "AI_LOCKED": 423,
                "AI_CONFLICT": 409,
                "AI_INVALID": 422,
            }.get(message, 503)
            raise HTTPException(status_code=status, detail="Working draft is unavailable")
        return value

    def _head(self, value: Any, owner: str, draft_id: str | None = None) -> DraftHead:
        try:
            head = DraftHead.model_validate(value)
            expected_owner = UUID(owner)
            if head.owner != expected_owner or head.corpus != f"draft:{head.document_id}":
                raise ValueError("invalid owner binding")
            if draft_id is not None and head.document_id != UUID(draft_id):
                raise ValueError("invalid draft binding")
            if (
                head.bucket != BUCKET
                or head.object_key != f"{head.owner}/ai/{head.document_id}/{head.revision}.corpus"
            ):
                raise ValueError("invalid object key")
            if not re.fullmatch(r"sha256:[0-9a-f]{64}", head.digest):
                raise ValueError("invalid digest")
            return head
        except (ValidationError, ValueError, TypeError) as error:
            raise HTTPException(
                status_code=503, detail="Working draft storage returned invalid data"
            ) from error

    def _require_capability(self, owner: str) -> None:
        value = self._rpc("capabilities", owner, {})
        if not isinstance(value, dict) or value.get("draft_api") != 1:
            raise HTTPException(status_code=503, detail="Working draft registry is unavailable")

    def _info(self, head: DraftHead) -> dict[str, Any]:
        return {
            "id": str(head.document_id),
            "corpus": head.corpus,
            "revision": str(head.revision),
            "digest": head.digest,
            "version": head.version,
            "state": head.state,
            "archive_url": f"/v1/ai/drafts/{head.document_id}/archive",
        }

    def _download(self, head: DraftHead) -> bytes:
        url, headers = self._config()
        path = quote(head.object_key, safe="/")
        try:
            with self.client.stream(
                "GET", f"{url}/storage/v1/object/{BUCKET}/{path}", headers=headers
            ) as response:
                if response.status_code != 200:
                    raise HTTPException(
                        status_code=503, detail="Working draft archive is unavailable"
                    )
                data = bytearray()
                for chunk in response.iter_bytes():
                    data.extend(chunk)
                    if len(data) > MAX_VALIDATION_ARCHIVE_BYTES:
                        raise HTTPException(
                            status_code=413, detail="Working draft archive exceeds size limits"
                        )
        except httpx.HTTPError as error:
            raise HTTPException(
                status_code=503, detail="Working draft archive is unavailable"
            ) from error
        result = bytes(data)
        if "sha256:" + hashlib.sha256(result).hexdigest() != head.digest:
            raise HTTPException(status_code=409, detail="Working draft archive digest mismatch")
        return result

    def resolve(self, owner: str, draft_id: str) -> DraftHead:
        try:
            draft_id = str(UUID(draft_id))
        except ValueError as error:
            raise HTTPException(status_code=404, detail="Working draft not found") from error
        row = self._rpc("resolve", owner, {"corpus": "draft:" + draft_id})
        if row is None:
            raise HTTPException(status_code=404, detail="Working draft not found")
        return self._head(row, owner, draft_id)

    def list(self, owner: str, limit: int, offset: int) -> dict[str, Any]:
        self._require_capability(owner)
        rows = self._rpc("drafts", owner, {"limit": limit + 1, "offset": offset})
        if not isinstance(rows, list):
            raise HTTPException(
                status_code=503, detail="Working draft storage returned invalid data"
            )
        heads = [self._head(row, owner) for row in rows]
        return {
            "drafts": [self._info(head) for head in heads[:limit]],
            "next_offset": offset + limit if len(heads) > limit else None,
        }

    def create(self, owner: str, job_id: str, jobs: Any, validator: Any) -> dict[str, Any]:
        try:
            owner, job_id = str(UUID(owner)), str(UUID(job_id))
        except ValueError as error:
            raise HTTPException(status_code=404, detail="Conversion job not found") from error
        self._require_capability(owner)
        name, data = jobs.fetch(owner, job_id)
        if not name.endswith(".corpus"):
            raise HTTPException(status_code=422, detail="Working drafts require a corpus archive")
        document_id = str(uuid5(NAMESPACE_URL, json.dumps(["ai-draft", owner, job_id])))
        try:
            return self._info(self.resolve(owner, document_id))
        except HTTPException as error:
            if error.status_code != 404:
                raise
        try:
            verdict = validator.validate(name, data)
            manifest = read_archive(data, "manifest")
        except (ValueError, zipfile.BadZipFile) as error:
            raise HTTPException(
                status_code=422, detail="Conversion archive is unreadable"
            ) from error
        if not verdict.get("valid"):
            raise HTTPException(status_code=422, detail="Conversion archive is invalid")
        version = str(manifest.get("version") or "")
        if not re.fullmatch(r"v?[0-9]+\.[0-9]+", version):
            raise HTTPException(
                status_code=422, detail="Working drafts require a major.minor version"
            )
        digest = "sha256:" + hashlib.sha256(data).hexdigest()
        # A retry of the same owned job and bytes must stage the same immutable
        # object, even if an earlier provision response was lost after commit.
        revision = str(uuid5(UUID(document_id), digest))
        value = {
            "document_id": document_id,
            "owner": owner,
            "corpus": "draft:" + document_id,
            "bucket": BUCKET,
            "object_key": f"{owner}/ai/{document_id}/{revision}.corpus",
            "revision": revision,
            "digest": digest,
            "version": version,
        }
        head = self._head(value, owner, document_id)
        url, headers = self._config()
        try:
            response = self.client.post(
                f"{url}/storage/v1/object/{BUCKET}/{quote(head.object_key, safe='/')}",
                headers={**headers, "Content-Type": "application/zip", "x-upsert": "false"},
                content=data,
            )
        except httpx.HTTPError as error:
            raise HTTPException(
                status_code=503, detail="Working draft archive could not be staged"
            ) from error
        if response.status_code not in (200, 201, 400, 409):
            raise HTTPException(status_code=503, detail="Working draft archive could not be staged")
        self._download(head)
        row = self._rpc(
            "provision",
            owner,
            {
                **head.model_dump(mode="json", exclude={"owner", "state"}),
                "job_id": job_id,
                "name": str(manifest.get("name") or name)[:512],
            },
        )
        return self._info(self._head(row, owner, document_id))
