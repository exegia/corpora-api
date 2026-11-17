"""Read corpus views from an admitted, digest-verified immutable archive."""

from __future__ import annotations

import hashlib
from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from corpora_api.corpus_access_api import read_access
from corpora_api.corpus_hub_deadline import DeadlineCorpusHubReader
from corpora_api.corpus_hub_read import CorpusHubReadError
from corpora_api.corpus_process import CorpusProcessError, run_corpus
from corpora_api.models import (
    CorpusContentResponse,
    CorpusIndexResponse,
    CorpusNodeDetailResponse,
    CorpusSectionPageResponse,
    ErrorResponse,
)


def public_corpus_read_path(path: str) -> bool:
    """Routes that may admit anonymous reads after database visibility checks."""
    parts = path.split("/")
    return parts[1:3] == ["v1", "corpora"] and (
        len(parts) == 5
        and parts[4] in {"metadata", "visibility", "manifest", "index", "sections", "content"}
        or len(parts) == 6
        and parts[4] == "nodes"
    )


router = APIRouter(
    prefix="/v1/corpora",
    tags=["Corpora"],
    responses={
        401: {"model": ErrorResponse, "description": "Supplied token is invalid or revoked"},
        404: {
            "model": ErrorResponse,
            "description": "Corpus, section or node is unavailable to this reader",
        },
        413: {"model": ErrorResponse, "description": "Corpus view exceeds the response size limit"},
        422: {"model": ErrorResponse, "description": "Invalid request or unreadable archive"},
        503: {
            "model": ErrorResponse,
            "description": "Storage, integrity verification or reader unavailable",
        },
    },
)
hub_reader = DeadlineCorpusHubReader()


async def read_view(
    corpus_id: UUID, request: Request, response: Response, view: str, **params: Any
) -> dict[str, Any]:
    # Access is admitted before any Hub operation. A supplied token remains
    # subject to the live session check even when the corpus is public.
    access = await run_in_threadpool(
        read_access, getattr(request.state, "user_claims", None), corpus_id
    )
    if access.row.state != "ready" or access.head is None:
        raise HTTPException(404, "Corpus content is not available")
    head = access.head
    data = bytearray()

    async def collect(chunk: bytes) -> None:
        data.extend(chunk)

    try:
        await hub_reader.transfer_authorized(
            access.row,
            head.commit_sha,
            ".corpora/source.corpus",
            max_bytes=head.archive_size,
            timeout=20,
            sink=collect,
        )
    except CorpusHubReadError:
        raise HTTPException(503, "Corpus storage is unavailable") from None
    if len(data) != head.archive_size or hashlib.sha256(data).hexdigest() != head.archive_sha256:
        raise HTTPException(503, "Corpus integrity verification failed")
    try:
        result = await run_in_threadpool(run_corpus, bytes(data), "view", view=view, **params)
    except CorpusProcessError as error:
        status = {"not_found": 404, "too_large": 413, "unavailable": 503}.get(error.kind, 422)
        raise HTTPException(status, "Corpus detail is unavailable") from None
    model = {
        "index": CorpusIndexResponse,
        "sections": CorpusSectionPageResponse,
        "content": CorpusContentResponse,
        "node": CorpusNodeDetailResponse,
    }.get(view)
    if model is not None:
        try:
            model.model_validate(result)
        except ValidationError:
            raise HTTPException(422, "Corpus detail is unavailable") from None
    # Visibility may change after admission; shared caches must not retain a read.
    response.headers["Cache-Control"] = "no-store"
    return result


_READ_DESCRIPTION = (
    "Read the corpus's current immutable version. Public corpora allow anonymous reads; "
    "private corpora require owner or superadmin access. A supplied token must have a live "
    "session. Backing Hugging Face storage remains private. Reads admitted before an access "
    "change may finish; responses are not cacheable."
)


@router.get("/{corpus_id}/manifest", description=_READ_DESCRIPTION)
async def get_manifest(corpus_id: UUID, request: Request, response: Response) -> dict[str, Any]:
    return await read_view(corpus_id, request, response, "manifest")


@router.get("/{corpus_id}/index", description=_READ_DESCRIPTION, response_model=CorpusIndexResponse)
async def get_index(corpus_id: UUID, request: Request, response: Response) -> dict[str, Any]:
    return await read_view(corpus_id, request, response, "index")


@router.get(
    "/{corpus_id}/sections", description=_READ_DESCRIPTION, response_model=CorpusSectionPageResponse
)
async def get_sections(
    corpus_id: UUID,
    request: Request,
    response: Response,
    parent: str | None = Query(default=None, max_length=512),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    return await read_view(
        corpus_id, request, response, "sections", parent=parent, offset=offset, limit=limit
    )


@router.get(
    "/{corpus_id}/content", description=_READ_DESCRIPTION, response_model=CorpusContentResponse
)
async def get_content(
    corpus_id: UUID,
    request: Request,
    response: Response,
    ref: str | None = Query(default=None, max_length=512),
    fmt: str | None = Query(default=None, max_length=128),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    return await read_view(
        corpus_id, request, response, "content", ref=ref, fmt=fmt, offset=offset, limit=limit
    )


@router.get(
    "/{corpus_id}/nodes/{node}",
    description=_READ_DESCRIPTION,
    response_model=CorpusNodeDetailResponse,
)
async def get_node(
    corpus_id: UUID, node: int, request: Request, response: Response
) -> dict[str, Any]:
    return await read_view(corpus_id, request, response, "node", node=node)
