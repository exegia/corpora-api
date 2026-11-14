"""Bounded plain-text ingestion into the shared Context Fabric v1 graph."""

from __future__ import annotations

import hashlib
import re
import uuid
from pathlib import Path

from corpora_schema_types import CanonicalGraph, validate_graph

MAX_INGEST_BYTES = 3_000_000
MAX_GRAPH_BYTES = 4_000_000
_LANGUAGE = re.compile(r"^[A-Za-z]{2,3}(-[A-Za-z0-9]{1,8})*$")
_SLUG_PART = re.compile(r"[^a-z0-9]+")


class IngestRejected(ValueError):
    """An upload cannot be represented as a supported canonical graph."""


def _slug(text: str) -> str:
    return _SLUG_PART.sub("-", text.lower()).strip("-")[:128].rstrip("-") or "document"


def validate_ingest_options(filename: str, name: str, language: str, corpus_id: str) -> str | None:
    """Reject unsafe filenames and invalid shared form fields before any staging."""
    if (
        not filename
        or Path(filename).name != filename
        or "\\" in filename
        or len(filename.encode("utf-8")) > 255
        or any(ord(char) < 32 for char in filename)
    ):
        raise IngestRejected("Upload a document with a plain filename")
    if not _LANGUAGE.fullmatch(language):
        raise IngestRejected("Not a BCP 47 language tag")
    if len(name) > 256:
        raise IngestRejected("Name exceeds 256 characters")
    try:
        return str(uuid.UUID(corpus_id)) if corpus_id else None
    except ValueError as error:
        raise IngestRejected("corpus_id must be a UUID") from error


def build_plain_graph(
    filename: str,
    source: bytes,
    *,
    name: str = "",
    language: str = "und",
    corpus_id: str = "",
) -> CanonicalGraph:
    """Keep every decoded character in one fragment; add only structural wrappers."""
    if Path(filename).suffix.lower() != ".txt":
        raise IngestRejected("Upload a plain .txt filename")
    existing_corpus_id = validate_ingest_options(filename, name, language, corpus_id)
    if len(source) > MAX_INGEST_BYTES:
        raise IngestRejected(f"Upload exceeds {MAX_INGEST_BYTES} bytes")
    try:
        text = source.decode("utf-8")
    except UnicodeDecodeError as error:
        raise IngestRejected("Plain text must be UTF-8") from error
    if not text:
        raise IngestRejected("Plain text is empty")

    title = name.strip() or Path(filename).stem or "Document"
    slug = _slug(title)
    corpus_id_value = existing_corpus_id or str(uuid.uuid4())
    work_id = str(uuid.uuid4())
    edition_id = str(uuid.uuid4())
    asset_id = str(uuid.uuid4())
    root_id = str(uuid.uuid4())
    block_id = str(uuid.uuid4())
    version = "1.0.0"
    graph = CanonicalGraph(
        schema_version=version,
        corpus=(
            {"schemaVersion": version, "id": corpus_id_value, "slug": slug, "title": title}
            if existing_corpus_id is None
            else None
        ),
        work={
            "schemaVersion": version,
            "id": work_id,
            "corpusId": corpus_id_value,
            "slug": slug,
            "title": title,
        },
        edition={
            "schemaVersion": version,
            "id": edition_id,
            "workId": work_id,
            "slug": slug,
            "title": title,
            "language": language,
            "sourceAssetIds": [asset_id],
        },
        source_asset={
            "schemaVersion": version,
            "id": asset_id,
            "editionId": edition_id,
            "sourceFormat": "plain",
            "mediaType": "text/plain",
            "filename": filename,
            "sha256": hashlib.sha256(source).hexdigest(),
            "byteSize": len(source),
        },
        nodes=[
            {
                "schemaVersion": version,
                "id": root_id,
                "editionId": edition_id,
                "parentId": None,
                "ordinal": 0,
                "depth": 0,
                "category": "root",
                "type": "generic:document",
                "childCount": 1,
            },
            {
                "schemaVersion": version,
                "id": block_id,
                "editionId": edition_id,
                "parentId": root_id,
                "ordinal": 0,
                "depth": 1,
                "category": "block",
                "type": "generic:paragraph",
                "childCount": 0,
            },
        ],
        fragments=[
            {
                "schemaVersion": version,
                "id": str(uuid.uuid4()),
                "nodeId": block_id,
                "ordinal": 0,
                "text": text,
                "after": "",
                "charStart": 0,
                "charEnd": len(text),
            }
        ],
    )
    return validate_graph(graph)
