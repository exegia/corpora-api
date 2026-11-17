"""Bounded offline decoder for the SHA-1 packs requested by corpus_git_acquire.

Only ordinary objects and REF_DELTA are accepted: acquisition does not request
ofs-delta or thin packs. No object can be loaded from outside this pack. The
caller still needs the exact commit/tree/file verifier and a killable worker.
Format: https://git-scm.com/docs/gitformat-pack
"""

from __future__ import annotations

import hashlib
import hmac
import struct
import zlib
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path

from corpora_api.corpus_git_acquire import MAX_PACK_BYTES, MAX_PACK_OBJECTS
from corpora_api.tf_check import MAX_UNPACKED_BYTES

MAX_INFLATED_BYTES = 128 * 1024 * 1024
MAX_RESOLVED_BYTES = 128 * 1024 * 1024
MAX_DELTA_DEPTH = 64
_KINDS = {1: "commit", 2: "tree", 3: "blob", 4: "tag"}


class CorpusGitPackError(ValueError):
    """The bounded pack cannot be decoded completely and independently."""


def _invalid() -> CorpusGitPackError:
    return CorpusGitPackError("Corpus Git pack is invalid or exceeds limits")


@dataclass(frozen=True)
class GitObject:
    kind: str
    data: bytes

    def oid(self) -> str:
        digest = hashlib.sha1(usedforsecurity=False)
        digest.update(self.kind.encode("ascii") + b" " + str(len(self.data)).encode() + b"\0")
        digest.update(self.data)
        return digest.hexdigest()


def _inflate(source: memoryview, position: int, size: int) -> tuple[bytes, int]:
    decoder = zlib.decompressobj()
    output = bytearray()
    while not decoder.eof:
        chunk = source[position : position + 65536]
        if not chunk:
            raise _invalid()
        # One extra byte detects dishonest sizes, including declared zero.
        piece = decoder.decompress(chunk, min(65536, size - len(output) + 1))
        consumed = len(chunk) - len(decoder.unused_data if decoder.eof else decoder.unconsumed_tail)
        if (not consumed and not piece) or len(output) + len(piece) > size:
            raise _invalid()
        position += consumed
        output.extend(piece)
    if len(output) != size:
        raise _invalid()
    return bytes(output), position


def _size(data: bytes, position: int) -> tuple[int, int]:
    value, shift = 0, 0
    while True:
        if position >= len(data) or shift >= 35:
            raise _invalid()
        byte = data[position]
        position += 1
        value |= (byte & 0x7F) << shift
        if value > MAX_UNPACKED_BYTES:
            raise _invalid()
        if not byte & 0x80:
            return value, position
        shift += 7


def _apply_delta(base: bytes, instructions: bytes, remaining: int) -> bytes:
    base_size, position = _size(instructions, 0)
    target_size, position = _size(instructions, position)
    if base_size != len(base) or target_size > remaining:
        raise _invalid()
    output = bytearray()
    while position < len(instructions):
        opcode = instructions[position]
        position += 1
        if not opcode:
            raise _invalid()
        if opcode & 0x80:
            offset, count = 0, 0
            for bit in range(7):
                if opcode & (1 << bit):
                    if position == len(instructions):
                        raise _invalid()
                    value = instructions[position]
                    position += 1
                    if bit < 4:
                        offset |= value << (8 * bit)
                    else:
                        count |= value << (8 * (bit - 4))
            count = count or 65536
            if offset + count > len(base) or len(output) + count > target_size:
                raise _invalid()
            output.extend(memoryview(base)[offset : offset + count])
        else:
            if position + opcode > len(instructions) or len(output) + opcode > target_size:
                raise _invalid()
            output.extend(instructions[position : position + opcode])
            position += opcode
    if len(output) != target_size:
        raise _invalid()
    return bytes(output)


def decode_git_pack(path: Path) -> dict[str, GitObject]:
    """Decode a complete pack with bounded expansion and delta resolution work.

    Both cumulative budgets charge duplicates too. Each waiting delta is
    processed once, and each chain has a fixed depth limit. Object hashes do
    not establish the expected candidate: the tree verifier supplies that check.
    """
    try:
        with path.open("rb") as stream:
            raw = stream.read(MAX_PACK_BYTES + 1)
        if not 32 <= len(raw) <= MAX_PACK_BYTES:
            raise _invalid()
        data = memoryview(raw)
        if not hmac.compare_digest(
            hashlib.sha1(data[:-20], usedforsecurity=False).digest(), data[-20:]
        ):
            raise _invalid()
        magic, version, count = struct.unpack(">4sII", data[:12])
        if magic != b"PACK" or version != 2 or not 0 < count <= MAX_PACK_OBJECTS:
            raise _invalid()
        position, inflated, resolved, completed = 12, 0, 0, 0
        objects: dict[str, GitObject] = {}
        depths: dict[str, int] = {}
        waiting: dict[str, list[bytes]] = defaultdict(list)
        available: deque[tuple[GitObject, int]] = deque()

        def enqueue(obj: GitObject, depth: int) -> None:
            nonlocal resolved
            resolved += len(obj.data)
            if resolved > MAX_RESOLVED_BYTES or depth > MAX_DELTA_DEPTH:
                raise _invalid()
            available.append((obj, depth))

        def resolve() -> None:
            nonlocal completed
            while available:
                obj, depth = available.popleft()
                completed += 1
                oid = obj.oid()
                if oid in objects:
                    # Legitimate pack output deduplicates objects. Rejecting
                    # duplicates also prevents ambiguous dependency accounting.
                    raise _invalid()
                objects[oid], depths[oid] = obj, depth
                for delta in waiting.pop(oid, []):
                    result = _apply_delta(obj.data, delta, MAX_RESOLVED_BYTES - resolved)
                    enqueue(GitObject(obj.kind, result), depth + 1)

        # Resolve each object immediately, allowing forward REF_DELTA bases
        # through a queue keyed by the canonical object hash.
        for _ in range(count):
            if position >= len(data) - 20:
                raise _invalid()
            byte = data[position]
            position += 1
            kind, size, shift = (byte >> 4) & 7, byte & 15, 4
            while byte & 0x80:
                if position >= len(data) - 20 or shift >= 35:
                    raise _invalid()
                byte = data[position]
                position += 1
                size |= (byte & 0x7F) << shift
                shift += 7
            if kind not in (*_KINDS, 7) or size > MAX_INFLATED_BYTES - inflated:
                raise _invalid()
            if kind != 7 and size > MAX_UNPACKED_BYTES:
                raise _invalid()
            base_oid = None
            if kind == 7:
                if position + 20 > len(data) - 20:
                    raise _invalid()
                base_oid = data[position : position + 20].hex()
                position += 20
            payload, position = _inflate(data[:-20], position, size)
            inflated += size
            if base_oid is None:
                enqueue(GitObject(_KINDS[kind], payload), 0)
            elif base_oid in objects:
                base = objects[base_oid]
                result = _apply_delta(base.data, payload, MAX_RESOLVED_BYTES - resolved)
                enqueue(GitObject(base.kind, result), depths[base_oid] + 1)
            else:
                waiting[base_oid].append(payload)
            resolve()
        if position != len(data) - 20 or waiting or completed != count:
            raise _invalid()
        return objects
    except (OSError, zlib.error, struct.error, OverflowError) as error:
        raise _invalid() from error
