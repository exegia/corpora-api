"""Bound a draft view operation before it is used under a session lease.

This helper is not wired to routes. Its caller must supply a verified owner and
hold the session lease until the result has been checked.
"""

from __future__ import annotations

import json
import math
import os
import signal
import subprocess
import sys
from typing import Any
from uuid import UUID

from fastapi import HTTPException

from corpora_api.drafts import MAX_DRAFT_VIEW_RESPONSE_BYTES, DraftStore

MAX_READ_SECONDS = 30.0
_ENV_NAMES = ("PATH", "LANG", "LC_ALL", "SSL_CERT_FILE", "SSL_CERT_DIR", "PYTHONPATH", "SYSTEMROOT")
_ERROR_STATUSES = frozenset({404, 409, 413, 422, 423, 503})


def read_draft_timed(
    store: DraftStore,
    owner: str,
    draft_id: str,
    view: str,
    params: dict[str, Any],
    *,
    timeout: float = MAX_READ_SECONDS,
) -> dict[str, Any]:
    """Read a draft in a killable process, with credentials passed on stdin."""
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(timeout)
        or not 0 < timeout <= MAX_READ_SECONDS
    ):
        raise ValueError("Invalid draft read timeout")
    try:
        owner, draft_id = str(UUID(owner)), str(UUID(draft_id))
    except (ValueError, TypeError, AttributeError) as error:
        raise HTTPException(404, "Working draft not found") from error
    if view not in {"manifest", "index", "versions", "node", "sections", "content"}:
        raise ValueError("Unsupported draft view")
    url, headers = store._config()
    payload = json.dumps(
        {
            "url": url,
            "key": headers["apikey"],
            "owner": owner,
            "draft": draft_id,
            "view": view,
            "params": params,
        }
    )
    env = {name: os.environ[name] for name in _ENV_NAMES if name in os.environ}
    try:
        with subprocess.Popen(
            [sys.executable, "-m", "corpora_api.timed_draft_read"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            env=env,
            start_new_session=(os.name == "posix"),
        ) as process:
            try:
                stdout, _ = process.communicate(payload, timeout=timeout)
            except subprocess.TimeoutExpired as error:
                try:
                    if os.name == "posix":
                        os.killpg(process.pid, signal.SIGKILL)
                    else:
                        process.kill()
                except ProcessLookupError:
                    pass
                try:
                    process.communicate(timeout=2)
                except subprocess.TimeoutExpired:
                    if process.stdout is not None:
                        process.stdout.close()
                    process.kill()
                    process.wait(timeout=2)
                raise HTTPException(504, "Working draft read timed out") from error
            returncode = process.returncode
    except HTTPException:
        raise
    except OSError as error:
        raise HTTPException(503, "Working draft reader is unavailable") from error
    try:
        if returncode != 0 or len(stdout.encode("utf-8")) > MAX_DRAFT_VIEW_RESPONSE_BYTES + 1024:
            raise ValueError("Invalid worker response")
        result = json.loads(stdout)
        if not isinstance(result, dict):
            raise ValueError("Invalid worker response")
        if set(result) == {"error"} and type(result["error"]) is int:
            status = result["error"] if result["error"] in _ERROR_STATUSES else 503
            raise HTTPException(status, "Working draft view is unavailable")
        if set(result) != {"draft", "data"}:
            raise ValueError("Invalid worker response")
        if not isinstance(result["draft"], dict) or not isinstance(result["data"], dict):
            raise ValueError("Invalid worker response")
        if result["draft"].get("id") != draft_id:
            raise ValueError("Invalid draft identity")
        return result
    except HTTPException:
        raise
    except (ValueError, TypeError) as error:
        raise HTTPException(503, "Working draft reader returned invalid data") from error


def _main() -> None:
    try:
        request = json.load(sys.stdin)
        url, key = request["url"], request["key"]
        if not isinstance(url, str) or not url or not isinstance(key, str) or not key:
            raise ValueError("Invalid Storage configuration")

        class ConfiguredDraftStore(DraftStore):
            def _config(self) -> tuple[str, dict[str, str]]:
                return url, {"Authorization": f"Bearer {key}", "apikey": key}

        result = ConfiguredDraftStore().read(
            request["owner"], request["draft"], request["view"], **request["params"]
        )
        encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > MAX_DRAFT_VIEW_RESPONSE_BYTES:
            raise HTTPException(413, "Working draft view exceeds response limit")
    except HTTPException as error:
        status = error.status_code if error.status_code in _ERROR_STATUSES else 503
        encoded = json.dumps({"error": status})
    except Exception:
        encoded = json.dumps({"error": 503})
    print(encoded)


if __name__ == "__main__":
    _main()
