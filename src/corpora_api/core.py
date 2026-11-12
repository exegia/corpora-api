"""Bounded JSON-lines client for corpora-core's supported process interface."""

import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Any


class CoreUnavailable(Exception):
    """The configured corpora-core executable or dictionary data is unavailable."""


class CoreRejected(Exception):
    """corpora-core rejected an input."""


class CoreProtocolError(Exception):
    """The core executable broke its JSON-lines response contract."""


class CoreBridge:
    def __init__(self, executable: str | None = None, dictionaries: str | None = None) -> None:
        self.executable = Path(
            executable or os.getenv("CORPORA_TEXT_CHECK_BIN", "corpora-text-check")
        )
        self.dictionaries = dictionaries or os.getenv("CORPORA_DICTIONARIES_DIR")

    def run(self, request: dict[str, Any], *, needs_dictionaries: bool = False) -> dict[str, Any]:
        if needs_dictionaries and not self.dictionaries:
            raise CoreUnavailable("CORPORA_DICTIONARIES_DIR is required for spelling checks")
        if self.dictionaries and not Path(self.dictionaries).is_dir():
            raise CoreUnavailable("Configured dictionary directory does not exist")

        command = [str(self.executable)]
        if self.dictionaries:
            command.append(self.dictionaries)
        try:
            result = subprocess.run(
                command,
                input=json.dumps(request, ensure_ascii=False) + "\n",
                text=True,
                capture_output=True,
                timeout=30 if needs_dictionaries else 10,
                check=False,
            )
        except FileNotFoundError as exc:
            raise CoreUnavailable("corpora-text-check executable was not found") from exc
        except subprocess.TimeoutExpired as exc:
            raise CoreUnavailable("corpora-text-check timed out") from exc

        if result.returncode != 0:
            raise CoreProtocolError("corpora-text-check exited without a valid response")
        lines = result.stdout.splitlines()
        if len(lines) != 1:
            raise CoreProtocolError("corpora-text-check returned an unexpected number of lines")
        try:
            payload = json.loads(lines[0])
        except json.JSONDecodeError as exc:
            raise CoreProtocolError("corpora-text-check returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise CoreProtocolError("corpora-text-check returned a non-object response")
        if "error" in payload:
            raise CoreRejected(str(payload["error"]))
        if request.get("operation") in {"structure", "purpose"}:
            expected = hashlib.sha256(request["text"].encode("utf-8")).hexdigest()
            if payload.get("source_sha256") != expected or payload.get("schema_version") != 1:
                raise CoreProtocolError("corpora-text-check returned a stale or unsupported report")
        return payload
