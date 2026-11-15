"""Owner-aware storage boundary for shared core reference rules."""

from __future__ import annotations

import os
import zipfile
from pathlib import PurePosixPath
from typing import Any

import yaml
from corpus_validate import ArchiveReadNotFound, ArchiveTooLarge, refcompact, refdisplay, tfref
from fastapi import HTTPException

from corpora_api.corpus_process import CorpusProcessError, run_corpus
from corpora_api.storage import archive_name
from corpora_api.storage_detail import download_stored_archive


def _corpus_id(filename: str) -> str:
    stem = PurePosixPath(filename).stem
    return filename if filename.endswith(".cusx") or stem.endswith((".corpus", ".cusx")) else stem


def reference(
    storage: Any,
    user_id: str,
    *,
    corpus: str | None = None,
    ref: str | None = None,
    node: int | None = None,
    end_node: int | None = None,
    lang: str | None = None,
    shortcode: bool = False,
    url_template: str | None = None,
) -> dict[str, Any]:
    """Resolve only an archive accessible through the configured storage backend."""
    try:
        compact = ref is not None and refcompact.is_compact(ref)
        parsed = tfref.parse(ref) if ref is not None and not compact else None
        if corpus is None and compact:
            slug = refcompact.slug_of(ref)
            found = [
                item.filename
                for item in storage.list(user_id)
                if refcompact.corpus_slug(_corpus_id(item.filename)) == slug
            ]
            if len(found) > 1:
                raise HTTPException(status_code=409, detail="Compact corpus name is ambiguous")
            corpus = found[0] if found else slug
        if corpus is None and parsed is not None:
            corpus = parsed.corpus
        if corpus is None:
            if shortcode and parsed is not None:
                template = url_template or os.getenv("REFERENCE_URL_TEMPLATE")
                return refdisplay.shortcode_payload(parsed, ref, url_template=template)
            raise tfref.ParseError("Reference names no corpus; provide corpus explicitly")
        filename = archive_name(corpus)
        corpus_id = _corpus_id(filename)
        data = download_stored_archive(storage, user_id, filename)
        return run_corpus(
            data,
            "reference",
            corpus_id=corpus_id,
            node=node,
            end_node=end_node,
            ref=ref,
            lang=lang,
            shortcode=shortcode,
            url_template=url_template or os.getenv("REFERENCE_URL_TEMPLATE"),
        )
    except CorpusProcessError as error:
        if error.kind == "version":
            raise HTTPException(
                status_code=409, detail={"message": str(error), **error.attributes}
            ) from error
        status = {"parse": 400, "not_found": 404, "too_large": 413, "unavailable": 503}.get(
            error.kind, 422
        )
        raise HTTPException(status_code=status, detail=str(error)) from error
    except tfref.VersionMismatch as error:
        raise HTTPException(
            status_code=409,
            detail={"message": str(error), "wanted": error.wanted, "loaded": error.loaded},
        ) from error
    except tfref.ParseError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except (
        tfref.SectionNotFound,
        tfref.TypeNotInSection,
        tfref.IndexOutOfRange,
        ArchiveReadNotFound,
    ) as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ArchiveTooLarge as error:
        raise HTTPException(
            status_code=413, detail="Stored corpus archive exceeds size limits"
        ) from error
    except (zipfile.BadZipFile, UnicodeError, yaml.YAMLError, ValueError) as error:
        raise HTTPException(
            status_code=422, detail="Stored corpus archive is unreadable"
        ) from error
