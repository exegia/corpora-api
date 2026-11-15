"""Owner-scoped reads of metadata and graph views from stored corpus archives."""

from __future__ import annotations

from typing import Any, Protocol

import httpx
from fastapi import HTTPException

from corpora_api.storage import StorageError

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
        from corpus_validate import ArchiveReadNotFound, ArchiveTooLarge, read_archive
    except ImportError as error:
        raise HTTPException(
            status_code=503, detail="Corpus detail reader is unavailable"
        ) from error

    try:
        return read_archive(data, view, **params)
    except HTTPException:
        raise
    except ArchiveReadNotFound as error:
        raise HTTPException(status_code=404, detail="Corpus detail was not found") from error
    except ArchiveTooLarge as error:
        raise HTTPException(
            status_code=413, detail="Stored corpus archive exceeds size limits"
        ) from error
    except Exception as error:
        raise HTTPException(
            status_code=422, detail="Stored corpus archive is unreadable"
        ) from error
