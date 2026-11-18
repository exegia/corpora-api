"""References resolved against a signed-in reader's verified corpus snapshot."""

from __future__ import annotations

import hashlib
import re
from typing import Any
from uuid import UUID

from corpus_validate import refcompact, tfref
from fastapi import HTTPException
from starlette.concurrency import run_in_threadpool

from corpora_api import corpus_detail_api
from corpora_api.corpus_access_api import read_access
from corpora_api.corpus_hub_read import CorpusHubReadError
from corpora_api.corpus_process import CorpusProcessError, run_corpus


def _selected_corpus(
    corpus: str | UUID | None, ref: str | None
) -> tuple[UUID, str | None, str | None]:
    try:
        embedded = None
        revision = None
        worker_ref = ref
        if ref is not None:
            token, separator, token_revision = ref.rpartition("@")
            if separator and refcompact.is_compact(token):
                embedded = refcompact.slug_of(token)
                revision = token_revision
                worker_ref = token
            else:
                parsed = tfref.parse(ref)
                embedded, revision = parsed.corpus, parsed.version
            if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{40}", revision):
                raise ValueError("Reference must name a revision")
        selected = UUID(str(corpus)) if corpus is not None else UUID(str(embedded))
        if embedded is not None and UUID(str(embedded)) != selected:
            raise ValueError("Reference corpus does not match")
        return selected, worker_ref, revision
    except (ValueError, TypeError, AttributeError, tfref.ParseError):
        raise HTTPException(400, "Reference must identify its corpus UUID and revision") from None


async def reference(
    claims: Any,
    *,
    corpus: str | UUID | None = None,
    ref: str | None = None,
    node: int | None = None,
    end_node: int | None = None,
    lang: str | None = None,
    shortcode: bool = False,
    url_template: str | None = None,
) -> dict[str, Any]:
    corpus_id, worker_ref, revision = _selected_corpus(corpus, ref)
    access = await run_in_threadpool(read_access, claims, corpus_id)
    if access.row.state != "ready" or access.head is None:
        raise HTTPException(404, "Corpus content is not available")
    head = access.head
    if revision is not None and revision != head.commit_sha:
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
            run_corpus,
            bytes(data),
            "reference",
            corpus_id=str(corpus_id),
            node=node,
            end_node=end_node,
            ref=worker_ref,
            version=head.commit_sha,
            lang=lang,
            shortcode=shortcode,
            url_template=url_template,
        )
        if result.get("token") is not None:
            result["token"] += "@" + head.commit_sha
        if ref is not None:
            result["input"] = ref
        return result
    except CorpusProcessError as error:
        if error.kind == "version":
            raise HTTPException(409, detail={"message": str(error), **error.attributes}) from error
        status = {"parse": 400, "not_found": 404, "too_large": 413, "unavailable": 503}.get(
            error.kind, 422
        )
        raise HTTPException(status, detail=str(error)) from error
