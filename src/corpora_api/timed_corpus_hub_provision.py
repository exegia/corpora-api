"""Unwired total-time limit for metadata lookup and private Hub creation.

Killing the worker cannot retract an HTTP write already accepted by the Hub.
Timeouts and invalid worker output therefore require reconciliation. A lost
database connection can release a session lock while the worker is still
running; this draft does not fence that case and must not be exposed to users.
"""

from __future__ import annotations

import json
import math
import os
import re
import selectors
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping
from typing import Any, Protocol
from urllib.parse import urlsplit
from uuid import UUID

from corpora_api.corpus_hub_provision import (
    CorpusHubProvisionConflict,
    CorpusHubProvisioner,
    CorpusHubProvisionError,
    CorpusHubProvisionUncertain,
    VerifiedPrivateRepository,
)
from corpora_api.corpus_metadata import (
    CorpusMetadataError,
    CorpusMetadataNotFound,
    CorpusMetadataStore,
)

MAX_PROVISION_SECONDS = 30.0
MAX_RESPONSE_BYTES = 8192
MAX_REQUEST_BYTES = 8192
MAX_IN_FLIGHT = 8
_PROVISION_SLOTS = threading.BoundedSemaphore(MAX_IN_FLIGHT)
_ENV_NAMES = ("PATH", "LANG", "LC_ALL", "SSL_CERT_FILE", "SSL_CERT_DIR", "PYTHONPATH", "SYSTEMROOT")


def _retain_unconfirmed_lease() -> None:
    """Do not release a lease after process-group termination cannot be confirmed."""
    threading.Event().wait()


def _stop_and_reap(process: subprocess.Popen[bytes]) -> None:
    """Wait for a signalled worker before releasing a still-live session lease."""
    try:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            # The group is absent. A reaped leader confirms there is no
            # remaining worker in that group; avoid retaining a dead lease.
            try:
                if process.poll() is not None:
                    return
            except OSError:
                pass
            try:
                process.kill()
            except OSError:
                pass
            _retain_unconfirmed_lease()
        except OSError:
            try:
                process.kill()
            except OSError:
                pass
            # The direct worker may have descendants. Without a confirmed
            # group signal, their termination cannot be established here.
            _retain_unconfirmed_lease()
        # If the OS cannot stop the process, the lease must remain held. The
        # request-facing thread can still time out while this thread waits.
        process.wait()
    except OSError:
        _retain_unconfirmed_lease()
    finally:
        for stream in (process.stdin, process.stdout):
            if stream is not None:
                try:
                    stream.close()
                except (OSError, ValueError):
                    pass


def _timeout(value: float) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not 0 < value <= MAX_PROVISION_SECONDS
    ):
        raise ValueError("Invalid repository provisioning timeout")
    return float(value)


def _validate_registry_url(value: str) -> None:
    try:
        parsed = urlsplit(value)
        if (
            parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Invalid registry URL")
        local = parsed.hostname in {"127.0.0.1", "::1"}
        if not parsed.hostname or (
            parsed.scheme != "https" and not (parsed.scheme == "http" and local)
        ):
            raise ValueError("Invalid registry URL")
        if parsed.port is not None and not 1 <= parsed.port <= 65535:
            raise ValueError("Invalid registry URL")
    except (ValueError, TypeError) as error:
        raise CorpusHubProvisionError("Corpus metadata connection is not secure") from error


class SessionLease(Protocol):
    """A trusted lease that validates verified claims before and after the callback."""

    def read(self, claims: Mapping[str, Any], operation: Callable[[str], bytes]) -> bytes: ...


def provision_under_session(
    lease: SessionLease,
    claims: Mapping[str, Any],
    corpus_id: str,
    *,
    timeout: float = MAX_PROVISION_SECONDS,
) -> VerifiedPrivateRepository:
    """Use a request deadline; a lost DB connection can release the lease early."""
    duration = _timeout(timeout)
    deadline = time.monotonic() + duration
    trusted_claims = dict(claims)
    outcomes: list[VerifiedPrivateRepository | Exception] = []
    failures: list[Exception] = []
    finished, cancelled = threading.Event(), threading.Event()
    slots = _PROVISION_SLOTS

    def run(owner: str) -> bytes:
        remaining = deadline - time.monotonic()
        if cancelled.is_set() or remaining <= 0:
            outcomes.append(CorpusHubProvisionUncertain("Repository creation needs reconciliation"))
            return b""
        try:
            outcomes.append(
                _provision_with_deadline(
                    owner, corpus_id, timeout=remaining, deadline=deadline, cancelled=cancelled
                )
            )
        except (CorpusMetadataError, CorpusHubProvisionError) as error:
            # Return through the lease so its final session check still runs.
            outcomes.append(error)
        except Exception:
            # A worker failure may occur after the remote write was accepted.
            outcomes.append(CorpusHubProvisionUncertain("Repository creation needs reconciliation"))
        return b""

    def leased_operation() -> None:
        try:
            lease.read(trusted_claims, run)
        except Exception as error:
            if outcomes and isinstance(
                outcomes[0], (VerifiedPrivateRepository, CorpusHubProvisionUncertain)
            ):
                failures.append(
                    CorpusHubProvisionUncertain("Repository creation needs reconciliation")
                )
            else:
                failures.append(error)
        finally:
            slots.release()
            finished.set()

    if not slots.acquire(blocking=False):
        raise CorpusHubProvisionError("Corpus repository provisioning is at capacity")
    try:
        threading.Thread(target=leased_operation, daemon=True).start()
    except RuntimeError as error:
        slots.release()
        raise CorpusHubProvisionError("Corpus repository worker is unavailable") from error
    if not finished.wait(max(0.0, deadline - time.monotonic())):
        cancelled.set()
        raise CorpusHubProvisionUncertain("Repository creation needs reconciliation")
    if failures:
        raise failures[0]
    if len(outcomes) != 1:
        raise CorpusHubProvisionUncertain("Repository creation needs reconciliation")
    if isinstance(outcomes[0], Exception):
        raise outcomes[0]
    return outcomes[0]


def _provision_with_deadline(
    verified_user: str,
    corpus_id: str,
    *,
    timeout: float = MAX_PROVISION_SECONDS,
    deadline: float | None = None,
    cancelled: threading.Event | None = None,
) -> VerifiedPrivateRepository:
    """Run the complete operation in a killable worker; caller must hold a live-session lease."""
    timeout = _timeout(timeout)
    deadline = (
        min(deadline, time.monotonic() + timeout)
        if deadline is not None
        else (time.monotonic() + timeout)
    )
    try:
        actor, identifier = str(UUID(verified_user)), str(UUID(corpus_id))
    except (ValueError, TypeError, AttributeError) as error:
        raise CorpusMetadataError("Verified user or corpus identity is invalid") from error
    registry_url, headers = CorpusMetadataStore._config()
    _validate_registry_url(registry_url)
    organization = os.getenv("HF_CORPUS_ORGANIZATION", "")
    token = os.getenv("HF_CORPUS_PROVISION_TOKEN", "")
    admin = os.getenv("CORPORA_SUPERADMIN_USER_ID", "")
    if not organization or not token:
        raise CorpusHubProvisionError("Corpus repository provisioning is unavailable")
    request = json.dumps(
        {
            "actor": actor,
            "corpus_id": identifier,
            "registry_url": registry_url,
            "registry_key": headers["apikey"],
            "organization": organization,
            "hub_token": token,
            "superadmin": admin,
            "deadline": deadline,
        }
    ).encode("utf-8")
    if len(request) > MAX_REQUEST_BYTES:
        raise CorpusHubProvisionError("Corpus repository configuration is invalid")
    if os.name != "posix":
        raise CorpusHubProvisionError("Corpus repository worker is unavailable")
    if (cancelled is not None and cancelled.is_set()) or time.monotonic() >= deadline:
        raise CorpusHubProvisionUncertain("Repository creation needs reconciliation")
    env = {name: os.environ[name] for name in _ENV_NAMES if name in os.environ}
    try:
        process = subprocess.Popen(
            [sys.executable, "-m", "corpora_api.timed_corpus_hub_provision"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=False,
            env=env,
            start_new_session=(os.name == "posix"),
        )
    except OSError as error:
        raise CorpusHubProvisionError("Corpus repository worker is unavailable") from error
    stdout = bytearray()
    try:
        if (cancelled is not None and cancelled.is_set()) or time.monotonic() >= deadline:
            raise TimeoutError("Worker deadline expired before input")
        assert process.stdin is not None and process.stdout is not None
        os.set_blocking(process.stdin.fileno(), False)
        os.set_blocking(process.stdout.fileno(), False)
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdin, selectors.EVENT_WRITE)
            selector.register(process.stdout, selectors.EVENT_READ)
            sent = 0
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Worker deadline expired")
                for key, _events in selector.select(remaining):
                    if key.fileobj is process.stdin:
                        try:
                            sent += os.write(process.stdin.fileno(), request[sent:])
                        except BrokenPipeError:
                            sent = len(request)
                        if sent == len(request):
                            selector.unregister(process.stdin)
                            process.stdin.close()
                    else:
                        data = os.read(process.stdout.fileno(), 4096)
                        if not data:
                            selector.unregister(process.stdout)
                            process.stdout.close()
                        else:
                            stdout.extend(data)
                            if len(stdout) > MAX_RESPONSE_BYTES:
                                raise ValueError("Worker output exceeds limit")
        process.wait(timeout=max(0.001, deadline - time.monotonic()))
    except (OSError, ValueError, TimeoutError, subprocess.TimeoutExpired) as error:
        _stop_and_reap(process)
        raise CorpusHubProvisionUncertain("Repository creation needs reconciliation") from error
    returncode = process.returncode
    try:
        if returncode != 0:
            raise ValueError("Invalid worker output")
        value = json.loads(stdout)
        if not isinstance(value, dict):
            raise ValueError("Invalid worker output")
        if set(value) == {"error"}:
            code = value["error"]
            if code == "not_found":
                raise CorpusMetadataNotFound("Corpus not found")
            if code == "conflict":
                raise CorpusHubProvisionConflict(
                    "Repository creation conflicts; review is required"
                )
            if code == "rejected":
                raise CorpusHubProvisionError("Corpus repository provisioning was rejected")
            raise CorpusHubProvisionUncertain("Repository creation needs reconciliation")
        if set(value) != {
            "corpus_id",
            "metadata_revision",
            "repo_id",
            "repo_type",
            "observed_head_sha",
        }:
            raise ValueError("Invalid worker output")
        if (
            value["corpus_id"] != identifier
            or value["repo_id"] != f"{organization}/corpus-{identifier}"
            or value["repo_type"] not in {"dataset", "model"}
            or type(value["metadata_revision"]) is not int
            or value["metadata_revision"] < 1
            or not isinstance(value["observed_head_sha"], str)
            or not re.fullmatch(r"[0-9a-f]{40}", value["observed_head_sha"])
        ):
            raise ValueError("Invalid worker identity")
        return VerifiedPrivateRepository(
            corpus_id=UUID(identifier),
            metadata_revision=value["metadata_revision"],
            repo_id=value["repo_id"],
            repo_type=value["repo_type"],
            observed_head_sha=value["observed_head_sha"],
        )
    except (CorpusMetadataNotFound, CorpusHubProvisionError):
        raise
    except (ValueError, TypeError, KeyError) as error:
        raise CorpusHubProvisionUncertain("Repository creation needs reconciliation") from error


def _main() -> None:
    try:
        request = json.load(sys.stdin)
        registry_url, registry_key = request["registry_url"], request["registry_key"]
        organization, token = request["organization"], request["hub_token"]
        admin = request["superadmin"]
        values = (registry_url, registry_key, organization, token)
        if not all(isinstance(v, str) and v for v in values):
            raise ValueError("Invalid worker configuration")

        class ConfiguredMetadata(CorpusMetadataStore):
            @staticmethod
            def _config() -> tuple[str, dict[str, str]]:
                return registry_url, {
                    "apikey": registry_key,
                    "Authorization": "Bearer " + registry_key,
                    "Prefer": "return=representation",
                }

            @staticmethod
            def _is_superadmin(actor: str) -> bool:
                if not admin:
                    return False
                try:
                    return UUID(admin) == UUID(actor)
                except (ValueError, TypeError, AttributeError) as error:
                    raise CorpusMetadataError("Superadmin configuration is invalid") from error

            @staticmethod
            def _check_link(row: Any) -> None:
                CorpusMetadataStore._check_link_in_org(row, organization)

        deadline = request["deadline"]
        if (
            isinstance(deadline, bool)
            or not isinstance(deadline, (int, float))
            or not math.isfinite(deadline)
        ):
            raise ValueError("Invalid worker deadline")
        result = CorpusHubProvisioner(ConfiguredMetadata(), token=token)._create_private(
            request["actor"], request["corpus_id"], deadline=deadline
        )
        output = {
            "corpus_id": str(result.corpus_id),
            "metadata_revision": result.metadata_revision,
            "repo_id": result.repo_id,
            "repo_type": result.repo_type,
            "observed_head_sha": result.observed_head_sha,
        }
    except CorpusMetadataNotFound:
        output = {"error": "not_found"}
    except CorpusHubProvisionConflict:
        output = {"error": "conflict"}
    except CorpusHubProvisionUncertain:
        output = {"error": "uncertain"}
    except (CorpusMetadataError, CorpusHubProvisionError):
        output = {"error": "rejected"}
    except Exception:
        output = {"error": "uncertain"}
    print(json.dumps(output, separators=(",", ":")))


if __name__ == "__main__":
    _main()
