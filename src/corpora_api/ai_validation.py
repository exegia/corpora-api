"""Authenticated storage handoff for scoped corpus validation."""

from __future__ import annotations

import hashlib
import re
from typing import Any
from uuid import UUID

from fastapi import HTTPException
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from corpora_api.conversion_artifacts import ConversionArtifactNotFound
from corpora_api.corpus_access_api import read_access
from corpora_api.corpus_hub_read import CorpusHubReadError
from corpora_api.corpus_process import (
    SAFE_SCOPE_ERRORS,
    CorpusProcessError,
    run_corpus,
    safe_scope_node_type,
)
from corpora_api.models import AINodeScope
from corpora_api.storage import StorageError, StoredCorpusNotFound
from corpora_api.storage_detail import download_stored_archive

_ARCHIVE_ID = re.compile(r"^[\w][\w .-]*$", re.UNICODE)


def validate_scope(
    storage: Any, jobs: Any, owner: str, request: AINodeScope, validator: Any
) -> dict[str, Any] | JSONResponse:
    if request.corpus.startswith("job:"):
        try:
            UUID(request.corpus[4:])
        except ValueError as error:
            raise HTTPException(422, "Expected job:<UUID>") from error
        try:
            name, data = jobs.fetch(owner, request.corpus[4:])
        except ConversionArtifactNotFound as error:
            raise HTTPException(404, "Corpus not found") from error
        # The stored validation flag describes an earlier version of the
        # object. Validate the exact downloaded bytes before using them.
        verdict = validator.validate(name, data)
        if not isinstance(verdict, dict) or verdict.get("valid") is not True:
            raise HTTPException(422, "Conversion archive is invalid")
    else:
        if not request.corpus.startswith("draft:") and (
            not _ARCHIVE_ID.fullmatch(request.corpus) or request.corpus != request.corpus.strip()
        ):
            raise HTTPException(422, "Expected a flat corpus archive ID, not a path or URL")
        try:
            data = download_stored_archive(storage, owner, request.corpus)
        except StoredCorpusNotFound as error:
            raise HTTPException(404, "Corpus not found") from error
        except StorageError as error:
            raise HTTPException(503, "Corpus storage is unavailable") from error
    return validate_archive(data, request)


async def validate_hub_scope(claims: Any, request: AINodeScope) -> dict[str, Any] | JSONResponse:
    """Validate an authorized current corpus from its verified private archive."""
    from corpora_api import corpus_detail_api

    try:
        raw_id = request.corpus.removeprefix("corpus:")
        corpus_id = UUID(raw_id)
        if raw_id != str(corpus_id):
            raise ValueError("Noncanonical corpus ID")
    except ValueError as error:
        raise HTTPException(422, "Expected corpus:<UUID>") from error
    if request.corpus_revision is None:
        raise HTTPException(422, "A full corpus_revision is required")
    access = await run_in_threadpool(read_access, claims, corpus_id)
    if access.row.state != "ready" or access.head is None:
        raise HTTPException(404, "Corpus not found")
    head = access.head
    if request.corpus_revision != head.commit_sha:
        return JSONResponse(
            status_code=409,
            content={
                "code": "stale",
                "reason": "Corpus revision has changed",
                "retryable": False,
                "current_version": head.commit_sha,
            },
        )
    data = bytearray()

    async def collect(chunk: bytes) -> None:
        data.extend(chunk)

    try:
        await corpus_detail_api.hub_reader.transfer_authorized(
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
    return await run_in_threadpool(validate_archive, bytes(data), request)


def validate_archive(data: bytes, request: AINodeScope) -> dict[str, Any] | JSONResponse:
    try:
        return run_corpus(data, "scope", scope=request.model_dump(exclude={"corpus_revision"}))
    except CorpusProcessError as error:
        if error.kind == "unknown_scope_node":
            nodes = [request.node_id]
            if request.unit_range is not None:
                nodes.extend([request.unit_range.start, request.unit_range.end])
            node = error.attributes.get("node")
            if type(node) is int and node in nodes:
                raise HTTPException(422, f"Unknown node {node}") from error
        if error.kind == "invalid_scope":
            detail = error.attributes.get("detail")
            if isinstance(detail, str) and detail in SAFE_SCOPE_ERRORS:
                raise HTTPException(422, detail) from error
        if error.kind == "scope_type_mismatch":
            actual = error.attributes.get("actual")
            if (
                request.node_id is not None
                and request.node_type is not None
                and safe_scope_node_type(actual)
            ):
                raise HTTPException(
                    422, f"Node {request.node_id} has type {actual!r}, not {request.node_type!r}"
                ) from error
        if error.kind == "stale":
            reason = (
                "Scoped text has changed"
                if str(error) == "Scoped text has changed"
                else "Corpus version has changed"
            )
            return JSONResponse(
                status_code=409,
                content={
                    "code": "stale",
                    "reason": reason,
                    "retryable": False,
                    "current_version": error.attributes.get("current_version"),
                },
            )
        status = {"too_large": 413, "unavailable": 503}.get(error.kind, 422)
        raise HTTPException(
            status_code=status, detail="Invalid scope or unreadable corpus"
        ) from error
