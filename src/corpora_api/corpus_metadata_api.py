"""Live-session metadata access through restricted database functions."""

from collections.abc import Mapping
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from corpora_api.auth import require_user
from corpora_api.corpus_creation import CreationRPC, _session, require_creation_config
from corpora_api.corpus_metadata import (
    CorpusMetadata,
    CorpusMetadataConflict,
    CorpusMetadataError,
    CorpusMetadataNotFound,
    CorpusMetadataStore,
    _normalize_changes,
)

router = APIRouter(tags=["Corpora"])


class CorpusSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: UUID
    owner_id: UUID
    title: str = Field(min_length=1, max_length=512)
    language: str | None = Field(max_length=128)
    state: Literal["provisioning", "ready", "failed"]
    revision: int = Field(gt=0)
    created_at: datetime
    updated_at: datetime


class CorpusMetadataInfo(CorpusSummary):
    description: str | None = Field(max_length=10000)
    properties: dict[str, Any]


class CorpusMetadataPage(BaseModel):
    items: list[CorpusSummary]
    next_cursor: UUID | None


class CorpusMetadataPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(strict=True, gt=0, lt=2**63 - 1)
    title: str | None = Field(default=None, min_length=1, max_length=512)
    description: str | None = Field(default=None, max_length=10000)
    language: str | None = Field(default=None, max_length=128)
    properties: dict[str, Any] | None = None


def _metadata(value: Any, corpus_id: UUID) -> CorpusMetadataInfo:
    if value == {"status": "not_found"}:
        raise CorpusMetadataNotFound("Corpus not found")
    if value == {"status": "conflict"}:
        raise CorpusMetadataConflict("Corpus metadata revision changed")
    try:
        if not isinstance(value, dict) or set(value) != {"status", "row"}:
            raise ValueError("Unexpected metadata response")
        if value["status"] != "ok":
            raise ValueError("Unexpected metadata state")
        row = CorpusMetadata.model_validate(value["row"])
        if row.id != corpus_id:
            raise ValueError("Unexpected corpus")
        CorpusMetadataStore._check_link(row)
        return CorpusMetadataInfo.model_validate(
            row.model_dump(exclude={"hf_repo_id", "hf_repo_type"})
        )
    except (ValueError, TypeError, KeyError):
        raise CorpusMetadataError("Corpus metadata response is invalid") from None


def read_metadata(claims: Mapping[str, Any], corpus_id: UUID) -> CorpusMetadataInfo:
    require_creation_config()
    result = CreationRPC(max_response_bytes=131072).call(
        "corpora_get_metadata", {**_session(claims), "p_corpus": str(corpus_id)}
    )
    return _metadata(result, corpus_id)


def edit_metadata(
    claims: Mapping[str, Any], corpus_id: UUID, patch: CorpusMetadataPatch
) -> CorpusMetadataInfo:
    require_creation_config()
    try:
        fields = _normalize_changes(
            patch.expected_revision,
            patch.model_dump(exclude_unset=True, exclude={"expected_revision"}),
        )
    except CorpusMetadataError:
        raise HTTPException(422, "Invalid corpus metadata update") from None
    result = CreationRPC(max_response_bytes=131072).call(
        "corpora_patch_metadata",
        {
            **_session(claims),
            "p_corpus": str(corpus_id),
            "p_revision": patch.expected_revision,
            "p_fields": fields,
        },
    )
    return _metadata(result, corpus_id)


def list_metadata(
    claims: Mapping[str, Any], owner: UUID | None, after: UUID | None, limit: int
) -> CorpusMetadataPage:
    require_creation_config()
    session = _session(claims)
    owner = owner or UUID(session["p_actor"])
    value = CreationRPC(max_response_bytes=524288).call(
        "corpora_list_metadata",
        {
            **session,
            "p_owner": str(owner),
            "p_after": str(after) if after else None,
            "p_limit": limit,
        },
    )
    if value == {"status": "not_found"}:
        raise CorpusMetadataNotFound("Corpus owner not found")
    try:
        if not isinstance(value, dict) or set(value) != {"status", "rows"}:
            raise ValueError("Unexpected metadata page")
        if value["status"] != "list" or not isinstance(value["rows"], list):
            raise ValueError("Unexpected metadata page")
        if len(value["rows"]) > limit + 1:
            raise ValueError("Metadata page exceeds bound")
        rows = [CorpusSummary.model_validate(row) for row in value["rows"]]
        previous = after
        for row in rows:
            if row.owner_id != owner or (previous is not None and row.id <= previous):
                raise ValueError("Metadata page identity mismatch")
            previous = row.id
        page = rows[:limit]
        return CorpusMetadataPage(
            items=page, next_cursor=page[-1].id if len(rows) > limit else None
        )
    except (ValueError, TypeError, KeyError):
        raise CorpusMetadataError("Corpus metadata page is invalid") from None


@router.get("/v1/corpora", response_model=CorpusMetadataPage)
async def list_corpora(
    request: Request,
    response: Response,
    owner_id: UUID | None = None,
    after: UUID | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    _user: str = Depends(require_user),
) -> CorpusMetadataPage:
    """List your corpus summaries; a server-configured superadmin may select another owner."""
    response.headers["Cache-Control"] = "no-store"
    return await run_in_threadpool(list_metadata, request.state.user_claims, owner_id, after, limit)


@router.get("/v1/corpora/{corpus_id}/metadata", response_model=CorpusMetadataInfo)
async def corpus_metadata(
    corpus_id: UUID,
    request: Request,
    response: Response,
    _user: str = Depends(require_user),
) -> CorpusMetadataInfo:
    """Read corpus metadata as its owner or server-configured superadmin."""
    response.headers["Cache-Control"] = "no-store"
    return await run_in_threadpool(read_metadata, request.state.user_claims, corpus_id)


@router.patch("/v1/corpora/{corpus_id}/metadata", response_model=CorpusMetadataInfo)
async def patch_corpus_metadata(
    corpus_id: UUID,
    patch: CorpusMetadataPatch,
    request: Request,
    response: Response,
    _user: str = Depends(require_user),
) -> CorpusMetadataInfo:
    """Edit metadata at an expected revision; properties replace the complete object."""
    response.headers["Cache-Control"] = "no-store"
    return await run_in_threadpool(edit_metadata, request.state.user_claims, corpus_id, patch)
