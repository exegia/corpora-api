"""Citation discoveries with project and source-corpus access checks."""

import json
from datetime import datetime
from typing import Literal, Self
from uuid import UUID

from corpora_linking.models import EpubLocator, PdfLocator, StructuralLocator
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, model_validator
from starlette.concurrency import run_in_threadpool

from corpora_api.auth import require_user
from corpora_api.corpus_creation import _session
from corpora_api.project_document import retrieve_document
from corpora_api.project_link_api import _call, _status
from corpora_api.project_link_models import LinkDecision, LinkEndpoint
from corpora_api.references import reference as resolve_corpus_reference

router = APIRouter(prefix="/v1/projects", tags=["Project discoveries"])


class DiscoveryEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: UUID
    source: LinkEndpoint
    quote: str = Field(min_length=1, max_length=16000)
    cited_name: str = Field(min_length=1, max_length=1000)
    status: Literal["pending", "accepted", "rejected"] = "pending"

    @model_validator(mode="after")
    def bounded(self) -> Self:
        if not self.quote.strip() or not self.cited_name.strip() or len(self.source.locators) != 1:
            raise ValueError("Discovery needs one exact source selection and citation")
        # Review changes the status, so keep the evidence limit independent of it.
        serialized = self.model_dump(mode="json", exclude={"status"})
        if len(json.dumps(serialized, ensure_ascii=False).encode()) > 32768:
            raise ValueError("Discovery evidence exceeds size limit")
        return self


class DiscoveryRegistration(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: UUID
    source: LinkEndpoint
    quote: str = Field(min_length=1, max_length=16000)
    cited_name: str = Field(min_length=1, max_length=1000)
    reason: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def validate_evidence(self) -> Self:
        DiscoveryEvidence(
            id=self.id, source=self.source, quote=self.quote, cited_name=self.cited_name
        )
        if not self.reason.strip():
            raise ValueError("Reason cannot be blank")
        return self


class DiscoveryReceipt(BaseModel):
    project_id: UUID
    discovery_id: UUID
    version: int = Field(strict=True, ge=1)


class DiscoveryRevision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = Field(strict=True, ge=1)
    actor_id: UUID
    recorded_at: datetime
    reason: str
    action: Literal["register", "accept", "reject"]
    evidence: DiscoveryEvidence


class DiscoveryHistory(BaseModel):
    project_id: UUID
    discovery_id: UUID
    revisions: list[DiscoveryRevision]
    next_before: int | None


async def _source_text(claims: dict, endpoint: LinkEndpoint) -> str:
    locator = endpoint.locators[0]
    if endpoint.document_id == ".corpora/source.corpus" and isinstance(locator, StructuralLocator):
        result = await resolve_corpus_reference(claims, ref=locator.anchor_id)
        if (
            type(result.get("node")) is not int
            or result["node"] < 1
            or not isinstance(result.get("text"), str)
        ):
            raise HTTPException(503, "Corpus selection could not be read")
        return result["text"]
    if isinstance(locator, EpubLocator):
        return await retrieve_document(claims, endpoint, "epub")
    if isinstance(locator, PdfLocator):
        return await retrieve_document(claims, endpoint, "pdf")
    raise HTTPException(422, "This source selection cannot yet be verified")


def _receipt(value: object, project: UUID, discovery: UUID, version: int) -> DiscoveryReceipt:
    _status(value)
    if not isinstance(value, dict) or value != {"status": "ok", "version": version}:
        raise HTTPException(503, "Project discovery is unavailable")
    return DiscoveryReceipt(project_id=project, discovery_id=discovery, version=version)


@router.post("/{project_id}/discoveries", response_model=DiscoveryReceipt)
async def register_discovery(
    project_id: UUID,
    body: DiscoveryRegistration,
    request: Request,
    response: Response,
    _user: str = Depends(require_user),
) -> DiscoveryReceipt:
    """Record verified citation evidence without inventing a target corpus."""
    evidence = DiscoveryEvidence(
        id=body.id, source=body.source, quote=body.quote, cited_name=body.cited_name
    )
    params = {
        **_session(request.state.user_claims),
        "p_project": str(project_id),
        "p_discovery": str(body.id),
        "p_reason": body.reason,
        "p_evidence": evidence.model_dump(mode="json"),
    }
    admitted = await run_in_threadpool(_call, "corpora_admit_project_discovery", params)
    _status(admitted)
    if (
        not isinstance(admitted, dict)
        or set(admitted) != {"status", "exists"}
        or (admitted["status"] != "ok" or type(admitted["exists"]) is not bool)
    ):
        raise HTTPException(503, "Project discovery is unavailable")
    response.headers["Cache-Control"] = "no-store"
    if admitted["exists"]:
        return DiscoveryReceipt(project_id=project_id, discovery_id=body.id, version=1)
    if await _source_text(request.state.user_claims, evidence.source) != evidence.quote:
        raise HTTPException(422, "Citation quote does not match its source selection")
    value = await run_in_threadpool(
        _call,
        "corpora_register_project_discovery",
        params,
    )
    return _receipt(value, project_id, body.id, 1)


@router.get("/{project_id}/discoveries/{discovery_id}", response_model=DiscoveryHistory)
async def discovery_history(
    project_id: UUID,
    discovery_id: UUID,
    request: Request,
    response: Response,
    before: int | None = Query(default=None, ge=1),
    limit: int = Query(default=10, ge=1, le=10),
    _user: str = Depends(require_user),
) -> DiscoveryHistory:
    """Read the history with project and source-corpus access."""
    value = await run_in_threadpool(
        _call,
        "corpora_project_discovery_history",
        {
            **_session(request.state.user_claims),
            "p_project": str(project_id),
            "p_discovery": str(discovery_id),
            "p_before": before,
            "p_limit": limit,
        },
    )
    _status(value)
    try:
        if (
            not isinstance(value, dict)
            or set(value) != {"status", "revisions"}
            or value["status"] != "ok"
        ):
            raise ValueError("Invalid discovery history")
        entries = [DiscoveryRevision.model_validate(row) for row in value["revisions"]]
        versions = [entry.version for entry in entries]
        if (
            len(entries) > limit + 1
            or versions != sorted(set(versions), reverse=True)
            or (before is not None and versions and versions[0] >= before)
            or any(entry.evidence.id != discovery_id for entry in entries)
        ):
            raise ValueError("Invalid discovery history ordering")
        items = entries[:limit]
        result = DiscoveryHistory(
            project_id=project_id,
            discovery_id=discovery_id,
            revisions=items,
            next_before=items[-1].version if len(entries) > limit else None,
        )
    except (ValueError, TypeError, KeyError):
        raise HTTPException(503, "Project discovery is unavailable") from None
    response.headers["Cache-Control"] = "no-store"
    return result


@router.post("/{project_id}/discoveries/{discovery_id}/{action}", response_model=DiscoveryReceipt)
async def decide_discovery(
    project_id: UUID,
    discovery_id: UUID,
    action: Literal["accept", "reject"],
    body: LinkDecision,
    request: Request,
    response: Response,
    _user: str = Depends(require_user),
) -> DiscoveryReceipt:
    """Record an owner or superadmin review decision."""
    value = await run_in_threadpool(
        _call,
        "corpora_decide_project_discovery",
        {
            **_session(request.state.user_claims),
            "p_project": str(project_id),
            "p_discovery": str(discovery_id),
            "p_expected": body.expected_version,
            "p_reason": body.reason,
            "p_action": action,
        },
    )
    response.headers["Cache-Control"] = "no-store"
    return _receipt(value, project_id, discovery_id, body.expected_version + 1)
