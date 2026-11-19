"""Killable worker for one previously admitted initial corpus import."""

from __future__ import annotations

import base64
import json
import os
import selectors
import subprocess
import sys
import threading
import time
from dataclasses import asdict
from typing import Any
from uuid import UUID, uuid4

import httpx
from fastapi import HTTPException
from huggingface_hub import HfApi, set_client_factory

from corpora_api.corpus_archive_prepare import prepare_corpus_archive
from corpora_api.corpus_creation import CreationRPC
from corpora_api.corpus_hub_publish import SOURCE_PATH, CorpusHubPublisher
from corpora_api.corpus_hub_stage import CorpusHubStager
from corpora_api.corpus_metadata import CorpusMetadataStore
from corpora_api.corpus_publication_claim import CorpusPublicationClaim
from corpora_api.corpus_save_claim import ClaimedCorpusSave
from corpora_api.timed_corpus_hub_provision import _ENV_NAMES, _stop_and_reap
from corpora_api.validation import ValidationBridge

MAX_IMPORT_SECONDS = 120.0
MAX_REQUEST_BYTES = 4_100_000
MAX_RESULT_BYTES = 8192
_SLOTS = threading.BoundedSemaphore(2)


class _BoundedHubStream(httpx.SyncByteStream):
    def __init__(self, stream: httpx.SyncByteStream, owner: _BoundedHubTransport) -> None:
        self.stream, self.owner = stream, owner

    def __iter__(self):
        received = 0
        for chunk in self.stream:
            received += len(chunk)
            self.owner.received += len(chunk)
            if (
                received > 2_000_000
                or self.owner.received > 8_000_000
                or time.monotonic() >= self.owner.deadline
            ):
                raise ValueError("Hub response exceeds import bounds")
            yield chunk

    def close(self) -> None:
        self.stream.close()


class _BoundedHubTransport(httpx.BaseTransport):
    """Bound SDK control responses; corpus downloads have separate byte limits."""

    def __init__(self, deadline: float) -> None:
        self.deadline, self.received = deadline, 0
        self.inner = httpx.HTTPTransport()

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Hub deadline expired")
        request.headers["Accept-Encoding"] = "identity"
        request.extensions["timeout"] = {
            key: min(15.0, remaining) for key in ("connect", "read", "write", "pool")
        }
        response = self.inner.handle_request(request)
        if response.headers.get("content-encoding", "identity").lower() != "identity":
            response.close()
            raise ValueError("Compressed Hub control response is unsupported")
        response.stream = _BoundedHubStream(response.stream, self)
        return response

    def close(self) -> None:
        self.inner.close()


class ImportSlot:
    """Reserve capacity before admission; a started worker owns its release."""

    def __init__(self) -> None:
        self.started = False
        self.released = False

    def __enter__(self) -> ImportSlot:
        if not _SLOTS.acquire(blocking=False):
            raise HTTPException(503, "Corpus import workers are at capacity")
        return self

    def release(self) -> None:
        if not self.released:
            self.released = True
            _SLOTS.release()

    def __exit__(self, *_: Any) -> None:
        if not self.started:
            self.release()


def _exchange(request: bytes, deadline: float) -> dict[str, Any]:
    env = {key: os.environ[key] for key in _ENV_NAMES if key in os.environ}
    env.update(
        {
            "HF_HUB_DISABLE_XET": "1",
            "HF_HUB_DISABLE_PROGRESS_BARS": "1",
            "HF_HUB_DISABLE_TELEMETRY": "1",
        }
    )
    process = subprocess.Popen(
        [sys.executable, "-m", "corpora_api.corpus_import_worker"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=env,
        start_new_session=True,
    )
    result = bytearray()
    try:
        assert process.stdin is not None and process.stdout is not None
        os.set_blocking(process.stdin.fileno(), False)
        os.set_blocking(process.stdout.fileno(), False)
        sent = 0
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdin, selectors.EVENT_WRITE)
            selector.register(process.stdout, selectors.EVENT_READ)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Import worker timed out")
                for key, _ in selector.select(remaining):
                    if key.fileobj is process.stdin:
                        sent += os.write(process.stdin.fileno(), request[sent : sent + 65536])
                        if sent == len(request):
                            selector.unregister(process.stdin)
                            process.stdin.close()
                    else:
                        chunk = os.read(process.stdout.fileno(), 4096)
                        if not chunk:
                            selector.unregister(process.stdout)
                            process.stdout.close()
                        else:
                            result.extend(chunk)
                            if len(result) > MAX_RESULT_BYTES:
                                raise ValueError("Import worker output exceeds limit")
        process.wait(timeout=max(0.001, deadline - time.monotonic()))
        if process.returncode != 0:
            raise ValueError("Import worker failed")
        return json.loads(result)
    except BaseException:
        _stop_and_reap(process)
        raise


def run_import_worker(
    claim: ClaimedCorpusSave, filename: str, archive: bytes, *, slot: ImportSlot
) -> None:
    registry, headers = CorpusMetadataStore._config()
    deadline = time.monotonic() + MAX_IMPORT_SECONDS
    payload = json.dumps(
        {
            "claim": asdict(claim),
            "filename": filename,
            "archive": base64.b64encode(archive).decode("ascii"),
            "deadline": deadline,
            "config": {
                "SUPABASE_URL": registry.rsplit("/rest/v1/", 1)[0],
                "SUPABASE_SERVICE_ROLE_KEY": headers["apikey"],
                "HF_CORPUS_ORGANIZATION": os.environ["HF_CORPUS_ORGANIZATION"],
                "HF_CORPUS_PROVISION_TOKEN": os.environ["HF_CORPUS_PROVISION_TOKEN"],
                "CORPORA_VALIDATE_BIN": os.getenv("CORPORA_VALIDATE_BIN", "corpora-validate"),
            },
        },
        default=str,
    ).encode()
    _run_dispatched(payload, deadline, slot)


def run_reconciliation_worker(target: dict[str, Any], *, slot: ImportSlot) -> None:
    _run_reconciliation(target, slot=slot, mode="reconcile")


def run_import_resume_worker(target: dict[str, Any], *, slot: ImportSlot) -> None:
    _run_reconciliation(target, slot=slot, mode="resume_import")


def _run_reconciliation(target: dict[str, Any], *, slot: ImportSlot, mode: str) -> None:
    registry, headers = CorpusMetadataStore._config()
    deadline = time.monotonic() + MAX_IMPORT_SECONDS
    payload = json.dumps(
        {
            "mode": mode,
            "target": target,
            "deadline": deadline,
            "config": {
                "SUPABASE_URL": registry.rsplit("/rest/v1/", 1)[0],
                "SUPABASE_SERVICE_ROLE_KEY": headers["apikey"],
                "HF_CORPUS_ORGANIZATION": os.environ["HF_CORPUS_ORGANIZATION"],
                "HF_CORPUS_PROVISION_TOKEN": os.environ["HF_CORPUS_PROVISION_TOKEN"],
                "CORPORA_VALIDATE_BIN": os.getenv("CORPORA_VALIDATE_BIN", "corpora-validate"),
            },
        }
    ).encode()
    _run_dispatched(payload, deadline, slot)


def _run_dispatched(payload: bytes, deadline: float, slot: ImportSlot) -> None:
    if len(payload) > MAX_REQUEST_BYTES:
        raise ValueError("Import worker request exceeds limit")
    finished = threading.Event()
    outcomes: list[Any] = []

    def run() -> None:
        try:
            outcomes.append(_exchange(payload, deadline))
        except BaseException:
            outcomes.append(None)
        finally:
            slot.release()
            finished.set()

    slot.started = True
    try:
        threading.Thread(target=run, daemon=True).start()
    except BaseException:
        slot.release()
        raise
    if not finished.wait(max(0.0, deadline - time.monotonic())):
        raise TimeoutError("Import worker outcome is uncertain")
    if outcomes != [{"completed": True}]:
        raise ValueError("Import worker outcome is uncertain")


def execute_import(
    claim: ClaimedCorpusSave, filename: str, archive: bytes, deadline: float
) -> None:
    """Trusted worker path: staging readback, durable claim, verified publication, completion."""
    subsequent = claim.expected_sequence > 0
    if claim.expected_sequence < 0 or (claim.expected_sequence == 0) != (
        claim.expected_parent_sha is None
    ):
        raise ValueError("Invalid corpus version parent")
    if subsequent and claim.expected_parent_sha != claim.hub_parent_sha:
        raise ValueError("Corpus version parent does not match Hub base")
    validator = ValidationBridge()
    rpc = CreationRPC()
    token = os.environ["HF_CORPUS_PROVISION_TOKEN"]
    api = HfApi(endpoint="https://huggingface.co", token=token)
    with httpx.Client(timeout=15, trust_env=False) as client:
        stager = CorpusHubStager(api, client, token)
        publisher = CorpusHubPublisher(api, client, token)
        previous = None
        if subsequent:
            base = rpc.call(
                "corpora_version_save_base",
                {
                    "p_operation": str(claim.operation_id),
                    "p_claim": str(claim.claim_id),
                },
            )
            if (
                not isinstance(base, dict)
                or base.get("sequence") != claim.expected_sequence
                or base.get("commit_sha") != claim.expected_parent_sha
                or not isinstance(base.get("retained_ref"), str)
            ):
                raise ValueError("Corpus version base changed")
            publisher._check_private_ref(
                claim.repo_id, claim.repo_type, base["retained_ref"], claim.hub_parent_sha
            )
            previous_bytes = stager._read_verified_bytes(
                claim.repo_id,
                claim.repo_type,
                claim.hub_parent_sha,
                SOURCE_PATH,
                base["archive_size"],
                base["archive_sha256"],
            )
            publisher._check_private_ref(
                claim.repo_id, claim.repo_type, base["retained_ref"], claim.hub_parent_sha
            )
            previous = prepare_corpus_archive(
                "base.corpus",
                previous_bytes,
                base["archive_sha256"],
                validator,
                expected_format="corpus",
            )
        staged = stager.stage(claim, filename, archive, validator)
        publication = uuid4()
        if (
            rpc.call(
                "corpora_stage_version_save" if subsequent else "corpora_stage_import",
                {
                    "p_operation": str(claim.operation_id),
                    "p_claim": str(claim.claim_id),
                    "p_stage_sha": staged.stage_sha,
                    "p_publication": str(publication),
                },
            )
            is not True
        ):
            raise ValueError("Import publication was not claimed")
        prepared = prepare_corpus_archive(
            filename, archive, claim.input_sha256, validator, expected_format="corpus"
        )
        candidate = publisher.publish(
            CorpusPublicationClaim(staged, publication),
            prepared,
            deadline=deadline,
            **({"previous": previous} if subsequent else {}),
        )
        params = {
            "p_operation": str(claim.operation_id),
            "p_claim": str(claim.claim_id),
            "p_publication": str(publication),
            "p_sha": candidate.commit_sha,
        }
        try:
            if (
                rpc.call(
                    "corpora_complete_version_save" if subsequent else "corpora_complete_import",
                    params,
                )
                is not True
            ):
                raise ValueError("Import completion was rejected")
        except Exception:
            # Observe the exact outcome after a lost commit acknowledgement.
            # No network upload or publication claim is repeated.
            if (
                rpc.call(
                    "corpora_find_version_save_result"
                    if subsequent
                    else "corpora_find_import_result",
                    params,
                )
                is not True
            ):
                raise


def _main() -> None:
    try:
        raw = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
        if len(raw) > MAX_REQUEST_BYTES:
            raise ValueError("Invalid input")
        request = json.loads(raw)
        for key in (
            "SUPABASE_URL",
            "SUPABASE_SERVICE_ROLE_KEY",
            "HF_CORPUS_ORGANIZATION",
            "HF_CORPUS_PROVISION_TOKEN",
            "CORPORA_VALIDATE_BIN",
        ):
            os.environ[key] = request["config"][key]
        transport = _BoundedHubTransport(request["deadline"])
        set_client_factory(
            lambda: httpx.Client(
                transport=transport, timeout=15, follow_redirects=True, trust_env=False
            )
        )
        if request.get("mode") in ("reconcile", "resume_import"):
            from corpora_api.corpus_save_reconcile import (
                SaveReconciliationTarget,
                execute_save_reconciliation,
                resume_initial_import,
            )

            target = SaveReconciliationTarget.model_validate(request["target"])
            if request["mode"] == "resume_import":
                resume_initial_import(target, request["deadline"])
            else:
                execute_save_reconciliation(target, request["deadline"])
        else:
            values = request["claim"]
            for key in ("operation_id", "claim_id", "corpus_id", "actor_id", "session_id"):
                values[key] = UUID(values[key])
            claim = ClaimedCorpusSave(**values)
            archive = base64.b64decode(request["archive"], validate=True)
            if not 0 < len(archive) <= 3_000_000:
                raise ValueError("Invalid archive")
            execute_import(claim, request["filename"], archive, request["deadline"])
        result = {"completed": True}
    except Exception:
        result = {"completed": False}
    print(json.dumps(result))


if __name__ == "__main__":
    _main()
