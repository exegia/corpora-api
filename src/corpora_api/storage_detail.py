"""Owner-scoped reads of metadata and graph views from stored corpus archives."""

from __future__ import annotations

from typing import Any, Protocol

import httpx
from fastapi import HTTPException

from corpora_api.corpus_process import CorpusProcessError, run_corpus
from corpora_api.storage import StorageError, archive_name

MAX_STORED_ARCHIVE_BYTES = 3_000_000


class StoredArchiveStorage(Protocol):
    def open_download(self, user_id: str, filename: str) -> tuple[httpx.Response, str]: ...


def download_stored_archive(storage: StoredArchiveStorage, user_id: str, filename: str) -> bytes:
    """Fetch an authorized archive while bounding the response size."""
    response, _stored_name = storage.open_download(user_id, filename)
    chunks: list[bytes] = []
    size = 0
    try:
        for chunk in response.iter_bytes():
            size += len(chunk)
            if size > MAX_STORED_ARCHIVE_BYTES:
                raise HTTPException(
                    status_code=413,
                    detail=f"Stored corpus archive exceeds {MAX_STORED_ARCHIVE_BYTES} bytes",
                )
            chunks.append(chunk)
    except httpx.HTTPError as error:
        raise StorageError("Corpus storage request failed") from error
    finally:
        response.close()

    return b"".join(chunks)


def read_stored_archive_view(
    storage: StoredArchiveStorage,
    user_id: str,
    filename: str,
    view: str,
    **params: Any,
) -> dict[str, Any]:
    """Fetch by verified owner, bound the bytes, and delegate read-only parsing."""
    data = download_stored_archive(storage, user_id, filename)

    try:
        return run_corpus(data, "view", view=view, **params)
    except CorpusProcessError as error:
        status = {"not_found": 404, "too_large": 413, "unavailable": 503}.get(error.kind, 422)
        detail = "Corpus detail is unavailable"
        if error.kind == "not_found":
            node = params.get("node")
            ref = params.get("parent") if view == "sections" else params.get("ref")
            if view == "node" and isinstance(node, int) and str(error) == f"Node {node} not found":
                detail = f"Node {node} not found in {archive_name(filename)}"
            elif (
                view in {"sections", "content"}
                and isinstance(ref, str)
                and len(ref) <= 512
                and str(error) == f"Section reference {ref!r} not found"
            ):
                detail = f"Section reference {ref!r} not found in {archive_name(filename)}"
        raise HTTPException(status_code=status, detail=detail) from error
