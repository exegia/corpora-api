"""Run Context-Fabric operations in an isolated, bounded child process."""

from __future__ import annotations

import base64
import json
import subprocess
import sys
from dataclasses import asdict
from typing import Any

# Leave room under the 4 MB hosted response limit for route-level metadata.
MAX_CORPUS_WORKER_RESPONSE_BYTES = 3_900_000


def _bounded_response(value: dict[str, Any]) -> str:
    """Encode before writing so oversized output never crosses the worker pipe."""
    chunks: list[str] = []
    size = 0
    for chunk in json.JSONEncoder(default=str, ensure_ascii=False).iterencode(value):
        size += len(chunk.encode("utf-8"))
        if size > MAX_CORPUS_WORKER_RESPONSE_BYTES:
            return json.dumps({"error": "too_large", "message": "Corpus result exceeds limit"})
        chunks.append(chunk)
    return "".join(chunks)


class CorpusProcessError(Exception):
    def __init__(self, kind: str, message: str, attributes: dict[str, Any] | None = None):
        super().__init__(message)
        self.kind = kind
        self.attributes = attributes or {}


def run_corpus(data: bytes, operation: str, **kwargs: Any) -> dict[str, Any]:
    """Keep native loader state out of the long-lived multithreaded server."""
    payload = json.dumps(
        {"data": base64.b64encode(data).decode("ascii"), "operation": operation, "args": kwargs}
    )
    try:
        result = subprocess.run(
            [sys.executable, "-m", "corpora_api.corpus_process"],
            input=payload,
            text=True,
            capture_output=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise CorpusProcessError("unavailable", "Corpus processing is unavailable") from error
    if result.returncode != 0:
        raise CorpusProcessError("unavailable", "Corpus processing failed")
    try:
        value = json.loads(result.stdout)
        if not isinstance(value, dict):
            raise ValueError("invalid worker response")
        if "error" in value:
            raise CorpusProcessError(
                value["error"], value.get("message", ""), value.get("attributes")
            )
        if not isinstance(value.get("result"), dict):
            raise ValueError("invalid worker result")
        return value["result"]
    except (ValueError, KeyError, TypeError) as error:
        raise CorpusProcessError("unavailable", "Corpus worker returned invalid data") from error


def _main() -> None:
    from corpus_validate import ArchiveReadNotFound, ArchiveTooLarge, read_archive, tfref
    from corpus_validate.ai_archive import StaleScope, validate_archive_scope
    from corpus_validate.ai_types import NodeScope, ScopeLevel, UnitRange
    from corpus_validate.reference import reference_archive

    try:
        request = json.load(sys.stdin)
        data = base64.b64decode(request["data"], validate=True)
        operation, args = request["operation"], request["args"]
        if operation == "view":
            value = read_archive(data, **args)
        elif operation == "reference":
            value = reference_archive(data, **args)
        elif operation == "scope":
            raw = args["scope"]
            raw["level"] = ScopeLevel(raw["level"])
            if raw.get("unit_range") is not None:
                raw["unit_range"] = UnitRange(**raw["unit_range"])
            value = asdict(validate_archive_scope(data, NodeScope(**raw)))
        else:
            raise ValueError("Unsupported corpus operation")
        response = {"result": value}
    except StaleScope as error:
        response = {
            "error": "stale",
            "message": str(error),
            "attributes": {"current_version": error.current_version},
        }
    except tfref.VersionMismatch as error:
        response = {
            "error": "version",
            "message": str(error),
            "attributes": {"wanted": error.wanted, "loaded": error.loaded},
        }
    except tfref.ParseError as error:
        response = {"error": "parse", "message": str(error)}
    except (
        ArchiveReadNotFound,
        tfref.SectionNotFound,
        tfref.TypeNotInSection,
        tfref.IndexOutOfRange,
    ) as error:
        response = {"error": "not_found", "message": str(error)}
    except ArchiveTooLarge as error:
        response = {"error": "too_large", "message": str(error)}
    except Exception:
        response = {"error": "invalid", "message": "Corpus archive or selection is invalid"}
    print(_bounded_response(response))


if __name__ == "__main__":
    _main()
