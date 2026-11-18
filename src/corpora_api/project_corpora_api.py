"""Project-to-managed-corpus associations with independent access checks."""

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from corpora_api.auth import require_user
from corpora_api.corpus_creation import CreationRPC, _session, require_creation_config
from corpora_api.corpus_metadata import CorpusMetadataError
from corpora_api.corpus_metadata_api import CorpusSummary

router = APIRouter(prefix="/v1/projects", tags=["Project corpora"])


class ProjectCorpus(CorpusSummary):
    linked: bool = Field(strict=True)


class ProjectCorpusPage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[ProjectCorpus]
    next_cursor: UUID | None


class ProjectCorpusReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: UUID
    corpus_id: UUID
    linked: bool = Field(strict=True)


def _call(name: str, params: dict[str, Any]) -> Any:
    require_creation_config()
    value = CreationRPC(max_response_bytes=524288).call(name, params)
    if value == {"status": "not_found"}:
        raise HTTPException(404, "Project or corpus not found")
    return value


def _list(
    claims: dict[str, Any], project: UUID, linked_only: bool, after: UUID | None, limit: int
) -> ProjectCorpusPage:
    value = _call(
        "corpora_list_project_corpora",
        {
            **_session(claims),
            "p_project": str(project),
            "p_linked_only": linked_only,
            "p_after": str(after) if after else None,
            "p_limit": limit,
        },
    )
    try:
        if (
            not isinstance(value, dict)
            or set(value) != {"status", "rows"}
            or value["status"] != "list"
        ):
            raise ValueError("Invalid project corpus list")
        raw = value["rows"]
        if not isinstance(raw, list) or len(raw) > limit + 1:
            raise ValueError("Invalid project corpus page")
        rows = [ProjectCorpus.model_validate(item) for item in raw]
        previous = after
        for row in rows:
            if (previous is not None and row.id <= previous) or (linked_only and not row.linked):
                raise ValueError("Invalid project corpus ordering or association")
            previous = row.id
        page = rows[:limit]
        return ProjectCorpusPage(items=page, next_cursor=page[-1].id if len(rows) > limit else None)
    except (ValueError, TypeError, KeyError):
        raise CorpusMetadataError("Project corpus list is invalid") from None


def _set(
    claims: dict[str, Any], project: UUID, corpus: UUID, present: bool
) -> ProjectCorpusReceipt:
    value = _call(
        "corpora_set_project_corpus",
        {
            **_session(claims),
            "p_project": str(project),
            "p_corpus": str(corpus),
            "p_present": present,
        },
    )
    try:
        if (
            not isinstance(value, dict)
            or set(value) != {"status", "project_id", "corpus_id", "linked"}
            or value["status"] != "ok"
        ):
            raise ValueError("Invalid association response")
        receipt = ProjectCorpusReceipt.model_validate(
            {k: v for k, v in value.items() if k != "status"}
        )
        if (
            receipt.project_id != project
            or receipt.corpus_id != corpus
            or receipt.linked is not present
        ):
            raise ValueError("Association identity mismatch")
        return receipt
    except (ValueError, TypeError, KeyError):
        raise CorpusMetadataError("Project corpus result is invalid") from None


@router.get("/{project_id}/corpora", response_model=ProjectCorpusPage)
async def list_project_corpora(
    project_id: UUID,
    request: Request,
    response: Response,
    linked_only: bool = True,
    after: UUID | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    _user: str = Depends(require_user),
) -> ProjectCorpusPage:
    """List accessible linked corpora; project managers can list available choices."""
    response.headers["Cache-Control"] = "no-store"
    return await run_in_threadpool(
        _list, request.state.user_claims, project_id, linked_only, after, limit
    )


@router.put("/{project_id}/corpora/{corpus_id}", response_model=ProjectCorpusReceipt)
async def link_project_corpus(
    project_id: UUID,
    corpus_id: UUID,
    request: Request,
    response: Response,
    _user: str = Depends(require_user),
) -> ProjectCorpusReceipt:
    """Link an accessible ready corpus as the project owner or superadmin."""
    response.headers["Cache-Control"] = "no-store"
    return await run_in_threadpool(_set, request.state.user_claims, project_id, corpus_id, True)


@router.delete("/{project_id}/corpora/{corpus_id}", response_model=ProjectCorpusReceipt)
async def unlink_project_corpus(
    project_id: UUID,
    corpus_id: UUID,
    request: Request,
    response: Response,
    _user: str = Depends(require_user),
) -> ProjectCorpusReceipt:
    """Idempotently unlink a corpus as the project owner or superadmin."""
    response.headers["Cache-Control"] = "no-store"
    return await run_in_threadpool(_set, request.state.user_claims, project_id, corpus_id, False)
