"""Bounded inventory of corpus ZIP bytes; this does not validate a corpus.

The inventory describes original archive entries, not the temporary tree used
by a validator that may rebuild caches. Callers must separately validate the
same immutable input bytes before using this for publication.
"""

from __future__ import annotations

import hashlib
import io
import stat
import struct
import zipfile
import zlib
from dataclasses import dataclass

from corpora_api.tf_check import MAX_ARCHIVE_BYTES, MAX_FILES, MAX_UNPACKED_BYTES


class CorpusInventoryError(ValueError):
    """An archive cannot be inventoried safely and completely."""


@dataclass(frozen=True)
class CorpusFileDigest:
    path: str
    size: int
    sha256: str


@dataclass(frozen=True)
class CorpusArchiveInventory:
    archive_size: int
    archive_sha256: str
    files: tuple[CorpusFileDigest, ...]


def _compressed_payload(archive: bytes, entry: zipfile.ZipInfo, boundary: int) -> bytes:
    """Locate the raw entry after ZipFile has checked its local header."""
    offset = entry.header_offset
    header = struct.unpack_from("<4s5H3L2H", archive, offset)
    if (
        header[0] != b"PK\x03\x04"
        or header[2] != entry.flag_bits
        or header[3] != entry.compress_type
        or header[7] == 0xFFFFFFFF
        or header[8] == 0xFFFFFFFF
    ):
        raise CorpusInventoryError("Corpus archive local header is invalid")
    start = offset + 30 + header[-2] + header[-1]
    end = start + entry.compress_size
    if start < offset or end > boundary:
        raise CorpusInventoryError("Corpus archive entry is truncated")
    if entry.flag_bits & 8:
        if any(
            local not in (0, expected)
            for local, expected in zip(
                (header[6], header[7], header[8]),
                (entry.CRC, entry.compress_size, entry.file_size),
                strict=True,
            )
        ):
            raise CorpusInventoryError("Corpus archive local header is inconsistent")
        positions = [end]
        if archive[end : end + 4] == b"PK\x07\x08":
            positions.append(end + 4)
        expected = (entry.CRC, entry.compress_size, entry.file_size)
        if not any(
            position + 12 <= boundary and struct.unpack_from("<3L", archive, position) == expected
            for position in positions
        ):
            raise CorpusInventoryError("Corpus archive descriptor is inconsistent")
    elif (header[6], header[7], header[8]) != (entry.CRC, entry.compress_size, entry.file_size):
        raise CorpusInventoryError("Corpus archive local header is inconsistent")
    return archive[start:end]


def _digest_entry(
    archive: bytes, entry: zipfile.ZipInfo, budget: int, boundary: int
) -> CorpusFileDigest:
    raw = _compressed_payload(archive, entry, boundary)
    digest = hashlib.sha256()
    crc = 0
    size = 0

    def include(chunk: bytes) -> None:
        nonlocal crc, size
        size += len(chunk)
        if size > budget or size > entry.file_size:
            raise CorpusInventoryError("Corpus archive expands beyond the limit")
        digest.update(chunk)
        crc = zlib.crc32(chunk, crc)

    if entry.compress_type == zipfile.ZIP_STORED:
        include(raw)
    elif entry.compress_type == zipfile.ZIP_DEFLATED:
        decoder = zlib.decompressobj(-15)
        pending = raw
        while True:
            before = len(pending)
            chunk = decoder.decompress(pending, 65536)
            include(chunk)
            pending = decoder.unconsumed_tail
            if decoder.eof:
                if decoder.unused_data or pending:
                    raise CorpusInventoryError("Corpus archive has trailing compressed data")
                break
            if not chunk and len(pending) == before:
                raise CorpusInventoryError("Corpus archive compressed stream is incomplete")
    else:
        raise CorpusInventoryError("Corpus archive compression method is unsupported")
    if size != entry.file_size or crc != entry.CRC:
        raise CorpusInventoryError("Corpus archive entry size or checksum is inconsistent")
    return CorpusFileDigest(entry.filename, size, digest.hexdigest())


def inventory_corpus_archive(archive: bytes) -> CorpusArchiveInventory:
    """Hash each regular ZIP entry without extracting it or claiming validation."""
    if not isinstance(archive, bytes) or not 0 < len(archive) <= MAX_ARCHIVE_BYTES:
        raise CorpusInventoryError("Corpus archive size is invalid")
    try:
        with zipfile.ZipFile(io.BytesIO(archive)) as source:
            entries = source.infolist()
            if not entries or len(entries) > MAX_FILES:
                raise CorpusInventoryError("Corpus archive entry count is invalid")
            offsets = sorted(entry.header_offset for entry in entries)
            if (
                len(set(offsets)) != len(offsets)
                or offsets[0] < 0
                or offsets[-1] >= source.start_dir
            ):
                raise CorpusInventoryError("Corpus archive has overlapping entries")
            boundaries = dict(zip(offsets, offsets[1:] + [source.start_dir], strict=True))
            files: dict[str, zipfile.ZipInfo] = {}
            directories: set[str] = set()
            declared = 0
            for entry in entries:
                name = entry.filename
                raw = entry.orig_filename
                is_dir = entry.is_dir()
                path = name[:-1] if is_dir else name
                parts = path.split("/")
                if (
                    raw != name
                    or not path
                    or name.startswith("/")
                    or "\\" in name
                    or any(not p or p in (".", "..") or p.casefold() == ".git" for p in parts)
                    or any(ord(c) < 32 or ord(c) == 127 for c in name)
                    or entry.flag_bits & 1
                ):
                    raise CorpusInventoryError("Corpus archive contains an unsafe entry")
                mode = (entry.external_attr >> 16) if entry.create_system == 3 else 0
                kind = stat.S_IFMT(mode)
                if (
                    kind not in (0, stat.S_IFREG, stat.S_IFDIR)
                    or (kind == stat.S_IFDIR and not is_dir)
                    or (kind == stat.S_IFREG and is_dir)
                ):
                    raise CorpusInventoryError("Corpus archive contains a special file")
                if path in files or path in directories:
                    raise CorpusInventoryError("Corpus archive contains duplicate paths")
                if is_dir:
                    if entry.file_size != 0:
                        raise CorpusInventoryError("Corpus archive directory has content")
                    with source.open(entry) as stream:
                        if stream.read(1):
                            raise CorpusInventoryError("Corpus archive directory has content")
                    _digest_entry(archive, entry, 0, boundaries[entry.header_offset])
                    directories.add(path)
                else:
                    files[path] = entry
                    declared += entry.file_size
                    if declared > MAX_UNPACKED_BYTES:
                        raise CorpusInventoryError("Corpus archive expands beyond the limit")
            if not files:
                raise CorpusInventoryError("Corpus archive contains no files")
            for path in files.keys() | directories:
                parts = path.split("/")
                if any("/".join(parts[:i]) in files for i in range(1, len(parts))):
                    raise CorpusInventoryError("Corpus archive contains conflicting paths")
            result = []
            expanded = 0
            for _, entry in sorted(files.items()):
                # Opening checks the local filename against the central directory.
                with source.open(entry):
                    pass
                item = _digest_entry(
                    archive, entry, MAX_UNPACKED_BYTES - expanded, boundaries[entry.header_offset]
                )
                expanded += item.size
                result.append(item)
            return CorpusArchiveInventory(
                len(archive), hashlib.sha256(archive).hexdigest(), tuple(result)
            )
    except CorpusInventoryError:
        raise
    except (
        zipfile.BadZipFile,
        OSError,
        RuntimeError,
        EOFError,
        NotImplementedError,
        zlib.error,
        struct.error,
        ValueError,
    ) as error:
        raise CorpusInventoryError("Corpus archive is unreadable") from error
