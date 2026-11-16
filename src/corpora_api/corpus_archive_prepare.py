"""Unwired preparation of the exact corpus bytes selected for a save.

This does not provide durable staging, authorize a user, or publish files. The
configured core executable is trusted, and its report is not self-authenticating.
"""

from __future__ import annotations

import hmac
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from corpora_api.corpus_archive_inventory import CorpusArchiveInventory, inventory_corpus_archive


class CorpusPreparationError(ValueError):
    """The claimed bytes or real-loader validation cannot be accepted."""


class CorpusValidator(Protocol):
    def validate(self, filename: str, archive: bytes) -> dict[str, Any]: ...


@dataclass(frozen=True)
class PreparedCorpusArchive:
    filename: str
    archive: bytes
    inventory: CorpusArchiveInventory


def prepare_corpus_archive(
    filename: str,
    archive: bytes,
    expected_sha256: str,
    validator: CorpusValidator,
    *,
    expected_format: str,
) -> PreparedCorpusArchive:
    """Inventory and real-loader validate the exact immutable claimed bytes.

    Validator errors propagate. A receipt must not be used as durable staging
    or as proof that an external Hub commit contains these files.
    """
    try:
        filename_length = len(filename.encode("utf-8")) if isinstance(filename, str) else 0
    except UnicodeError as error:
        raise CorpusPreparationError("Unsupported corpus archive filename") from error
    if (
        not isinstance(filename, str)
        or not filename
        or filename_length > 255
        or "/" in filename
        or "\\" in filename
        or any(ord(c) < 32 or ord(c) == 127 for c in filename)
        or Path(filename).suffix.lower() not in (".corpus", ".zip")
    ):
        raise CorpusPreparationError("Unsupported corpus archive filename")
    if expected_format not in ("zip", "corpus") or (
        Path(filename).suffix.lower() != "." + expected_format
    ):
        raise CorpusPreparationError("Corpus archive format does not match its claim")
    if not isinstance(expected_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise CorpusPreparationError("Invalid claimed archive digest")
    if type(archive) is not bytes:
        raise CorpusPreparationError("Corpus input must be immutable bytes")
    inventory = inventory_corpus_archive(archive)
    if not hmac.compare_digest(inventory.archive_sha256, expected_sha256):
        raise CorpusPreparationError("Corpus archive does not match its claimed digest")
    report = validator.validate(filename, archive)
    if (
        not isinstance(report, dict)
        or report.get("path") != filename
        or report.get("valid") is not True
    ):
        raise CorpusPreparationError("Corpus failed real-loader validation")
    checks = report.get("checks")
    required = {
        "text_fabric_load",
        "context_fabric_load",
        "cfm_mmap_reload",
        "stats_match",
        "samples_match",
    }
    if Path(filename).suffix.lower() == ".corpus":
        required |= {"shipped_cfm_load", "shipped_cfm_match"}
    if (
        not isinstance(checks, dict)
        or any(checks.get(key) is not True for key in required)
        or any(value is not True and value is not None for value in checks.values())
    ):
        raise CorpusPreparationError("Corpus real-loader checks are incomplete")
    return PreparedCorpusArchive(filename, archive, inventory)
