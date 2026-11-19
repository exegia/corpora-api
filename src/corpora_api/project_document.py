"""Resolve a document selection from an authorized immutable corpus archive."""

import hashlib
import shutil
from typing import Any
from uuid import UUID

from corpora_linking.models import PdfLocator, TextLocator
from fastapi import HTTPException
from starlette.concurrency import run_in_threadpool

from corpora_api import corpus_detail_api
from corpora_api.corpus_access_api import read_access
from corpora_api.corpus_hub_read import CorpusHubReadError
from corpora_api.corpus_process import CorpusProcessError, run_corpus
from corpora_api.project_link_models import LinkEndpoint, PinnedAsset


async def _document(
    claims: Any, endpoint: PinnedAsset, operation: str, *, raw_capture: dict | None = None
) -> dict[str, Any]:
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
        args = {"raw_endpoint": endpoint.model_dump(mode="json")}
        if raw_capture is not None:
            args = {"raw_capture": raw_capture}
        result = await run_in_threadpool(run_corpus, bytes(data), operation, **args)
    except CorpusProcessError as error:
        status = 503 if error.kind == "unavailable" else 422
        detail = "Document selection could not be verified"
        if operation == "map_browser" and status == 422:
            detail = "Browser selection does not match the pinned HTML source; recapture it"
        raise HTTPException(status, detail) from None
    return result


async def verify_document(claims: Any, endpoint: LinkEndpoint, operation: str) -> None:
    if await _document(claims, endpoint, operation) != {"verified": True}:
        raise HTTPException(503, "Document verification returned an invalid result")


async def verify_epub(claims: Any, endpoint: LinkEndpoint) -> None:
    await verify_document(claims, endpoint, "verify_epub")


async def verify_html(claims: Any, endpoint: LinkEndpoint) -> None:
    await verify_document(claims, endpoint, "verify_html")


def _check_pdf(endpoint: LinkEndpoint) -> None:
    if (
        not endpoint.document_id
        or not endpoint.document_id.lower().endswith(".pdf")
        or len(endpoint.locators) != 1
        or not isinstance(endpoint.locators[0], PdfLocator)
    ):
        raise HTTPException(422, "This PDF selection cannot yet be verified")
    locator = endpoint.locators[0]
    if (
        locator.text is None
        or locator.text.stream_id != "poppler-page-words-v1"
        or locator.text.normalization != "preserve"
        or locator.quads
        or not locator.rectangles
        or locator.page > 1000
    ):
        raise HTTPException(422, "This PDF selection cannot yet be verified")
    if not shutil.which("pdfinfo") or not shutil.which("pdftotext"):
        raise HTTPException(503, "PDF verification is unavailable")


async def verify_pdf(claims: Any, endpoint: LinkEndpoint) -> None:
    _check_pdf(endpoint)
    await verify_document(claims, endpoint, "verify_pdf")


def _check_cusx(endpoint: LinkEndpoint) -> None:
    if (
        not endpoint.document_id
        or not endpoint.document_id.lower().endswith(".cusx")
        or len(endpoint.locators) != 1
        or not isinstance(endpoint.locators[0], TextLocator)
        or endpoint.locators[0].stream_id != "cusx-itertext/v1"
        or endpoint.locators[0].normalization != "preserve"
    ):
        raise HTTPException(422, "This C-USX selection cannot yet be verified")


async def verify_cusx(claims: Any, endpoint: LinkEndpoint) -> None:
    _check_cusx(endpoint)
    await verify_document(claims, endpoint, "verify_cusx")


async def retrieve_document(claims: Any, endpoint: LinkEndpoint, kind: str) -> str:
    if kind == "pdf":
        _check_pdf(endpoint)
    elif kind == "cusx":
        _check_cusx(endpoint)
    elif kind not in ("epub", "html"):
        raise HTTPException(422, "Unsupported document selection")
    result = await _document(claims, endpoint, "retrieve_" + kind)
    if (
        set(result) != {"text"}
        or not isinstance(result["text"], str)
        or not result["text"]
        or len(result["text"].encode("utf-8")) > 32768
    ):
        raise HTTPException(503, "Document retrieval returned an invalid result")
    return result["text"]
