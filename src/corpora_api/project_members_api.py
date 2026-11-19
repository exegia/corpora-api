"""Private project membership managed by the owner or superadmin."""

import re
from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator
from starlette.concurrency import run_in_threadpool

from corpora_api.auth import require_user
from corpora_api.corpus_creation import CreationRPC, _session
from corpora_api.corpus_metadata import CorpusMetadataError

router = APIRouter(prefix="/v1/projects", tags=["Projects"])


class ProjectMember(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_id: UUID
    created_at: datetime


class ProjectMembersPage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: UUID
    items: list[ProjectMember]
    next_after: UUID | None


class ProjectMembership(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: UUID
    user_id: UUID
    present: StrictBool


class ProjectMemberEmail(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str = Field(min_length=3, max_length=254)

    @field_validator("email")
    @classmethod
    def valid_email(cls, value: str) -> str:
        value = value.strip()
        if not re.fullmatch(r"[^@\s]+@[^@\s]+", value):
            raise ValueError("A valid email is required")
        return value


def _check(value: Any) -> None:
    if value == {"status": "not_found"} or value == {"status": "member_not_found"}:
        raise HTTPException(404, "Project or member not found")


def _call(name: str, params: dict[str, Any]) -> Any:
    try:
        return CreationRPC(max_response_bytes=131072).call(name, params)
    except CorpusMetadataError:
        raise HTTPException(503, "Project membership is unavailable") from None


async def _set_member(request: Request, project_id: UUID, user_id: UUID, present: bool):
    value = await run_in_threadpool(
        _call,
        "corpora_set_project_member",
        {
            **_session(request.state.user_claims),
            "p_project": str(project_id),
            "p_member": str(user_id),
            "p_present": present,
        },
    )
    _check(value)
    try:
        if not isinstance(value, dict) or value.get("status") != "ok":
            raise ValueError("Invalid membership receipt")
        result = ProjectMembership.model_validate({k: v for k, v in value.items() if k != "status"})
        if (
            result.project_id != project_id
            or result.user_id != user_id
            or result.present != present
        ):
            raise ValueError("Mismatched membership receipt")
        return result
    except (ValueError, TypeError):
        raise HTTPException(503, "Project membership is unavailable") from None


@router.put("/{project_id}/members/{user_id}", response_model=ProjectMembership)
async def add_project_member(
    project_id: UUID,
    user_id: UUID,
    request: Request,
    response: Response,
    _user: str = Depends(require_user),
) -> ProjectMembership:
    """Add an existing account as a read-only member; owner or superadmin only.

    `user_id` is the member's Supabase Auth user UUID, not a directory profile ID.
    """
    response.headers["Cache-Control"] = "no-store"
    return await _set_member(request, project_id, user_id, True)


@router.post("/{project_id}/members", response_model=ProjectMembership)
async def add_project_member_by_email(
    project_id: UUID,
    body: ProjectMemberEmail,
    request: Request,
    response: Response,
    _user: str = Depends(require_user),
) -> ProjectMembership:
    """Add a confirmed account by email; no invitation or email is sent."""
    value = await run_in_threadpool(
        _call,
        "corpora_add_project_member_by_email",
        {
            **_session(request.state.user_claims),
            "p_project": str(project_id),
            "p_email": body.email,
        },
    )
    _check(value)
    try:
        if not isinstance(value, dict) or value.get("status") != "ok":
            raise ValueError("Invalid membership receipt")
        result = ProjectMembership.model_validate({k: v for k, v in value.items() if k != "status"})
        if result.project_id != project_id or result.present is not True:
            raise ValueError("Mismatched membership receipt")
    except (ValueError, TypeError):
        raise HTTPException(503, "Project membership is unavailable") from None
    response.headers["Cache-Control"] = "no-store"
    return result


@router.delete("/{project_id}/members/{user_id}", response_model=ProjectMembership)
async def remove_project_member(
    project_id: UUID,
    user_id: UUID,
    request: Request,
    response: Response,
    _user: str = Depends(require_user),
) -> ProjectMembership:
    """Remove a project member; owner or superadmin only."""
    response.headers["Cache-Control"] = "no-store"
    return await _set_member(request, project_id, user_id, False)


@router.get("/{project_id}/members", response_model=ProjectMembersPage)
async def list_project_members(
    project_id: UUID,
    request: Request,
    response: Response,
    after: UUID | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    _user: str = Depends(require_user),
) -> ProjectMembersPage:
    """List project members; owner or superadmin only."""
    value = await run_in_threadpool(
        _call,
        "corpora_list_project_members",
        {
            **_session(request.state.user_claims),
            "p_project": str(project_id),
            "p_after": str(after) if after else None,
            "p_limit": limit,
        },
    )
    _check(value)
    try:
        if not isinstance(value, dict) or set(value) != {"status", "project_id", "members"}:
            raise ValueError("Invalid member page")
        if value["status"] != "ok" or UUID(value["project_id"]) != project_id:
            raise ValueError("Mismatched project")
        entries = [ProjectMember.model_validate(item) for item in value["members"]]
        ids = [entry.user_id.int for entry in entries]
        if (
            len(entries) > limit + 1
            or ids != sorted(set(ids))
            or (after and ids and ids[0] <= after.int)
        ):
            raise ValueError("Invalid member page ordering")
        more = len(entries) > limit
        items = entries[:limit]
        result = ProjectMembersPage(
            project_id=project_id, items=items, next_after=items[-1].user_id if more else None
        )
    except (ValueError, TypeError, KeyError):
        raise HTTPException(503, "Project membership is unavailable") from None
    response.headers["Cache-Control"] = "no-store"
    return result
