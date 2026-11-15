"""Authenticated storage handoff for scoped corpus validation."""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException
from fastapi.responses import JSONResponse

from corpora_api.corpus_process import CorpusProcessError, run_corpus
from corpora_api.models import AINodeScope
from corpora_api.storage_detail import download_stored_archive


def validate_scope(
    storage: Any, jobs: Any, owner: str, request: AINodeScope
) -> dict[str, Any] | JSONResponse:
    if request.corpus.startswith("job:"):
        _, data = jobs.fetch(owner, request.corpus[4:])
    else:
        data = download_stored_archive(storage, owner, request.corpus)
    try:
        return run_corpus(data, "scope", scope=request.model_dump())
    except CorpusProcessError as error:
        if error.kind == "stale":
            return JSONResponse(
                status_code=409,
                content={
                    "code": "stale",
                    "reason": str(error),
                    "retryable": False,
                    **error.attributes,
                },
            )
        status = {"too_large": 413, "unavailable": 503}.get(error.kind, 422)
        raise HTTPException(
            status_code=status, detail="Invalid scope or unreadable corpus"
        ) from error
