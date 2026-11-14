"""Bounded static preflight for an uploaded Text-Fabric ZIP bundle."""

import hashlib
import io
import re
import zipfile
from pathlib import PurePosixPath
from typing import Any

MAX_ARCHIVE_BYTES = 3_000_000
MAX_UNPACKED_BYTES = 64_000_000
MAX_FILES = 3_000
MAX_NODES = 1_000_000
NODE_SPEC = re.compile(r"^[1-9][0-9]*(?:-[1-9][0-9]*)?(?:,[1-9][0-9]*(?:-[1-9][0-9]*)?)*$")


class TFArchiveRejected(ValueError):
    """The uploaded archive cannot be checked safely or is not a TF bundle."""


def _data_lines(raw: bytes, name: str) -> list[tuple[int, str]]:
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise TFArchiveRejected(f"{name} is not UTF-8") from error
    lines = text.splitlines()
    try:
        start = lines.index("") + 1
    except ValueError as error:
        raise TFArchiveRejected(f"{name} has no metadata separator") from error
    return [(number, value) for number, value in enumerate(lines[start:], start + 1) if value]


def _numbers(spec: str, max_node: int) -> list[tuple[int, int]]:
    if not NODE_SPEC.fullmatch(spec):
        raise ValueError("invalid node range")
    ranges = []
    for part in spec.split(","):
        ends = [int(value) for value in part.split("-")]
        lo, hi = (ends[0], ends[0]) if len(ends) == 1 else (ends[0], ends[1])
        if lo > hi or hi > max_node:
            raise ValueError("node range is reversed or exceeds the limit")
        ranges.append((lo, hi))
    return ranges


def check_tf_archive(archive: bytes) -> dict[str, Any]:
    """Check WARP node typing and containment without executing corpus content."""
    if not archive or len(archive) > MAX_ARCHIVE_BYTES:
        raise TFArchiveRejected("Upload a nonempty ZIP of at most 3 MB")
    try:
        source = zipfile.ZipFile(io.BytesIO(archive))
    except zipfile.BadZipFile as error:
        raise TFArchiveRejected("Upload a valid ZIP archive") from error

    with source:
        files: dict[str, zipfile.ZipInfo] = {}
        total = 0
        for entry in source.infolist():
            path = PurePosixPath(entry.filename)
            if (
                path.is_absolute()
                or ".." in path.parts
                or "\\" in entry.filename
                or (entry.external_attr >> 16) & 0o170000 == 0o120000
            ):
                raise TFArchiveRejected("ZIP contains an unsafe path or link")
            if entry.is_dir():
                continue
            if entry.filename in files:
                raise TFArchiveRejected("ZIP contains a duplicate path")
            files[entry.filename] = entry
            total += entry.file_size
            if len(files) > MAX_FILES or total > MAX_UNPACKED_BYTES:
                raise TFArchiveRejected("ZIP exceeds the file or unpacked-size limit")

        roots = [
            PurePosixPath(name).parent
            for name in files
            if name.endswith("/otype.tf") or name == "otype.tf"
        ]
        if len(roots) != 1:
            raise TFArchiveRejected("ZIP must contain exactly one Text-Fabric root with otype.tf")
        root = roots[0]

        def feature(name: str) -> bytes | None:
            path = str(root / name)
            if path not in files:
                return None
            try:
                return source.read(files[path])
            except (RuntimeError, zipfile.BadZipFile, OSError) as error:
                raise TFArchiveRejected(f"Cannot read {name} from ZIP") from error

        otype = feature("otype.tf")
        oslots = feature("oslots.tf")
        otext = feature("otext.tf")
        issues: list[dict[str, Any]] = []

        def issue(code: str, filename: str, line: int, message: str) -> None:
            issues.append(
                {
                    "code": code,
                    "severity": "error",
                    "file": filename,
                    "line": line,
                    "message": message,
                }
            )

        if oslots is None:
            issue("MISSING_OSLOTS", "oslots.tf", 0, "Containment edges are missing")
        if otext is None:
            issue("MISSING_OTEXT", "otext.tf", 0, "Text configuration is missing")
        assert otype is not None
        if not otype.startswith(b"@node\n"):
            issue("OTYPE_HEADER", "otype.tf", 1, "Expected @node header")
        types: list[str] = [""]
        for line, value in _data_lines(otype, "otype.tf"):
            parts = value.split("\t")
            if len(parts) not in (1, 2) or not parts[-1]:
                issue("OTYPE_ROW", "otype.tf", line, "Expected a type or node range and type")
                continue
            try:
                ranges = (
                    _numbers(parts[0], MAX_NODES) if len(parts) == 2 else [(len(types), len(types))]
                )
            except ValueError:
                issue("OTYPE_RANGE", "otype.tf", line, "Invalid node range")
                continue
            for lo, hi in ranges:
                if lo != len(types):
                    issue(
                        "OTYPE_SEQUENCE",
                        "otype.tf",
                        line,
                        "Node ranges must cover 1..maxNode once, in order",
                    )
                    continue
                types.extend([parts[-1]] * (hi - lo + 1))
        max_node = len(types) - 1
        slot_type = types[1] if max_node else None
        max_slot = 0
        if slot_type:
            for node_type in types[1:]:
                if node_type != slot_type:
                    break
                max_slot += 1
            if slot_type in types[max_slot + 1 :]:
                issue(
                    "SLOT_SEQUENCE",
                    "otype.tf",
                    0,
                    "Slot nodes must occupy only the first contiguous block",
                )
        else:
            issue("NO_NODES", "otype.tf", 0, "No nodes are defined")

        covered: set[int] = set()
        if oslots is not None:
            if not oslots.startswith(b"@edge\n"):
                issue("OSLOTS_HEADER", "oslots.tf", 1, "Expected @edge header")
            for line, value in _data_lines(oslots, "oslots.tf"):
                parts = value.split("\t")
                if len(parts) != 2:
                    issue("OSLOTS_ROW", "oslots.tf", line, "Expected source node and slot range")
                    continue
                try:
                    sources = _numbers(parts[0], MAX_NODES)
                    ranges = _numbers(parts[1], MAX_NODES)
                except ValueError:
                    issue("OSLOTS_RANGE", "oslots.tf", line, "Invalid containment range")
                    continue
                for source_lo, source_hi in sources:
                    for node in range(source_lo, source_hi + 1):
                        if node <= max_slot or node > max_node or node in covered:
                            issue(
                                "OSLOTS_SOURCE",
                                "oslots.tf",
                                line,
                                "Source must be a unique non-slot node",
                            )
                            continue
                        covered.add(node)
                if any(lo < 1 or hi > max_slot for lo, hi in ranges):
                    issue(
                        "OSLOTS_TARGET",
                        "oslots.tf",
                        line,
                        "Containment must reference existing slots",
                    )
            missing = max_node - max_slot - len(covered)
            if missing > 0:
                issue(
                    "OSLOTS_MISSING",
                    "oslots.tf",
                    0,
                    f"{missing} non-slot node(s) have no containment entry",
                )

        if otext is not None and not otext.startswith(b"@config\n"):
            issue("OTEXT_HEADER", "otext.tf", 1, "Expected @config header")
        return {
            "status": "failed" if issues else "passed",
            "check_scope": "static-warp-preflight",
            "source_sha256": hashlib.sha256(archive).hexdigest(),
            "tf_root": str(root),
            "slot_type": slot_type,
            "max_slot": max_slot,
            "max_node": max_node,
            "issues": issues[:100],
            "issue_count": len(issues),
        }
