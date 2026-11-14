"""Bounded process adapter for the core corpus archive validator."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

MAX_VALIDATION_ARCHIVE_BYTES = 3_000_000
VALIDATION_TIMEOUT_SECONDS = 90
SUPPORTED_SUFFIXES = frozenset({".corpus", ".zip", ".ctei", ".cusx"})


class ValidationUnavailable(Exception):
    """The installed core validator is unavailable."""


class ValidationTimedOut(Exception):
    """The real loader did not finish within the hosted time bound."""


class ValidationTooLarge(Exception):
    """An upload exceeds the hosted size bound."""


class ValidationUnsupported(Exception):
    """The uploaded file format is not a corpus archive."""


class ValidationProtocolError(Exception):
    """The core process returned an invalid or unexpected report."""


class ValidationBridge:
    def __init__(self, executable: str | None = None) -> None:
        self.executable = executable or os.getenv("CORPORA_VALIDATE_BIN", "corpora-validate")

    def validate(self, filename: str, archive: bytes) -> dict[str, Any]:
        if (
            not filename
            or len(filename.encode("utf-8")) > 255
            or "/" in filename
            or "\\" in filename
            or any(ord(c) < 32 for c in filename)
        ):
            raise ValidationUnsupported("Upload a corpus archive with a plain filename")
        if Path(filename).suffix.lower() not in SUPPORTED_SUFFIXES:
            raise ValidationUnsupported("Unsupported corpus archive format")
        if len(archive) > MAX_VALIDATION_ARCHIVE_BYTES:
            raise ValidationTooLarge(f"Archive exceeds {MAX_VALIDATION_ARCHIVE_BYTES} bytes")

        try:
            with tempfile.TemporaryDirectory(prefix="corpora-api-validate-") as temporary:
                path = Path(temporary) / ("upload" + Path(filename).suffix.lower())
                path.write_bytes(archive)
                result = subprocess.run(
                    [self.executable, str(path), "--name", filename],
                    capture_output=True,
                    text=True,
                    timeout=VALIDATION_TIMEOUT_SECONDS,
                    check=False,
                )
        except OSError as error:
            raise ValidationUnavailable(
                "corpora-validate executable or staging area is unavailable"
            ) from error
        except subprocess.TimeoutExpired as error:
            raise ValidationTimedOut("Corpus validation timed out") from error

        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise ValidationProtocolError("Core validator returned invalid JSON") from error
        if not isinstance(payload, dict):
            raise ValidationProtocolError("Core validator returned a non-object response")
        if result.returncode == 2:
            message = str(payload.get("error", "Core rejected the archive"))
            code = payload.get("code")
            if code == "unsupported_format":
                raise ValidationUnsupported(message)
            if code == "archive_too_large":
                raise ValidationTooLarge(message)
            raise ValidationProtocolError("Core validator rejected a staged upload")
        if result.returncode != 0 or payload.get("path") != filename:
            raise ValidationProtocolError("Core validator response did not match the upload")
        return payload
