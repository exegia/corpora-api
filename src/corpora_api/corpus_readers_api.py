"""Owner-managed, read-only access to private corpora."""

from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, StrictBool
from starlette.concurrency import run_in_threadpool

from corpora_api.auth import require_user
from corpora_api.corpus_creation import CreationRPC, _session
from corpora_api.corpus_metadata import CorpusMetadataError
from corpora_api.corpus_metadata_api import CorpusMetadataPage, CorpusSummary
from corpora_api.project_members_api import ProjectMemberEmail

router = APIRouter(prefix="/v1/corpora", tags=["Corpora"])


class CorpusReader(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_id: UUID
    created_at: datetime


class CorpusReadersPage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    corpus_id: UUID
    items: list[CorpusReader]
    next_after: UUID | None


class CorpusReaderGrant(BaseModel):
    model_config = ConfigDict(extra="forbid")
    corpus_id: UUID
    user_id: UUID
    present: StrictBool


def _call(name: str, params: dict[str, Any], max_response_bytes: int = 131072) -> Any:
    try:
        return CreationRPC(max_response_bytes=max_response_bytes).call(name, params)
    except CorpusMetadataError:
        raise HTTPException(503, "Corpus sharing is unavailable") from None


def _status(value: Any) -> None:
    if value in ({"status": "not_found"}, {"status": "reader_not_found"}):
        raise HTTPException(404, "Corpus or account not found")


def _grant(value: Any, corpus: UUID, present: bool, user: UUID | None = None) -> CorpusReaderGrant:
    _status(value)
    try:
        if not isinstance(value, dict) or value.get("status") != "ok":
            raise ValueError("Invalid reader receipt")
        result = CorpusReaderGrant.model_validate({k: v for k, v in value.items() if k != "status"})
        if (
            result.corpus_id != corpus
            or result.present != present
            or (user and result.user_id != user)
        ):
            raise ValueError("Mismatched reader receipt")
        return result
    except (ValueError, TypeError):
        raise HTTPException(503, "Corpus sharing is unavailable") from None


async def _set_reader(
    request: Request, corpus: UUID, user: UUID, present: bool
) -> CorpusReaderGrant:
    value = await run_in_threadpool(
        _call,
        "corpora_set_corpus_reader",
        {
            **_session(request.state.user_claims),
            "p_corpus": str(corpus),
            "p_reader": str(user),
            "p_present": present,
        },
    )
    return _grant(value, corpus, present, user)


@router.get("/shared", response_model=CorpusMetadataPage)
async def shared_corpora(
    request: Request,
    response: Response,
    after: UUID | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    _user: str = Depends(require_user),
) -> CorpusMetadataPage:
    """List ready corpora shared directly with the signed-in user."""
    value = await run_in_threadpool(
        _call,
        "corpora_list_shared_corpora",
        {
            **_session(request.state.user_claims),
            "p_after": str(after) if after else None,
            "p_limit": limit,
        },
        524288,
    )
    try:
        if (
            not isinstance(value, dict)
            or set(value) != {"status", "rows"}
            or value["status"] != "list"
        ):
            raise ValueError("Invalid shared corpus page")
        entries = [CorpusSummary.model_validate(row) for row in value["rows"]]
        ids = [row.id.int for row in entries]
        if (
            len(entries) > limit + 1
            or ids != sorted(set(ids))
            or (after and ids and ids[0] <= after.int)
            or any(row.state != "ready" for row in entries)
        ):
            raise ValueError("Invalid shared corpus ordering")
        items = entries[:limit]
        result = CorpusMetadataPage(
            items=items, next_cursor=items[-1].id if len(entries) > limit else None
        )
    except (ValueError, TypeError, KeyError):
        raise HTTPException(503, "Corpus sharing is unavailable") from None
    response.headers["Cache-Control"] = "no-store"
    return result


@router.put("/{corpus_id}/readers/{user_id}", response_model=CorpusReaderGrant)
async def add_corpus_reader(
    corpus_id: UUID,
    user_id: UUID,
    request: Request,
    response: Response,
    _user: str = Depends(require_user),
) -> CorpusReaderGrant:
    """Share read-only access with a confirmed Auth user; owner or superadmin only."""
    response.headers["Cache-Control"] = "no-store"
    return await _set_reader(request, corpus_id, user_id, True)


@router.post("/{corpus_id}/readers", response_model=CorpusReaderGrant)
async def add_corpus_reader_by_email(
    corpus_id: UUID,
    body: ProjectMemberEmail,
    request: Request,
    response: Response,
    _user: str = Depends(require_user),
) -> CorpusReaderGrant:
    """Share with a confirmed account by email; no invitation is sent."""
    value = await run_in_threadpool(
        _call,
        "corpora_add_corpus_reader_by_email",
        {**_session(request.state.user_claims), "p_corpus": str(corpus_id), "p_email": body.email},
    )
    response.headers["Cache-Control"] = "no-store"
    return _grant(value, corpus_id, True)


@router.delete("/{corpus_id}/readers/{user_id}", response_model=CorpusReaderGrant)
async def remove_corpus_reader(
    corpus_id: UUID,
    user_id: UUID,
    request: Request,
    response: Response,
    _user: str = Depends(require_user),
) -> CorpusReaderGrant:
    """Revoke read-only access; owner or superadmin only."""
    response.headers["Cache-Control"] = "no-store"
    return await _set_reader(request, corpus_id, user_id, False)


@router.get("/{corpus_id}/readers", response_model=CorpusReadersPage)
async def list_corpus_readers(
    corpus_id: UUID,
    request: Request,
    response: Response,
    after: UUID | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    _user: str = Depends(require_user),
) -> CorpusReadersPage:
    """List direct readers; owner or superadmin only."""
    value = await run_in_threadpool(
        _call,
        "corpora_list_corpus_readers",
        {
            **_session(request.state.user_claims),
            "p_corpus": str(corpus_id),
            "p_after": str(after) if after else None,
            "p_limit": limit,
        },
    )
    _status(value)
    try:
        if not isinstance(value, dict) or set(value) != {"status", "corpus_id", "readers"}:
            raise ValueError("Invalid reader page")
        if value["status"] != "ok" or UUID(value["corpus_id"]) != corpus_id:
            raise ValueError("Mismatched corpus")
        entries = [CorpusReader.model_validate(item) for item in value["readers"]]
        ids = [row.user_id.int for row in entries]
        if (
            len(entries) > limit + 1
            or ids != sorted(set(ids))
            or (after and ids and ids[0] <= after.int)
        ):
            raise ValueError("Invalid reader page ordering")
        items = entries[:limit]
        result = CorpusReadersPage(
            corpus_id=corpus_id,
            items=items,
            next_after=items[-1].user_id if len(entries) > limit else None,
        )
    except (ValueError, TypeError, KeyError):
        raise HTTPException(503, "Corpus sharing is unavailable") from None
    response.headers["Cache-Control"] = "no-store"
    return result
