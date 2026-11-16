"""Unwired structural verification of SHA-1 Git commit and tree objects.

This checks hashes of uncompressed object payloads, the direct parent, paths and
regular-file modes. It does not authenticate where the objects came from, check
Hub privacy or references, or verify blob/LFS payload bytes. The expected path
set must explicitly include any repository-managed files such as .gitattributes.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from collections.abc import Mapping
from dataclasses import dataclass

from corpora_api.tf_check import MAX_FILES, MAX_UNPACKED_BYTES

_MAX_COMMIT_BYTES = 65_536
_MAX_TREE_OBJECTS = 10_000
_MAX_TREE_ENTRIES = 100_000
_MAX_TREE_DEPTH = 128
_MAX_PATH_BYTES = 4096
_SHA1 = re.compile(r"[0-9a-f]{40}")


class CorpusGitTreeError(ValueError):
    """The candidate's Git structure cannot be authenticated safely."""


@dataclass(frozen=True)
class CorpusGitFile:
    path: str
    mode: str
    blob_sha1: str


def verify_corpus_git_file(
    entry: CorpusGitFile,
    blob_payload: bytes,
    resolved_bytes: bytes,
    expected_size: int,
    expected_sha256: str,
) -> None:
    """Verify one authenticated Git blob and its expected resolved content.

    For LFS, the blob must contain the standard three-line pointer and its
    SHA-256/size must match the downloaded payload. Extended pointer syntax is
    not supported yet. The caller must bound aggregate downloads separately.
    """
    if (
        not isinstance(entry, CorpusGitFile)
        or entry.mode not in ("100644", "100755")
        or not isinstance(entry.blob_sha1, str)
        or not _SHA1.fullmatch(entry.blob_sha1)
        or type(blob_payload) is not bytes
        or len(blob_payload) > MAX_UNPACKED_BYTES
        or type(resolved_bytes) is not bytes
        or type(expected_size) is not int
        or not 0 <= expected_size <= MAX_UNPACKED_BYTES
        or len(resolved_bytes) != expected_size
        or not isinstance(expected_sha256, str)
        or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256)
    ):
        raise CorpusGitTreeError("Invalid Git file verification input")
    if _object_id(b"blob", blob_payload) != entry.blob_sha1:
        raise CorpusGitTreeError("Git blob hash does not match")
    actual_sha256 = hashlib.sha256(resolved_bytes).hexdigest()
    if not hmac.compare_digest(actual_sha256, expected_sha256):
        raise CorpusGitTreeError("Corpus file contents do not match")
    if blob_payload == resolved_bytes:
        return
    if len(blob_payload) > 1024:
        raise CorpusGitTreeError("Git LFS pointer is invalid")
    pointer = (
        b"version https://git-lfs.github.com/spec/v1\noid sha256:"
        + actual_sha256.encode("ascii")
        + b"\nsize "
        + str(expected_size).encode("ascii")
        + b"\n"
    )
    if blob_payload != pointer:
        raise CorpusGitTreeError("Git LFS pointer is invalid")


def _object_id(kind: bytes, payload: bytes) -> str:
    header = kind + b" " + str(len(payload)).encode("ascii") + b"\0"
    digest = hashlib.sha1(usedforsecurity=False)
    digest.update(header)
    digest.update(payload)
    return digest.hexdigest()


def _component(raw: bytes) -> str:
    try:
        name = raw.decode("utf-8", errors="strict")
    except UnicodeError as error:
        raise CorpusGitTreeError("Git tree contains an invalid path") from error
    if (
        not name
        or name in (".", "..")
        or name.casefold() == ".git"
        or "/" in name
        or "\\" in name
        or any(ord(char) < 32 or ord(char) == 127 for char in name)
    ):
        raise CorpusGitTreeError("Git tree contains an unsafe or ambiguous path")
    return name


def verify_corpus_git_structure(
    candidate_sha: str,
    expected_parent_sha: str,
    commit_payload: bytes,
    trees: Mapping[str, bytes],
    expected_paths: frozenset[str],
) -> tuple[CorpusGitFile, ...]:
    """Verify candidate object, one direct parent and complete regular-file tree.

    This is structural evidence only. Actual blob and LFS payloads must be
    independently checked against trusted archive hashes before publication.
    """
    if (
        not isinstance(candidate_sha, str)
        or not _SHA1.fullmatch(candidate_sha)
        or not isinstance(expected_parent_sha, str)
        or not _SHA1.fullmatch(expected_parent_sha)
        or candidate_sha == expected_parent_sha
        or type(commit_payload) is not bytes
        or len(commit_payload) > _MAX_COMMIT_BYTES
        or not isinstance(trees, Mapping)
        or len(trees) > _MAX_TREE_OBJECTS
        or not isinstance(expected_paths, frozenset)
        or len(expected_paths) > MAX_FILES + 1
    ):
        raise CorpusGitTreeError("Invalid Git verification input")
    if _object_id(b"commit", commit_payload) != candidate_sha:
        raise CorpusGitTreeError("Git commit hash does not match")
    try:
        headers, _message = commit_payload.split(b"\n\n", 1)
    except ValueError as error:
        raise CorpusGitTreeError("Git commit headers are incomplete") from error
    lines = headers.split(b"\n")
    if (
        len(lines) < 2
        or not re.fullmatch(rb"tree [0-9a-f]{40}", lines[0])
        or lines[1] != b"parent " + expected_parent_sha.encode("ascii")
    ):
        raise CorpusGitTreeError("Git commit parent or tree is invalid")
    continuation_allowed = False
    for line in lines[2:]:
        if line.startswith(b" "):
            if not continuation_allowed:
                raise CorpusGitTreeError("Git commit header continuation is invalid")
            continue
        key, separator, _value = line.partition(b" ")
        if not separator or not key or key in (b"tree", b"parent"):
            raise CorpusGitTreeError("Git commit headers are invalid")
        continuation_allowed = True

    required_dirs: set[str] = set()
    for path in expected_paths:
        try:
            if not isinstance(path, str) or len(path.encode("utf-8")) > _MAX_PATH_BYTES:
                raise CorpusGitTreeError("Invalid expected Git path")
            parts = path.split("/")
            for part in parts:
                _component(part.encode("utf-8"))
            required_dirs.update("/".join(parts[:i]) for i in range(1, len(parts)))
        except UnicodeError as error:
            raise CorpusGitTreeError("Invalid expected Git path") from error

    # Copy references once: callers must supply immutable byte objects.
    objects = dict(trees)
    if (
        any(
            not isinstance(key, str) or not _SHA1.fullmatch(key) or type(value) is not bytes
            for key, value in objects.items()
        )
        or sum(len(value) for value in objects.values()) > MAX_UNPACKED_BYTES
    ):
        raise CorpusGitTreeError("Git tree objects exceed limits")
    root = lines[0][5:].decode("ascii")
    pending = [(root, "", frozenset())]
    files: dict[str, CorpusGitFile] = {}
    directories: set[str] = set()
    entry_count = 0
    while pending:
        oid, prefix, ancestors = pending.pop()
        if oid in ancestors or len(ancestors) >= _MAX_TREE_DEPTH:
            raise CorpusGitTreeError("Git tree depth or cycle is invalid")
        payload = objects.get(oid)
        if payload is None or _object_id(b"tree", payload) != oid:
            raise CorpusGitTreeError("Git tree object is missing or mismatched")
        offset, seen = 0, set()
        previous_key = b""
        while offset < len(payload):
            entry_count += 1
            if entry_count > _MAX_TREE_ENTRIES:
                raise CorpusGitTreeError("Git tree contains too many entries")
            space = payload.find(b" ", offset)
            zero = payload.find(b"\0", space + 1) if space >= 0 else -1
            if (
                space < 0
                or space - offset > 6
                or zero < 0
                or zero - space - 1 > _MAX_PATH_BYTES - len(prefix.encode("utf-8"))
                or zero + 21 > len(payload)
            ):
                raise CorpusGitTreeError("Git tree entry is incomplete")
            mode, raw_name = payload[offset:space], payload[space + 1 : zero]
            if mode not in (b"40000", b"100644", b"100755"):
                raise CorpusGitTreeError("Git tree contains unsupported file modes")
            sort_key = raw_name + (b"/" if mode == b"40000" else b"\0")
            if sort_key <= previous_key:
                raise CorpusGitTreeError("Git tree entries are not sorted")
            previous_key = sort_key
            name = _component(raw_name)
            if name in seen:
                raise CorpusGitTreeError("Git tree contains duplicate paths")
            seen.add(name)
            path = prefix + name
            if len(path.encode("utf-8")) > _MAX_PATH_BYTES:
                raise CorpusGitTreeError("Git tree path is too long")
            child = payload[zero + 1 : zero + 21].hex()
            offset = zero + 21
            if mode == b"40000":
                directories.add(path)
                pending.append((child, path + "/", ancestors | {oid}))
            elif mode in (b"100644", b"100755"):
                if path in files or len(files) >= MAX_FILES + 1:
                    raise CorpusGitTreeError("Git tree contains too many files")
                files[path] = CorpusGitFile(path, mode.decode("ascii"), child)
            else:
                raise CorpusGitTreeError("Git tree contains unsupported file modes")
    if frozenset(files) != expected_paths or directories != required_dirs:
        raise CorpusGitTreeError("Git tree paths do not match the expected corpus")
    return tuple(files[path] for path in sorted(files))
