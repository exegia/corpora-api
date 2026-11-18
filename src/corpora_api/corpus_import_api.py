"""Authenticated initial imports into an already provisioned private corpus."""

import hashlib
import os
from collections.abc import Mapping
from dataclasses import fields
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, Response, UploadFile
from pydantic import BaseModel, ConfigDict, model_validator
from starlette.concurrency import run_in_threadpool

from corpora_api.auth import require_user
from corpora_api.corpus_archive_prepare import prepare_corpus_archive
from corpora_api.corpus_creation import CreationRPC, _session, require_creation_config
from corpora_api.corpus_hub_publish import publication_files
from corpora_api.corpus_hub_stage import CorpusStageError
from corpora_api.corpus_metadata import (
    CorpusMetadataConflict,
    CorpusMetadataError,
    CorpusMetadataNotFound,
)
from corpora_api.corpus_metadata_api import read_metadata
from corpora_api.corpus_save_claim import ClaimedCorpusSave
from corpora_api.validation import ValidationBridge

router = APIRouter(tags=["Corpora"])


class CorpusImportInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")
    corpus_id: UUID
    operation_id: UUID
    state: Literal["admitted", "executing", "uncertain", "completed", "failed"]
    sequence: Literal[1] | None

    @model_validator(mode="after")
    def consistent(self) -> "CorpusImportInfo":
        if (self.state == "completed") != (self.sequence is not None):
            raise ValueError("Invalid import receipt")
        return self


def _info(value: Any, corpus: UUID, operation: UUID) -> CorpusImportInfo:
    if value == {"status": "not_found"}:
        raise CorpusMetadataNotFound("Corpus import not found")
    if value == {"status": "conflict"}:
        raise CorpusMetadataConflict("Corpus import request conflicts")
    try:
        if not isinstance(value, dict) or value.get("status") != "existing":
            raise ValueError("Invalid import status")
        info = CorpusImportInfo.model_validate({k: v for k, v in value.items() if k != "status"})
        if info.corpus_id != corpus or info.operation_id != operation:
            raise ValueError("Import identity mismatch")
        return info
    except (ValueError, TypeError):
        raise CorpusMetadataError("Corpus import response is invalid") from None


def import_status(claims: Mapping[str, Any], corpus: UUID, operation: UUID) -> CorpusImportInfo:
    require_creation_config()
    return _info(
        CreationRPC().call(
            "corpora_get_import",
            {
                **_session(claims),
                "p_corpus": str(corpus),
                "p_operation": str(operation),
            },
        ),
        corpus,
        operation,
    )


def _claim(value: dict[str, Any], expected: dict[str, Any]) -> ClaimedCorpusSave:
    try:
        raw = {k: v for k, v in value.items() if k != "status"}
        if set(raw) != {field.name for field in fields(ClaimedCorpusSave)}:
            raise ValueError("Unexpected claim fields")
        for name, wanted in expected.items():
            if raw[name] != wanted or type(raw[name]) is not type(wanted):
                raise ValueError("Claim identity mismatch")
        for name in ("operation_id", "claim_id", "corpus_id", "actor_id", "session_id"):
            raw[name] = UUID(raw[name])
        claim = ClaimedCorpusSave(**raw)
        if (
            not isinstance(claim.hub_parent_sha, str)
            or len(claim.hub_parent_sha) != 40
            or any(c not in "0123456789abcdef" for c in claim.hub_parent_sha)
        ):
            raise ValueError("Invalid baseline")
        return claim
    except (ValueError, TypeError, KeyError, AttributeError):
        raise CorpusMetadataError("Corpus import admission is invalid") from None


def import_corpus(
    claims: Mapping[str, Any],
    corpus: UUID,
    operation: UUID,
    revision: int,
    filename: str,
    archive: bytes,
) -> tuple[CorpusImportInfo, int]:
    from corpora_api.corpus_import_worker import ImportSlot

    with ImportSlot() as slot:
        return _import_corpus(claims, corpus, operation, revision, filename, archive, slot)


def _import_corpus(
    claims: Mapping[str, Any],
    corpus: UUID,
    operation: UUID,
    revision: int,
    filename: str,
    archive: bytes,
    slot: Any,
) -> tuple[CorpusImportInfo, int]:
    from corpora_api.corpus_import_worker import run_import_worker

    require_creation_config(creating=True)
    read_metadata(claims, corpus)
    if not filename.lower().endswith(".corpus"):
        raise HTTPException(415, "Import a validated .corpus archive with its shipped cache")
    digest = hashlib.sha256(archive).hexdigest()
    try:
        prepared = prepare_corpus_archive(
            filename, archive, digest, ValidationBridge(), expected_format="corpus"
        )
        publication_files(prepared)
    except (ValueError, CorpusStageError):
        raise HTTPException(422, "Corpus archive or storage layout is invalid") from None
    session = _session(claims)
    rpc = CreationRPC()
    value = rpc.call(
        "corpora_begin_import",
        {
            **session,
            "p_corpus": str(corpus),
            "p_operation": str(operation),
            "p_revision": revision,
            "p_digest": digest,
            "p_size": len(archive),
        },
    )
    if not isinstance(value, dict) or value.get("status") != "claimed":
        info = _info(value, corpus, operation)
        return info, 200 if info.state == "completed" else 202
    claim = _claim(
        value,
        {
            "corpus_id": str(corpus),
            "operation_id": str(operation),
            "actor_id": session["p_actor"],
            "session_id": session["p_session"],
            "expected_sequence": 0,
            "expected_parent_sha": None,
            "input_sha256": digest,
            "input_format": "corpus",
            "metadata_revision": revision,
            "repo_id": f"{os.environ['HF_CORPUS_ORGANIZATION']}/corpus-{corpus}",
            "repo_type": "dataset",
            "retained_ref": "corpora-op-" + str(operation),
        },
    )
    try:
        run_import_worker(claim, filename, archive, slot=slot)
    except Exception:
        try:
            rpc.call(
                "corpora_mark_import_uncertain",
                {
                    "p_operation": str(operation),
                    "p_claim": str(claim.claim_id),
                },
            )
        except Exception:
            pass  # A lost acknowledgement leaves the original claim reserved.
        raise HTTPException(503, "Import outcome is uncertain; check import status") from None
    return CorpusImportInfo(
        corpus_id=corpus, operation_id=operation, state="completed", sequence=1
    ), 201


@router.post("/v1/corpora/{corpus_id}/imports", response_model=CorpusImportInfo)
async def initial_import(
    corpus_id: UUID,
    request: Request,
    response: Response,
    operation_id: Annotated[UUID, Form()],
    expected_revision: Annotated[int, Form(ge=1, lt=2**63 - 1)],
    file: Annotated[UploadFile, File()],
    _user: str = Depends(require_user),
) -> CorpusImportInfo:
    """Store the initial validated .corpus; use one operation UUID for identical retries."""
    data = await file.read(3_000_001)
    await file.close()
    if len(data) > 3_000_000:
        raise HTTPException(413, "Corpus archive exceeds 3000000 bytes")
    result, response.status_code = await run_in_threadpool(
        import_corpus,
        request.state.user_claims,
        corpus_id,
        operation_id,
        expected_revision,
        file.filename or "",
        data,
    )
    response.headers["Cache-Control"] = "no-store"
    return result


@router.get("/v1/corpora/{corpus_id}/imports/{operation_id}", response_model=CorpusImportInfo)
async def get_import(
    corpus_id: UUID,
    operation_id: UUID,
    request: Request,
    response: Response,
    _user: str = Depends(require_user),
) -> CorpusImportInfo:
    """Inspect a retained import operation without redispatching a worker."""
    response.headers["Cache-Control"] = "no-store"
    return await run_in_threadpool(
        import_status, request.state.user_claims, corpus_id, operation_id
    )


@router.post(
    "/v1/corpora/{corpus_id}/imports/{operation_id}/reconcile", response_model=CorpusImportInfo
)
async def reconcile_import(
    corpus_id: UUID,
    operation_id: UUID,
    request: Request,
    response: Response,
    _user: str = Depends(require_user),
) -> CorpusImportInfo:
    """Verify an uncertain initial import without issuing another Hub write."""
    from corpora_api.corpus_import_worker import ImportSlot, run_reconciliation_worker
    from corpora_api.corpus_save_reconcile import SaveReconciliationTarget

    require_creation_config(creating=True)
    rpc = CreationRPC()
    identity = {
        **_session(request.state.user_claims),
        "p_corpus": str(corpus_id),
        "p_operation": str(operation_id),
    }
    with ImportSlot() as slot:
        value = await run_in_threadpool(rpc.call, "corpora_get_import_reconciliation", identity)
        if isinstance(value, dict) and value.get("status") == "ready":
            try:
                target = SaveReconciliationTarget.model_validate(value)
                if (
                    target.mode != "import"
                    or target.corpus_id != corpus_id
                    or target.operation_id != operation_id
                ):
                    raise ValueError("Mismatched import reconciliation target")
            except (TypeError, ValueError):
                raise CorpusMetadataError("Invalid import reconciliation target") from None
            try:
                await run_in_threadpool(
                    run_reconciliation_worker, target.model_dump(mode="json"), slot=slot
                )
            except Exception:
                # Missing or changing Hub evidence is not proof of failure.
                pass
            value = await run_in_threadpool(rpc.call, "corpora_get_import", identity)
        elif value == {"status": "unresolved"}:
            value = await run_in_threadpool(rpc.call, "corpora_get_import", identity)
        info = _info(value, corpus_id, operation_id)
    response.status_code = 200 if info.state in ("completed", "failed") else 202
    response.headers["Cache-Control"] = "no-store"
    return info
