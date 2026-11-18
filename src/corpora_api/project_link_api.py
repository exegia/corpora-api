"""Durable project references with live project and corpus authorization."""

import json
from typing import Any, Literal
from uuid import UUID

from corpora_linking.models import EpubLocator, PdfLocator, StructuralLocator
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette.concurrency import run_in_threadpool

from corpora_api.auth import require_user
from corpora_api.corpus_creation import CreationRPC, _session
from corpora_api.corpus_metadata import CorpusMetadataError
from corpora_api.project_document import retrieve_document, verify_epub, verify_pdf
from corpora_api.project_link_models import (
    LinkDecision,
    LinkEndpoint,
    LinkRevision,
    LinkSave,
    draft_reference,
)
from corpora_api.references import reference as resolve_corpus_reference

router = APIRouter(prefix="/v1/projects", tags=["Project links"])


class LinkReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: UUID
    reference_id: UUID
    version: int = Field(strict=True, ge=1)


class LinkHistory(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: UUID
    reference_id: UUID
    revisions: list[LinkRevision]
    next_before: int | None


class SelectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    side: Literal["source", "target"]


class RetrievedSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: UUID
    reference_id: UUID
    version: int = Field(strict=True, ge=1)
    side: Literal["source", "target"]
    endpoint: LinkEndpoint
    fragments: list[str]


class LinkExportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reference_ids: list[UUID] = Field(min_length=1, max_length=10)

    @field_validator("reference_ids")
    @classmethod
    def distinct_references(cls, value: list[UUID]) -> list[UUID]:
        if len(set(value)) != len(value):
            raise ValueError("Reference IDs must be distinct")
        return value


class LinkExport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["1"] = "1"
    project_id: UUID
    revisions: list[LinkRevision]


def _call(name: str, params: dict[str, Any]) -> Any:
    try:
        return CreationRPC(max_response_bytes=524288).call(name, params)
    except CorpusMetadataError:
        raise HTTPException(503, "Project linking is unavailable") from None


def _status(value: Any) -> None:
    if value == {"status": "not_found"}:
        raise HTTPException(404, "Project, reference or corpus not found")
    if value == {"status": "conflict"}:
        raise HTTPException(409, "Reference version changed")
    if value == {"status": "invalid_transition"}:
        raise HTTPException(409, "Reference decision is not allowed in its current state")
    if value == {"status": "stale_corpus"}:
        raise HTTPException(409, "A linked corpus has changed")


async def _current_reference(
    request: Request, project: UUID, ref: UUID, *, edit: bool
) -> LinkRevision:
    value = await run_in_threadpool(
        _call,
        "corpora_current_project_link" if edit else "corpora_read_current_project_link",
        {
            **_session(request.state.user_claims),
            "p_project": str(project),
            "p_reference": str(ref),
        },
    )
    _status(value)
    try:
        if (
            not isinstance(value, dict)
            or set(value) != {"status", "revision"}
            or value.get("status") != "ok"
        ):
            raise ValueError("Invalid history")
        latest = LinkRevision.model_validate(value["revision"])
        if latest.reference.id != ref:
            raise ValueError("Mismatched reference")
    except (ValueError, TypeError, KeyError, IndexError):
        raise HTTPException(503, "Project linking is unavailable") from None
    return latest


async def _resolve_reference(request: Request, project: UUID, ref: UUID, version: int) -> None:
    latest = await _current_reference(request, project, ref, edit=True)
    if latest.version != version:
        raise HTTPException(409, "Reference version changed")
    for endpoint in (latest.reference.source, latest.reference.target):
        try:
            selected = LinkEndpoint.model_validate(endpoint.model_dump(mode="json"))
        except ValueError:
            raise HTTPException(422, "Reference endpoint is invalid") from None
        if selected.document_id == ".corpora/source.corpus" and all(
            isinstance(locator, StructuralLocator) for locator in selected.locators
        ):
            for locator in selected.locators:
                assert isinstance(locator, StructuralLocator)
                result = await resolve_corpus_reference(
                    request.state.user_claims, ref=locator.anchor_id
                )
                if type(result.get("node")) is not int or result["node"] < 1:
                    raise HTTPException(503, "Corpus reference verification failed")
        elif len(selected.locators) == 1 and isinstance(selected.locators[0], EpubLocator):
            await verify_epub(request.state.user_claims, selected)
        elif len(selected.locators) == 1 and isinstance(selected.locators[0], PdfLocator):
            await verify_pdf(request.state.user_claims, selected)
        else:
            raise HTTPException(422, "This selection cannot yet be verified")


def _receipt(value: Any, project: UUID, reference: UUID, expected: int) -> LinkReceipt:
    _status(value)
    if (
        not isinstance(value, dict)
        or set(value) != {"status", "version"}
        or value["status"] != "ok"
    ):
        raise HTTPException(503, "Project linking is unavailable")
    try:
        receipt = LinkReceipt(project_id=project, reference_id=reference, version=value["version"])
        if receipt.version != expected + 1:
            raise ValueError("Invalid version receipt")
        return receipt
    except (ValueError, TypeError):
        raise HTTPException(503, "Project linking is unavailable") from None


@router.post("/{project_id}/references", response_model=LinkReceipt)
async def save_reference(
    project_id: UUID,
    body: LinkSave,
    request: Request,
    response: Response,
    _user: str = Depends(require_user),
) -> LinkReceipt:
    """Save a draft linking reference; project owner or superadmin only."""
    actor = UUID(request.state.user_claims["sub"])
    reference = draft_reference(body, actor)
    value = await run_in_threadpool(
        _call,
        "corpora_save_project_link",
        {
            **_session(request.state.user_claims),
            "p_project": str(project_id),
            "p_reference": str(body.reference_id),
            "p_expected": body.expected_version,
            "p_reason": body.reason,
            "p_value": reference.model_dump(mode="json"),
        },
    )
    response.headers["Cache-Control"] = "no-store"
    return _receipt(value, project_id, body.reference_id, body.expected_version or 0)


@router.post("/{project_id}/references/export", response_model=LinkExport)
async def export_references(
    project_id: UUID,
    body: LinkExportRequest,
    request: Request,
    response: Response,
    _user: str = Depends(require_user),
) -> LinkExport:
    """Export selected current references and their provenance, regardless of review state."""
    value = await run_in_threadpool(
        _call,
        "corpora_export_project_links",
        {
            **_session(request.state.user_claims),
            "p_project": str(project_id),
            "p_references": [str(item) for item in body.reference_ids],
        },
    )
    _status(value)
    try:
        if (
            not isinstance(value, dict)
            or set(value) != {"status", "revisions"}
            or value["status"] != "ok"
        ):
            raise ValueError("Invalid export")
        revisions = [LinkRevision.model_validate(row) for row in value["revisions"]]
        actual = [row.reference.id for row in revisions]
        if actual != sorted(set(body.reference_ids)):
            raise ValueError("Mismatched export references")
        result = LinkExport(project_id=project_id, revisions=revisions)
        encoded = json.dumps(
            result.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":")
        )
        if len(encoded.encode("utf-8")) > 3_900_000:
            raise ValueError("Export exceeds response limit")
    except (ValueError, TypeError, KeyError):
        raise HTTPException(503, "Project linking is unavailable") from None
    response.headers["Cache-Control"] = "no-store"
    response.headers["Content-Disposition"] = 'attachment; filename="corpora-links.json"'
    return result


@router.post("/{project_id}/references/{reference_id}/retrieve", response_model=RetrievedSelection)
async def retrieve_selection(
    project_id: UUID,
    reference_id: UUID,
    body: SelectionRequest,
    request: Request,
    response: Response,
    _user: str = Depends(require_user),
) -> RetrievedSelection:
    """Retrieve a selection; Text-Fabric text uses the core reference renderer."""
    latest = await _current_reference(request, project_id, reference_id, edit=False)
    raw = latest.reference.source if body.side == "source" else latest.reference.target
    try:
        selected = LinkEndpoint.model_validate(raw.model_dump(mode="json"))
    except ValueError:
        raise HTTPException(422, "Reference endpoint is invalid") from None
    fragments = []
    if selected.document_id == ".corpora/source.corpus" and all(
        isinstance(locator, StructuralLocator) for locator in selected.locators
    ):
        for locator in selected.locators:
            assert isinstance(locator, StructuralLocator)
            value = await resolve_corpus_reference(request.state.user_claims, ref=locator.anchor_id)
            if (
                type(value.get("node")) is not int
                or value["node"] < 1
                or not isinstance(value.get("text"), str)
            ):
                raise HTTPException(503, "Corpus reference retrieval failed")
            fragments.append(value["text"])
            if sum(len(fragment.encode("utf-8")) for fragment in fragments) > 3_000_000:
                raise HTTPException(413, "Selection exceeds the response size limit")
    elif len(selected.locators) == 1 and isinstance(selected.locators[0], EpubLocator):
        fragments.append(await retrieve_document(request.state.user_claims, selected, "epub"))
    elif len(selected.locators) == 1 and isinstance(selected.locators[0], PdfLocator):
        fragments.append(await retrieve_document(request.state.user_claims, selected, "pdf"))
    else:
        raise HTTPException(422, "This selection cannot yet be retrieved")
    response.headers["Cache-Control"] = "no-store"
    result = RetrievedSelection(
        project_id=project_id,
        reference_id=reference_id,
        version=latest.version,
        side=body.side,
        endpoint=selected,
        fragments=fragments,
    )
    encoded = json.dumps(result.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > 3_900_000:
        raise HTTPException(413, "Selection exceeds the response size limit")
    return result


@router.post("/{project_id}/references/{reference_id}/{action}", response_model=LinkReceipt)
async def decide_reference(
    project_id: UUID,
    reference_id: UUID,
    action: Literal["reject", "reopen", "resolve", "approve"],
    body: LinkDecision,
    request: Request,
    response: Response,
    _user: str = Depends(require_user),
) -> LinkReceipt:
    """Verify, approve, reject, or reopen a reference."""
    if action == "resolve":
        await _resolve_reference(request, project_id, reference_id, body.expected_version)
    value = await run_in_threadpool(
        _call,
        "corpora_decide_project_link",
        {
            **_session(request.state.user_claims),
            "p_project": str(project_id),
            "p_reference": str(reference_id),
            "p_expected": body.expected_version,
            "p_reason": body.reason,
            "p_action": action,
        },
    )
    response.headers["Cache-Control"] = "no-store"
    return _receipt(value, project_id, reference_id, body.expected_version)


@router.get("/{project_id}/references/{reference_id}/history", response_model=LinkHistory)
async def reference_history(
    project_id: UUID,
    reference_id: UUID,
    request: Request,
    response: Response,
    before: int | None = Query(default=None, ge=1),
    limit: int = Query(default=10, ge=1, le=10),
    _user: str = Depends(require_user),
) -> LinkHistory:
    """Read revisions if the caller may access the project and each linked corpus."""
    value = await run_in_threadpool(
        _call,
        "corpora_project_link_history",
        {
            **_session(request.state.user_claims),
            "p_project": str(project_id),
            "p_reference": str(reference_id),
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
            raise ValueError("Invalid history")
        entries = [LinkRevision.model_validate(row) for row in value["revisions"]]
        versions = [entry.version for entry in entries]
        if (
            len(entries) > limit + 1
            or versions != sorted(set(versions), reverse=True)
            or (before is not None and versions and versions[0] >= before)
            or any(entry.reference.id != reference_id for entry in entries)
        ):
            raise ValueError("Invalid history ordering")
        items = entries[:limit]
        result = LinkHistory(
            project_id=project_id,
            reference_id=reference_id,
            revisions=items,
            next_before=items[-1].version if len(entries) > limit else None,
        )
    except (ValueError, TypeError, KeyError):
        raise HTTPException(503, "Project linking is unavailable") from None
    response.headers["Cache-Control"] = "no-store"
    return result
