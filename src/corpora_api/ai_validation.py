"""Authenticated storage handoff for scoped corpus validation."""

from __future__ import annotations

import zipfile
from dataclasses import asdict
from typing import Any

import yaml
from corpus_validate import ArchiveReadNotFound, ArchiveTooLarge
from corpus_validate.ai_archive import StaleScope, validate_archive_scope
from corpus_validate.ai_types import NodeScope, ScopeLevel, UnitRange
from fastapi import HTTPException
from fastapi.responses import JSONResponse

from corpora_api.models import AINodeScope
from corpora_api.storage_detail import download_stored_archive


def validate_scope(
    storage: Any, jobs: Any, owner: str, request: AINodeScope
) -> dict[str, Any] | JSONResponse:
    if request.corpus.startswith("job:"):
        _, data = jobs.fetch(owner, request.corpus[4:])
    else:
        data = download_stored_archive(storage, owner, request.corpus)
    selected = request.unit_range
    scope = NodeScope(
        corpus=request.corpus,
        level=ScopeLevel(request.level),
        node_id=request.node_id,
        node_type=request.node_type,
        label=request.label,
        version=request.version,
        unit_range=UnitRange(selected.start, selected.end) if selected else None,
        content_hash=request.content_hash,
    )
    try:
        return asdict(validate_archive_scope(data, scope))
    except StaleScope as error:
        return JSONResponse(
            status_code=409,
            content={
                "code": "stale",
                "reason": str(error),
                "retryable": False,
                "current_version": error.current_version,
            },
        )
    except ArchiveTooLarge as error:
        raise HTTPException(
            status_code=413, detail="Stored corpus archive exceeds size limits"
        ) from error
    except (
        ArchiveReadNotFound,
        ValueError,
        zipfile.BadZipFile,
        UnicodeError,
        yaml.YAMLError,
    ) as error:
        raise HTTPException(status_code=422, detail="Invalid scope or unreadable corpus") from error
