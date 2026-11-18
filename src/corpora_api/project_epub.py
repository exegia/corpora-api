"""Resolve an EPUB selection from an authorized immutable corpus archive."""

import hashlib
from typing import Any
from uuid import UUID

from fastapi import HTTPException
from starlette.concurrency import run_in_threadpool

from corpora_api import corpus_detail_api
from corpora_api.corpus_access_api import read_access
from corpora_api.corpus_hub_read import CorpusHubReadError
from corpora_api.corpus_process import CorpusProcessError, run_corpus
from corpora_api.project_link_models import LinkEndpoint


async def verify_epub(claims: Any, endpoint: LinkEndpoint) -> None:
    access = await run_in_threadpool(read_access, claims, UUID(endpoint.package_id or ""))
    if access.row.state != "ready" or access.head is None:
        raise HTTPException(404, "Corpus content is not available")
    head = access.head
    if endpoint.revision != head.commit_sha:
        raise HTTPException(409, "Reference points to a different corpus revision")
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
    try:
        result = await run_in_threadpool(
            run_corpus, bytes(data), "verify_epub", raw_endpoint=endpoint.model_dump(mode="json")
        )
    except CorpusProcessError as error:
        status = 503 if error.kind == "unavailable" else 422
        raise HTTPException(status, "EPUB selection could not be verified") from None
    if result != {"verified": True}:
        raise HTTPException(503, "EPUB verification returned an invalid result")
