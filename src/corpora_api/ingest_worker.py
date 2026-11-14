"""Optional out-of-process Docling bridge for non-text documents."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from corpora_schema_types import CanonicalGraph, GraphValidationError, validate_graph

from corpora_api.ingest import MAX_GRAPH_BYTES

DOCLING_SUFFIXES = frozenset({".pdf", ".docx", ".pptx", ".html", ".md"})
WORKER_TIMEOUT_SECONDS = 90


class IngestWorkerUnavailable(Exception):
    """The optional Docling worker is not installed or configured."""


class IngestWorkerFailed(Exception):
    """The optional worker rejected the document."""


class IngestWorkerProtocolError(Exception):
    """The optional worker returned an invalid graph."""


class IngestWorkerTimedOut(Exception):
    """The optional worker exceeded the bounded processing time."""


def run_docling_worker(
    filename: str, source: bytes, *, name: str, language: str, corpus_id: str
) -> CanonicalGraph:
    """Stage bytes under a server-generated path and validate the worker output."""
    python = os.getenv("CORPORA_INGEST_WORKER_PYTHON")
    if not python:
        raise IngestWorkerUnavailable("Docling worker is not configured")
    if Path(filename).suffix.lower() not in DOCLING_SUFFIXES:
        raise IngestWorkerFailed("Unsupported Docling document format")
    with tempfile.TemporaryDirectory(prefix="corpora-api-ingest-") as temporary:
        source_path = Path(temporary) / filename
        result_path = Path(temporary) / "graph.json"
        source_path.write_bytes(source)
        command = [
            python,
            "-m",
            "corpora_api.ingest_worker",
            "--source",
            str(source_path),
            "--result",
            str(result_path),
            "--filename",
            filename,
            "--name",
            name,
            "--language",
            language,
            "--corpus-id",
            corpus_id,
        ]
        try:
            result = subprocess.run(
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=WORKER_TIMEOUT_SECONDS,
                check=False,
            )
        except OSError as error:
            raise IngestWorkerUnavailable("Docling worker could not start") from error
        except subprocess.TimeoutExpired as error:
            raise IngestWorkerTimedOut("Docling worker timed out") from error
        if result.returncode == 3:
            raise IngestWorkerUnavailable("Docling worker dependencies are unavailable")
        if result.returncode == 4:
            raise IngestWorkerProtocolError("Docling worker returned an invalid graph")
        if result.returncode != 0:
            raise IngestWorkerFailed("Docling could not ingest this document")
        try:
            if result_path.stat().st_size > MAX_GRAPH_BYTES:
                raise IngestWorkerProtocolError("Docling graph exceeds hosted response limit")
            payload = json.loads(result_path.read_bytes())
            return validate_graph(payload)
        except (OSError, ValueError) as error:
            raise IngestWorkerProtocolError("Docling worker returned an invalid graph") from error


def _worker_main(args: argparse.Namespace) -> int:
    try:
        from admin.ingest import DoclingNotInstalledError, parse_file
    except ImportError:
        return 3
    try:
        graph: Any = parse_file(
            Path(args.source),
            language=args.language,
            corpus_id=args.corpus_id or None,
            title=args.name or None,
        )
        parsed = validate_graph(graph.to_dict())
        Path(args.result).write_text(
            json.dumps(parsed.to_wire(), ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
    except DoclingNotInstalledError:
        return 3
    except GraphValidationError:
        return 4
    except Exception:
        return 2
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Optional Docling graph worker")
    parser.add_argument("--source", required=True)
    parser.add_argument("--result", required=True)
    parser.add_argument("--filename", required=True)
    parser.add_argument("--name", default="")
    parser.add_argument("--language", default="und")
    parser.add_argument("--corpus-id", default="")
    sys.exit(_worker_main(parser.parse_args()))
