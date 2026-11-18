"""Owner and superadmin access to immutable corpus-version history."""

from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from starlette.concurrency import run_in_threadpool

from corpora_api.auth import require_user
from corpora_api.corpus_creation import CreationRPC, _session, require_creation_config
from corpora_api.corpus_metadata import CorpusMetadataError, CorpusMetadataNotFound
from corpora_api.models import ErrorResponse

router = APIRouter(tags=["Corpora"])


class CorpusVersionInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sequence: int = Field(strict=True, gt=0)
    commit_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    parent_sha: str | None = Field(pattern=r"^[0-9a-f]{40}$")
    operation_id: UUID
    actor_id: UUID
    created_at: datetime

    @model_validator(mode="after")
    def valid_parent(self) -> "CorpusVersionInfo":
        if (self.sequence == 1) != (self.parent_sha is None):
            raise ValueError("Invalid version parent")
        return self


class CorpusVersionPage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    corpus_id: UUID
    current_sequence: int | None = Field(strict=True, gt=0)
    versions: list[CorpusVersionInfo]
    next_before: int | None = Field(strict=True, gt=0)


def list_versions(
    claims: dict[str, Any], corpus: UUID, before: int | None, limit: int
) -> CorpusVersionPage:
    require_creation_config()
    value = CreationRPC(max_response_bytes=65536).call(
        "corpora_list_corpus_versions",
        {**_session(claims), "p_corpus": str(corpus), "p_before": before, "p_limit": limit},
    )
    if value == {"status": "not_found"}:
        raise CorpusMetadataNotFound("Corpus not found")
    try:
        if not isinstance(value, dict) or value.get("status") != "ok":
            raise ValueError("Invalid version page")
        page = CorpusVersionPage.model_validate({k: v for k, v in value.items() if k != "status"})
        sequences = [entry.sequence for entry in page.versions]
        if (
            page.corpus_id != corpus
            or len(sequences) > limit
            or sequences != sorted(set(sequences), reverse=True)
            or (before is not None and any(sequence >= before for sequence in sequences))
            or (page.current_sequence is None and bool(sequences))
            or (
                page.current_sequence is not None
                and any(s > page.current_sequence for s in sequences)
            )
            or (
                page.next_before is not None
                and (not sequences or page.next_before != sequences[-1])
            )
        ):
            raise ValueError("Invalid version page identity or ordering")
        return page
    except (ValueError, TypeError, ValidationError):
        raise CorpusMetadataError("Corpus version history is unavailable") from None


@router.get(
    "/v1/corpora/{corpus_id}/versions",
    response_model=CorpusVersionPage,
    responses={
        401: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
        503: {"model": ErrorResponse},
    },
)
async def get_corpus_versions(
    corpus_id: UUID,
    request: Request,
    response: Response,
    before: int | None = Query(default=None, ge=1, le=2**63 - 1),
    limit: int = Query(default=50, ge=1, le=100),
    _user: str = Depends(require_user),
) -> CorpusVersionPage:
    """List immutable versions for a live owner or superadmin session."""
    response.headers["Cache-Control"] = "no-store"
    return await run_in_threadpool(
        list_versions, request.state.user_claims, corpus_id, before, limit
    )
