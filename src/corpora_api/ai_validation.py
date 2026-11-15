"""Authenticated storage handoff for scoped corpus validation."""

from __future__ import annotations

import re
from typing import Any
from uuid import UUID

from fastapi import HTTPException
from fastapi.responses import JSONResponse

from corpora_api.corpus_process import CorpusProcessError, run_corpus
from corpora_api.models import AINodeScope
from corpora_api.storage import StorageError, StoredCorpusNotFound
from corpora_api.storage_detail import download_stored_archive

_ARCHIVE_ID = re.compile(r"^[\w][\w .-]*$", re.UNICODE)


def validate_scope(
    storage: Any, jobs: Any, owner: str, request: AINodeScope
) -> dict[str, Any] | JSONResponse:
    if request.corpus.startswith("job:"):
        try:
            UUID(request.corpus[4:])
        except ValueError as error:
            raise HTTPException(422, "Expected job:<UUID>") from error
        _, data = jobs.fetch(owner, request.corpus[4:])
    else:
        if not request.corpus.startswith("draft:") and (
            not _ARCHIVE_ID.fullmatch(request.corpus) or request.corpus != request.corpus.strip()
        ):
            raise HTTPException(422, "Expected a flat corpus archive ID, not a path or URL")
        try:
            data = download_stored_archive(storage, owner, request.corpus)
        except StoredCorpusNotFound:
            raise
        except StorageError as error:
            raise HTTPException(503, "Corpus storage is unavailable") from error
    try:
        return run_corpus(data, "scope", scope=request.model_dump())
    except CorpusProcessError as error:
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
