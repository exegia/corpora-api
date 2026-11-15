"""Unconfigured route proposal; never mounted without coordinated cutover.

The owning application must supply verified user identity and coordinate all
archive reads, imports, deletes, session revocation, and retention first.
"""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Response
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, model_validator

from corpora_api.auth import require_user
from corpora_api.stored_history import stored_history
from corpora_api.stored_revisions import StoredRevisions
from corpora_api.stored_versions import StoredVersions


class ManifestPatch(BaseModel):
    name: str | None = None
    description: str | None = None
    version: str | None = None
    language: str | None = None
    languageCode: str | None = None  # noqa: N815 - legacy API field
    type: str | None = None
    category: str | None = None
    written_date: str | None = None

    @model_validator(mode="after")
    def require_field(self) -> ManifestPatch:
        if not self.model_fields_set:
            raise ValueError("Provide at least one manifest field")
        return self


class NodePatch(BaseModel):
    otype: str | None = None
    note: str | None = None

    @model_validator(mode="after")
    def require_field(self) -> NodePatch:
        if not self.model_fields_set:
            raise ValueError("Provide otype or note")
        return self


class RestoreRequest(BaseModel):
    revision: UUID


def _expected(value: str | None) -> str:
    if value is None:
        raise HTTPException(428, "If-Match is required")
    if len(value) != 38 or not value.startswith('"') or not value.endswith('"'):
        raise HTTPException(400, "If-Match must contain one strong revision ETag")
    try:
        revision = UUID(value[1:-1])
    except ValueError as error:
        raise HTTPException(400, "If-Match must contain one strong revision ETag") from error
    if str(revision) != value[1:-1]:
        raise HTTPException(400, "If-Match must contain one strong revision ETag")
    return str(revision)


def _set_revision(response: Response, revision: str) -> None:
    response.headers["ETag"] = f'"{UUID(revision)}"'
    response.headers["Cache-Control"] = "no-store"


def stored_version_router(revisions: StoredRevisions) -> APIRouter:
    """Construct proposed routes; the production app does not include them."""
    router = APIRouter(prefix="/v1/storage", tags=["storage"])
    versions = StoredVersions(revisions)
    precondition = {
        400: {"description": "Invalid If-Match"},
        412: {"description": "Stale revision"},
        428: {"description": "If-Match is required"},
    }

    @router.get("/{filename}/versions")
    async def history(
        filename: str, response: Response, user_id: Annotated[str, Depends(require_user)]
    ) -> dict[str, Any]:
        result = await run_in_threadpool(stored_history, revisions, user_id, filename)
        _set_revision(response, result["head_revision"])
        return result

    @router.patch("/{filename}/manifest", responses=precondition)
    async def manifest(
        filename: str,
        payload: ManifestPatch,
        response: Response,
        user_id: Annotated[str, Depends(require_user)],
        if_match: Annotated[str | None, Header(alias="If-Match")] = None,
    ) -> dict[str, Any]:
        result = await run_in_threadpool(
            versions.edit,
            user_id,
            filename,
            _expected(if_match),
            "manifest",
            payload.model_dump(exclude_unset=True),
        )
        _set_revision(response, result["revision"])
        return result["result"]

    @router.patch("/{filename}/nodes/{node}", responses=precondition)
    async def node_patch(
        filename: str,
        payload: NodePatch,
        response: Response,
        user_id: Annotated[str, Depends(require_user)],
        node: Annotated[int, Path(ge=1)],
        if_match: Annotated[str | None, Header(alias="If-Match")] = None,
    ) -> dict[str, Any]:
        result = await run_in_threadpool(
            versions.edit,
            user_id,
            filename,
            _expected(if_match),
            "node",
            payload.model_dump(exclude_unset=True),
            node,
        )
        _set_revision(response, result["revision"])
        return result["result"]

    @router.post("/{filename}/restore", responses=precondition)
    async def restore(
        filename: str,
        payload: RestoreRequest,
        response: Response,
        user_id: Annotated[str, Depends(require_user)],
        if_match: Annotated[str | None, Header(alias="If-Match")] = None,
    ) -> dict[str, Any]:
        result = await run_in_threadpool(
            versions.restore, user_id, filename, _expected(if_match), str(payload.revision)
        )
        _set_revision(response, result["revision"])
        return {
            "revision": result["revision"],
            "version": result["version"],
            "manifest": result["result"],
        }

    return router
