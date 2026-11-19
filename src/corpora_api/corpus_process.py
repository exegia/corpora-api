"""Run Context-Fabric operations in an isolated, bounded child process."""

from __future__ import annotations

import base64
import json
import re
import subprocess
import sys
from dataclasses import asdict
from typing import Any

from corpora_api.project_document_errors import DocumentVerificationUnavailable

# Leave room under the 4 MB hosted response limit for route-level metadata.
MAX_CORPUS_WORKER_RESPONSE_BYTES = 3_900_000
MAX_CORPUS_EDIT_WORKER_RESPONSE_BYTES = 4_100_000


def _bounded_response(value: dict[str, Any], max_bytes: int | None = None) -> str:
    """Encode before writing so oversized output never crosses the worker pipe."""
    if max_bytes is None:
        max_bytes = MAX_CORPUS_WORKER_RESPONSE_BYTES
    chunks: list[str] = []
    size = 0
    for chunk in json.JSONEncoder(default=str, ensure_ascii=False).iterencode(value):
        size += len(chunk.encode("utf-8"))
        if size > max_bytes:
            return json.dumps({"error": "too_large", "message": "Corpus result exceeds limit"})
        chunks.append(chunk)
    return "".join(chunks)


class CorpusProcessError(Exception):
    def __init__(self, kind: str, message: str, attributes: dict[str, Any] | None = None):
        super().__init__(message)
        self.kind = kind
        self.attributes = attributes or {}


class UnknownScopeNode(Exception):
    def __init__(self, node: int):
        self.node = node


class InvalidScopeSelection(Exception):
    def __init__(self, detail: str):
        self.detail = detail


class ScopeNodeTypeMismatch(Exception):
    def __init__(self, actual: str):
        self.actual = actual


SAFE_SCOPE_ERRORS = frozenset(
    {
        "Corpus scope cannot carry a node, node type, or unit range",
        "Word scope requires a slot node",
        "A structural scope requires a non-slot node",
        "Section scope requires a declared section type",
        "Document scope requires the outermost declared section type",
        "Only passage scope accepts a unit range",
        "Passage endpoints must be structural nodes of the same type",
        "Passage has ambiguous enclosing sections",
        "Passage range requires an enclosing declared section",
        "Passage range crosses a section boundary",
        "Passage endpoints must be contained in their section",
        "Passage endpoints are reversed in document order",
        "Passage anchor must contain the range or be its first unit",
    }
)


def safe_scope_node_type(value: object) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r"[\w.-]{1,128}", value))


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
    from corpus_validate import (
        ArchiveReadNotFound,
        ArchiveTooLarge,
        edit_archive,
        read_archive,
        tfref,
    )
    from corpus_validate.ai_archive import StaleScope, validate_archive_scope
    from corpus_validate.ai_types import NodeScope, ScopeLevel, UnitRange
    from corpus_validate.reference import reference_archive

    max_response = MAX_CORPUS_WORKER_RESPONSE_BYTES
    try:
        request = json.load(sys.stdin)
        data = base64.b64decode(request["data"], validate=True)
        operation, args = request["operation"], request["args"]
        if operation == "edit":
            edited = edit_archive(data, **args)
            value = {"archive": base64.b64encode(edited).decode("ascii")}
            max_response = MAX_CORPUS_EDIT_WORKER_RESPONSE_BYTES
        elif operation == "view":
            value = read_archive(data, **args)
        elif operation == "reference":
            value = reference_archive(data, **args)
        elif operation == "verify_epub":
            from corpora_api.project_epub_verify import verify_epub_in_corpus

            value = verify_epub_in_corpus(data, **args)
        elif operation == "verify_pdf":
            from corpora_api.project_pdf_verify import verify_pdf_in_corpus

            value = verify_pdf_in_corpus(data, **args)
        elif operation == "verify_cusx":
            from corpora_api.project_cusx_verify import verify_cusx_in_corpus

            value = verify_cusx_in_corpus(data, **args)
        elif operation == "retrieve_epub":
            from corpora_api.project_epub_verify import retrieve_epub_in_corpus

            value = retrieve_epub_in_corpus(data, **args)
        elif operation == "retrieve_pdf":
            from corpora_api.project_pdf_verify import retrieve_pdf_in_corpus

            value = retrieve_pdf_in_corpus(data, **args)
        elif operation == "retrieve_cusx":
            from corpora_api.project_cusx_verify import retrieve_cusx_in_corpus

            value = retrieve_cusx_in_corpus(data, **args)
        elif operation == "scope":
            raw = args["scope"]
            raw["level"] = ScopeLevel(raw["level"])
            if raw.get("unit_range") is not None:
                raw["unit_range"] = UnitRange(**raw["unit_range"])
            scope = NodeScope(**raw)
            try:
                value = asdict(validate_archive_scope(data, scope))
            except ValueError as error:
                nodes = [scope.node_id]
                if scope.unit_range is not None:
                    nodes.extend([scope.unit_range.start, scope.unit_range.end])
                for node in nodes:
                    if node is not None and str(error) == f"Unknown node {node}":
                        raise UnknownScopeNode(node) from error
                detail = str(error)
                if detail in SAFE_SCOPE_ERRORS:
                    raise InvalidScopeSelection(detail) from error
                if scope.node_id is not None and scope.node_type is not None:
                    prefix = f"Node {scope.node_id} has type '"
                    suffix = f"', not {scope.node_type!r}"
                    if detail.startswith(prefix) and detail.endswith(suffix):
                        actual = detail[len(prefix) : -len(suffix)]
                        if safe_scope_node_type(actual):
                            raise ScopeNodeTypeMismatch(actual) from error
                raise
        else:
            raise ValueError("Unsupported corpus operation")
        response = {"result": value}
    except DocumentVerificationUnavailable:
        response = {"error": "unavailable", "message": "Document verification is unavailable"}
    except UnknownScopeNode as error:
        response = {"error": "unknown_scope_node", "attributes": {"node": error.node}}
    except InvalidScopeSelection as error:
        response = {"error": "invalid_scope", "attributes": {"detail": error.detail}}
    except ScopeNodeTypeMismatch as error:
        response = {"error": "scope_type_mismatch", "attributes": {"actual": error.actual}}
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
    print(_bounded_response(response, max_response))


if __name__ == "__main__":
    _main()
