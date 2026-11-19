"""Extract one exact asset from an already authorized corpus archive."""

import hashlib
import io
import zipfile

from corpora_api.corpus_archive_inventory import inventory_corpus_archive


def sha256_revision(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def corpus_asset(archive_bytes: bytes, path: str, *, max_bytes: int = 16_777_216) -> bytes:
    inventory = inventory_corpus_archive(archive_bytes)
    matches = [item for item in inventory.files if item.path == path]
    if len(matches) != 1 or matches[0].size > max_bytes:
        raise ValueError("Document asset unavailable")
    with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
        with archive.open(path) as source:
            data = source.read(matches[0].size + 1)
    if len(data) != matches[0].size or hashlib.sha256(data).hexdigest() != matches[0].sha256:
        raise ValueError("Document asset integrity mismatch")
    return data
