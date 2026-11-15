"""Reconcile owner-bound revision facts with the archive's existing history."""

from __future__ import annotations

import json
from collections import Counter
from typing import Any
from uuid import UUID

from fastapi import HTTPException

from corpora_api.corpus_process import (
    MAX_CORPUS_WORKER_RESPONSE_BYTES,
    CorpusProcessError,
    run_corpus,
)
from corpora_api.stored_revisions import Revision, StoredRevisions

MAX_HISTORY_ROWS = 500
BATCH_SIZE = 100


def _revision(row: Any) -> UUID | None:
    if not isinstance(row, dict) or not isinstance(row.get("snapshot_revision"), str):
        return None
    try:
        candidate = UUID(row["snapshot_revision"])
        # Core compares the selected canonical ID to the literal history value.
        return candidate if str(candidate) == row["snapshot_revision"] else None
    except ValueError:
        return None


def _matches_version(row: dict[str, Any], fact: Revision) -> bool:
    version = fact.version.removeprefix("v")
    return version in {str(row.get(key, "")).removeprefix("v") for key in ("id", "label")}


def stored_history(revisions: StoredRevisions, owner: str, filename: str) -> dict[str, Any]:
    """Preserve legacy rows and annotate IDs backed by the same owned registry.

    A binding is not a promise that the corresponding object remains available.
    This helper is intentionally not wired to a public endpoint.
    """
    head = revisions.get(owner, filename)
    try:
        result = run_corpus(head.data, "view", view="versions")
    except CorpusProcessError as error:
        status = {"too_large": 413, "unavailable": 503}.get(error.kind, 422)
        raise HTTPException(status, "Stored version history is unavailable") from error
    rows = result.get("versions")
    if not isinstance(rows, list):
        raise HTTPException(503, "Stored version history is invalid")
    if len(rows) > MAX_HISTORY_ROWS:
        raise HTTPException(413, "Stored version history exceeds limit")
    candidates = [_revision(row) for row in rows]
    counts = Counter(candidate for candidate in candidates if candidate is not None)
    distinct = [candidate for candidate, count in counts.items() if count == 1]
    facts: dict[UUID, Revision] = {}
    for start in range(0, len(distinct), BATCH_SIZE):
        facts.update(revisions.facts(owner, filename, distinct[start : start + BATCH_SIZE]))
    annotated = []
    for row, candidate in zip(rows, candidates, strict=True):
        if not isinstance(row, dict):
            annotated.append(row)
            continue
        fact = facts.get(candidate) if candidate is not None and counts[candidate] == 1 else None
        bound = str(fact.revision) if fact is not None and _matches_version(row, fact) else None
        annotated.append({**row, "registered_revision": bound})
    output = {**result, "versions": annotated, "head_revision": str(head.fact.revision)}
    try:
        if (
            len(json.dumps(output, ensure_ascii=False, default=str).encode("utf-8"))
            > MAX_CORPUS_WORKER_RESPONSE_BYTES
        ):
            raise HTTPException(413, "Stored version history exceeds limit")
    except (ValueError, TypeError) as error:
        raise HTTPException(503, "Stored version history is invalid") from error
    return output
