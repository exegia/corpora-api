"""Run proposed core archive version transformations in an isolated process."""

from __future__ import annotations

import base64
import contextlib
import json
import subprocess
import sys
from typing import Any

from corpora_api.storage_detail import MAX_STORED_ARCHIVE_BYTES

# One base64-encoded archive plus bounded transform metadata.
MAX_WORKER_RESPONSE = 4_200_000
MAX_REQUEST_METADATA = 100_000


class VersionProcessError(Exception):
    def __init__(self, kind: str):
        self.kind = kind
        super().__init__(kind)


def run_version(
    current: bytes, selected: bytes | None, worker_operation: str, **args: Any
) -> dict[str, Any]:
    if len(current) > MAX_STORED_ARCHIVE_BYTES or (
        selected is not None and len(selected) > MAX_STORED_ARCHIVE_BYTES
    ):
        raise VersionProcessError("too_large")
    try:
        metadata = json.dumps(args, ensure_ascii=False)
        if len(metadata.encode("utf-8")) > MAX_REQUEST_METADATA:
            raise VersionProcessError("too_large")
        payload = json.dumps(
            {
                "current": base64.b64encode(current).decode("ascii"),
                "selected": base64.b64encode(selected).decode("ascii")
                if selected is not None
                else None,
                "operation": worker_operation,
                "args": args,
            }
        )
        result = subprocess.run(
            [sys.executable, "-m", "corpora_api.version_process"],
            input=payload,
            text=True,
            capture_output=True,
            timeout=60,
            check=False,
        )
        if result.returncode != 0 or len(result.stdout.encode("utf-8")) > MAX_WORKER_RESPONSE:
            raise VersionProcessError("unavailable")
        response = json.loads(result.stdout)
        if not isinstance(response, dict):
            raise ValueError("invalid worker response")
        if isinstance(response.get("error"), str):
            raise VersionProcessError(response["error"])
        archive = base64.b64decode(response["archive"], validate=True)
        if len(archive) > MAX_STORED_ARCHIVE_BYTES:
            raise VersionProcessError("too_large")
        if (
            not isinstance(response.get("version"), str)
            or not isinstance(response.get("digest"), str)
            or not isinstance(response.get("result"), dict)
        ):
            raise ValueError("invalid worker response")
        return {**response, "archive": archive}
    except VersionProcessError:
        raise
    except (OSError, subprocess.TimeoutExpired, ValueError, TypeError, KeyError) as error:
        raise VersionProcessError("unavailable") from error


def _main() -> None:
    try:
        request = json.load(sys.stdin)
        current = base64.b64decode(request["current"], validate=True)
        selected = request["selected"]
        if len(current) > MAX_STORED_ARCHIVE_BYTES:
            raise VersionProcessError("too_large")
        with contextlib.redirect_stdout(sys.stderr):
            from corpus_validate import ArchiveTooLarge
            from corpus_validate.version_edit import prepare_edit, prepare_restore

            try:
                if request["operation"] == "edit" and selected is None:
                    prepared = prepare_edit(current, **request["args"])
                elif request["operation"] == "restore" and isinstance(selected, str):
                    selected_data = base64.b64decode(selected, validate=True)
                    if len(selected_data) > MAX_STORED_ARCHIVE_BYTES:
                        raise VersionProcessError("too_large")
                    prepared = prepare_restore(current, selected_data, **request["args"])
                else:
                    raise ValueError("invalid operation")
            except ArchiveTooLarge as error:
                raise VersionProcessError("too_large") from error
        response = {
            "archive": base64.b64encode(prepared.archive).decode("ascii"),
            "version": prepared.version,
            "digest": prepared.digest,
            "result": prepared.result,
        }
        encoded = json.dumps(response, ensure_ascii=False)
        if len(encoded.encode("utf-8")) > MAX_WORKER_RESPONSE:
            raise VersionProcessError("too_large")
    except VersionProcessError as error:
        encoded = json.dumps({"error": error.kind})
    except ModuleNotFoundError:
        encoded = json.dumps({"error": "unavailable"})
    except Exception:
        encoded = json.dumps({"error": "invalid"})
    print(encoded)


if __name__ == "__main__":
    _main()
