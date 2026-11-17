"""Bounded smart-HTTP acquisition for offline verification of a Hub commit.

This only downloads a depth-one pack. It does not authorize a user, verify Hub
privacy, expand Git objects, or attest corpus contents. The caller must run it
inside a worker with a hard overall deadline, then independently verify the
commit/tree/blobs. No Git subprocess is allowed to fetch missing objects.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import struct
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import httpx
from huggingface_hub.utils import HFValidationError, validate_repo_id

MAX_PACK_BYTES = 128 * 1024 * 1024
MAX_PACK_OBJECTS = 20_000
MAX_ADVERTISEMENT_BYTES = 256 * 1024
MAX_RESPONSE_BYTES = 144 * 1024 * 1024
MAX_PROGRESS_BYTES = 64 * 1024
_SHA = re.compile(r"[0-9a-f]{40}")


class CorpusGitAcquisitionError(ValueError):
    """A complete bounded pack could not be acquired for the exact revision."""


def _fail() -> CorpusGitAcquisitionError:
    return CorpusGitAcquisitionError("Corpus Git transfer could not be verified")


def _remaining(deadline: float) -> float:
    value = deadline - time.monotonic()
    if not 0 < value <= 120:
        raise _fail()
    return value


class _Packets:
    def __init__(self, chunks: Iterator[bytes], maximum: int, deadline: float) -> None:
        self.chunks, self.maximum, self.deadline = chunks, maximum, deadline
        self.buffer = bytearray()
        self.received = 0

    def _read(self, size: int, *, allow_eof: bool = False) -> bytes:
        while len(self.buffer) < size:
            _remaining(self.deadline)
            chunk = next(self.chunks, None)
            if chunk is None:
                if allow_eof and not self.buffer:
                    return b""
                raise _fail()
            self.received += len(chunk)
            if self.received > self.maximum:
                raise _fail()
            self.buffer.extend(chunk)
        value = bytes(self.buffer[:size])
        del self.buffer[:size]
        return value

    def packet(self) -> bytes | None:
        header = self._read(4)
        if not re.fullmatch(rb"[0-9a-fA-F]{4}", header):
            raise _fail()
        length = int(header, 16)
        if length == 0:
            return None
        if not 4 <= length <= 65520:
            raise _fail()
        return self._read(length - 4)

    def finish(self) -> None:
        if self._read(1, allow_eof=True):
            raise _fail()


def _advertisement(packets: _Packets, ref: str, sha: str) -> set[bytes]:
    service = packets.packet()
    if service is None or service.removesuffix(b"\n") != b"# service=git-upload-pack":
        raise _fail()
    if packets.packet() is not None:
        raise _fail()
    first = packets.packet()
    if first is None or first.count(b"\0") != 1:
        raise _fail()
    entry, raw_capabilities = first.removesuffix(b"\n").split(b"\0")
    capabilities = set(raw_capabilities.split(b" "))
    if not {b"shallow", b"side-band-64k"} <= capabilities:
        raise _fail()
    if any(c.startswith(b"object-format=") and c != b"object-format=sha1" for c in capabilities):
        raise _fail()
    refs: dict[bytes, bytes] = {}
    while True:
        match = re.fullmatch(rb"([0-9a-f]{40}) ([^\x00-\x20\x7f]+)", entry)
        if match is None or match[2] in refs:
            raise _fail()
        refs[match[2]] = match[1]
        entry = packets.packet()
        if entry is None:
            break
        entry = entry.removesuffix(b"\n")
    packets.finish()
    if refs.get(ref.encode("ascii")) != sha.encode("ascii"):
        raise _fail()
    return capabilities


def _pkt(value: bytes) -> bytes:
    return f"{len(value) + 4:04x}".encode("ascii") + value


@dataclass(frozen=True)
class DownloadedGitPack:
    size: int
    object_count: int
    checksum: str


def _pack(packets: _Packets, sha: str, path: Path) -> DownloadedGitPack:
    update = packets.packet()
    if update is not None:
        if update.removesuffix(b"\n") != b"shallow " + sha.encode("ascii"):
            raise _fail()
        if packets.packet() is not None:
            raise _fail()
    acknowledgement = packets.packet()
    if acknowledgement is None or acknowledgement.removesuffix(b"\n") != b"NAK":
        raise _fail()
    size, progress, object_count = 0, 0, 0
    prefix, tail = bytearray(), bytearray()
    digest = hashlib.sha1(usedforsecurity=False)
    with path.open("xb") as output:
        while (packet := packets.packet()) is not None:
            if not packet:
                raise _fail()
            if packet[0] == 2:
                progress += len(packet) - 1
                if progress > MAX_PROGRESS_BYTES:
                    raise _fail()
                continue
            if packet[0] != 1 or len(packet) == 1:
                raise _fail()
            data = packet[1:]
            size += len(data)
            if size > MAX_PACK_BYTES:
                raise _fail()
            if len(prefix) < 12:
                prefix.extend(data[: 12 - len(prefix)])
                if len(prefix) == 12:
                    magic, version, object_count = struct.unpack(">4sII", prefix)
                    if magic != b"PACK" or version != 2 or not 0 < object_count <= MAX_PACK_OBJECTS:
                        raise _fail()
            tail.extend(data)
            if len(tail) > 20:
                digest.update(tail[:-20])
                del tail[:-20]
            output.write(data)
        packets.finish()
        if size < 32 or not object_count or not hmac.compare_digest(digest.digest(), tail):
            raise _fail()
    return DownloadedGitPack(size, object_count, digest.hexdigest())


def download_commit_pack(
    repo_id: str,
    repo_type: str,
    ref: str,
    sha: str,
    token: str,
    path: Path,
    *,
    deadline: float,
    transport: httpx.BaseTransport | None = None,
) -> DownloadedGitPack:
    """Acquire the exact advertised commit without redirects or protocol fallback.

    A cooperative deadline is checked between bounded reads. A parent process
    must enforce the hard deadline even when the peer slowly fills one chunk.
    The output path must be new and inside the caller's private temporary area.
    """
    try:
        validate_repo_id(repo_id)
        if (
            repo_id.count("/") != 1
            or repo_type not in ("dataset", "model")
            or not isinstance(sha, str)
            or not _SHA.fullmatch(sha)
            or sha == "0" * 40
            or not isinstance(ref, str)
            or not re.fullmatch(r"refs/heads/[A-Za-z0-9][A-Za-z0-9_-]{0,100}", ref)
            or not isinstance(token, str)
            or not token
            or path.exists()
        ):
            raise _fail()
        _remaining(deadline)
    except (HFValidationError, TypeError, AttributeError) as error:
        raise _fail() from error
    base = "https://huggingface.co/" + ("datasets/" if repo_type == "dataset" else "") + repo_id
    headers = {"Accept-Encoding": "identity", "Cache-Control": "no-cache"}
    try:
        with httpx.Client(
            auth=("user", token), follow_redirects=False, trust_env=False, transport=transport
        ) as client:
            with client.stream(
                "GET",
                base + "/info/refs?service=git-upload-pack",
                headers=headers,
                timeout=_remaining(deadline),
            ) as response:
                _check_response(response, "advertisement")
                capabilities = _advertisement(
                    _Packets(response.iter_raw(65536), MAX_ADVERTISEMENT_BYTES, deadline), ref, sha
                )
            selected = b"side-band-64k"
            if b"no-progress" in capabilities:
                selected += b" no-progress"
            body = (
                _pkt(b"want " + sha.encode("ascii") + b" " + selected + b"\n")
                + _pkt(b"deepen 1\n")
                + b"0000"
                + _pkt(b"done\n")
            )
            with client.stream(
                "POST",
                base + "/git-upload-pack",
                content=body,
                headers={**headers, "Content-Type": "application/x-git-upload-pack-request"},
                timeout=_remaining(deadline),
            ) as response:
                _check_response(response, "result")
                return _pack(
                    _Packets(response.iter_raw(65536), MAX_RESPONSE_BYTES, deadline), sha, path
                )
    except (httpx.HTTPError, OSError, ValueError) as error:
        path.unlink(missing_ok=True)
        raise _fail() from error


def _check_response(response: httpx.Response, kind: str) -> None:
    if (
        response.status_code != 200
        or response.headers.get("content-type") != "application/x-git-upload-pack-" + kind
        or response.headers.get("content-encoding", "identity").lower() != "identity"
    ):
        raise _fail()
