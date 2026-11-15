"""Unwired, time-bounded download of a parent-validated registry revision."""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import math
import os
import signal
import subprocess
import sys
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from corpora_api.storage import StoredCorpusNotFound, archive_name
from corpora_api.storage_detail import MAX_STORED_ARCHIVE_BYTES

MAX_WORKER_RESPONSE = 4_200_000
MAX_READ_SECONDS = 30.0


class RevisionFact(BaseModel):
    model_config = ConfigDict(extra="forbid")
    owner: UUID
    filename: str
    revision: UUID
    digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    version: str = Field(min_length=1, max_length=128)


@dataclass(frozen=True)
class VerifiedSnapshot:
    fact: RevisionFact
    data: bytes


class RevisionRegistry(Protocol):
    def _rpc(self, name: str, payload: dict[str, object]) -> object: ...
    def _config(self) -> tuple[str, dict[str, str]]: ...


def _identity(owner: str, filename: str, revision: str | None) -> tuple[str, str, str | None]:
    try:
        owner = str(UUID(owner))
        if archive_name(filename) != filename or len(filename.encode("utf-8")) > 255:
            raise ValueError("invalid archive name")
        revision = str(UUID(revision)) if revision is not None else None
        return owner, filename, revision
    except (ValueError, TypeError, AttributeError, StoredCorpusNotFound) as error:
        raise HTTPException(404, "Stored revision not found") from error


def _child_environment() -> dict[str, str]:
    # No application secrets or bearer tokens are inherited through the env.
    # The trusted revision adapter receives only its required URL/key via stdin.
    names = ("PATH", "LANG", "LC_ALL", "SSL_CERT_FILE", "SSL_CERT_DIR", "PYTHONPATH", "SYSTEMROOT")
    return {name: os.environ[name] for name in names if name in os.environ}


class TimedRevisionReader:
    """Resolve owner-bound facts in the parent; kill and reap a timed-out reader."""

    def __init__(self, registry: RevisionRegistry, timeout: float = MAX_READ_SECONDS) -> None:
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or not 0 < timeout <= MAX_READ_SECONDS
        ):
            raise ValueError("invalid revision read timeout")
        self.registry, self.timeout = registry, float(timeout)

    def get(self, owner: str, filename: str, revision: str | None = None) -> VerifiedSnapshot:
        owner, filename, revision = _identity(owner, filename, revision)
        value = self.registry._rpc(
            "corpora_stored_get", {"p_owner": owner, "p_filename": filename, "p_revision": revision}
        )
        if value is None:
            raise HTTPException(404, "Stored revision not found")
        try:
            fact = RevisionFact.model_validate(value)
            if (
                str(fact.owner) != owner
                or fact.filename != filename
                or (revision is not None and str(fact.revision) != revision)
            ):
                raise ValueError("invalid registry identity")
            url, headers = self.registry._config()
            key = headers["apikey"]
            if not isinstance(url, str) or not url or not isinstance(key, str) or not key:
                raise ValueError("invalid Storage configuration")
        except (ValueError, TypeError, KeyError, ValidationError) as error:
            raise HTTPException(503, "Stored revision registry returned invalid data") from error
        payload = json.dumps({"fact": fact.model_dump(mode="json"), "url": url, "key": key})
        try:
            with subprocess.Popen(
                [sys.executable, "-m", "corpora_api.timed_revision_read"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                env=_child_environment(),
                start_new_session=(os.name == "posix"),
            ) as process:
                try:
                    stdout, _ = process.communicate(payload, timeout=self.timeout)
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
                        # A descendant outside the group might hold the pipe.
                        # Stop waiting on it after the worker itself is reaped.
                        if process.stdout is not None:
                            process.stdout.close()
                        process.kill()
                        process.wait(timeout=2)
                    raise HTTPException(504, "Stored revision read timed out") from error
                returncode = process.returncode
        except HTTPException:
            raise
        except OSError as error:
            raise HTTPException(503, "Stored revision reader is unavailable") from error
        try:
            if returncode != 0 or len(stdout.encode("utf-8")) > MAX_WORKER_RESPONSE:
                raise ValueError("invalid worker response")
            result = json.loads(stdout)
            if not isinstance(result, dict):
                raise ValueError("invalid worker response")
            if "error" in result:
                status = result["error"] if result["error"] in (404, 409, 413, 503) else 503
                raise HTTPException(status, "Stored revision is unavailable")
            if set(result) != {"archive"}:
                raise ValueError("invalid worker response")
            data = base64.b64decode(result["archive"], validate=True)
            if (
                len(data) > MAX_STORED_ARCHIVE_BYTES
                or "sha256:" + hashlib.sha256(data).hexdigest() != fact.digest
            ):
                raise ValueError("invalid snapshot digest")
            return VerifiedSnapshot(fact, data)
        except HTTPException:
            raise
        except (ValueError, TypeError, KeyError) as error:
            raise HTTPException(503, "Stored revision reader returned invalid data") from error


def _main() -> None:
    try:
        request = json.load(sys.stdin)
        fact = RevisionFact.model_validate(request["fact"])
        _identity(str(fact.owner), fact.filename, str(fact.revision))
        url, key = request["url"], request["key"]
        if not isinstance(url, str) or not url or not isinstance(key, str) or not key:
            raise ValueError("invalid Storage configuration")
        os.environ["SUPABASE_URL"] = url
        os.environ["SUPABASE_SERVICE_ROLE_KEY"] = key
        with contextlib.redirect_stdout(sys.stderr):
            from corpora_api.stored_revisions import Revision, StoredRevisions

            data = StoredRevisions()._download(
                Revision.model_validate(fact.model_dump(mode="json"))
            )
        encoded = json.dumps({"archive": base64.b64encode(data).decode("ascii")})
        if len(encoded.encode("utf-8")) > MAX_WORKER_RESPONSE:
            raise HTTPException(413, "Stored revision exceeds size limit")
    except HTTPException as error:
        encoded = json.dumps(
            {"error": error.status_code if error.status_code in (404, 409, 413, 503) else 503}
        )
    except Exception:
        encoded = json.dumps({"error": 503})
    print(encoded)


if __name__ == "__main__":
    _main()
