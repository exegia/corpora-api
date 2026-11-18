"""Session-authorized, idempotent edits of immutable corpus versions."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, model_validator
from starlette.concurrency import run_in_threadpool

from corpora_api.auth import require_user
from corpora_api.corpus_access_api import CorpusReadHead, read_access
from corpora_api.corpus_creation import CreationRPC, _session, require_creation_config
from corpora_api.corpus_hub_deadline import DeadlineCorpusHubReader
from corpora_api.corpus_hub_read import CorpusHubReadError
from corpora_api.corpus_import_api import _claim
from corpora_api.corpus_import_worker import ImportSlot, run_import_worker
from corpora_api.corpus_metadata import (
    CorpusMetadataConflict,
    CorpusMetadataError,
    CorpusMetadataNotFound,
)
from corpora_api.corpus_process import CorpusProcessError, run_corpus

router = APIRouter(prefix="/v1/corpora", tags=["Corpora"])
hub_reader = DeadlineCorpusHubReader()


class EditBase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation_id: UUID
    expected_sequence: int = Field(strict=True, ge=1, lt=2**63 - 1)
    expected_commit: str = Field(pattern=r"^[0-9a-f]{40}$")


class ManifestEdit(EditBase):
    changes: dict[str, Any] = Field(min_length=1)


class NodeEdit(EditBase):
    features: dict[str, StrictStr | StrictInt | None] = Field(min_length=1)


class RestoreEdit(EditBase):
    pass


class CorpusEditInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")
    corpus_id: UUID
    operation_id: UUID
    state: Literal["admitted", "executing", "uncertain", "completed", "failed"]
    sequence: int | None = Field(default=None, gt=1)

    @model_validator(mode="after")
    def consistent(self) -> CorpusEditInfo:
        if (self.state == "completed") != (self.sequence is not None):
            raise ValueError("Invalid corpus edit receipt")
        return self


def _info(value: Any, corpus: UUID, operation: UUID) -> CorpusEditInfo:
    if value == {"status": "not_found"}:
        raise CorpusMetadataNotFound("Corpus edit not found")
    if value == {"status": "conflict"}:
        raise CorpusMetadataConflict("Corpus edit conflicts")
    try:
        if not isinstance(value, dict) or value.get("status") != "existing":
            raise ValueError("Unexpected edit status")
        info = CorpusEditInfo.model_validate({k: v for k, v in value.items() if k != "status"})
        if info.corpus_id != corpus or info.operation_id != operation:
            raise ValueError("Edit identity mismatch")
        return info
    except (TypeError, ValueError):
        raise CorpusMetadataError("Invalid corpus edit response") from None


def _intent(corpus: UUID, kind: str, node: int | None, edit: EditBase) -> str:
    try:
        data = json.dumps(
            {
                "corpus_id": str(corpus),
                "kind": kind,
                "node": node,
                "request": edit.model_dump(mode="json", exclude={"operation_id"}),
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (ValueError, TypeError, UnicodeError):
        raise HTTPException(422, "Invalid corpus edit") from None
    if len(data) > 100_000:
        raise HTTPException(413, "Corpus edit exceeds 100000 bytes")
    return hashlib.sha256(data).hexdigest()


async def _edit(
    claims: dict[str, Any],
    corpus: UUID,
    kind: str,
    node: int | None,
    edit: ManifestEdit | NodeEdit | RestoreEdit,
) -> tuple[CorpusEditInfo, int]:
    require_creation_config(creating=True)
    session = _session(claims)
    intent = _intent(corpus, kind, node, edit)
    rpc = CreationRPC()
    identity = {
        **session,
        "p_corpus": str(corpus),
        "p_operation": str(edit.operation_id),
        "p_intent": intent,
    }
    with ImportSlot() as slot:
        status = await run_in_threadpool(rpc.call, "corpora_get_edit_intent", identity)
        if status != {"status": "absent"}:
            info = _info(status, corpus, edit.operation_id)
            return info, 200 if info.state == "completed" else 202
        access = await run_in_threadpool(read_access, claims, corpus)
        head = access.head
        if access.row.state != "ready" or head is None:
            raise HTTPException(404, "Corpus content is unavailable")
        if (head.sequence, head.commit_sha) != (edit.expected_sequence, edit.expected_commit):
            # An identical concurrent request may have completed since our first lookup.
            latest = await run_in_threadpool(rpc.call, "corpora_get_edit_intent", identity)
            if latest != {"status": "absent"}:
                info = _info(latest, corpus, edit.operation_id)
                return info, 200 if info.state == "completed" else 202
            raise HTTPException(409, "Corpus version changed")
        source = head
        if isinstance(edit, RestoreEdit):
            result = await run_in_threadpool(
                rpc.call,
                "corpora_get_restore_source",
                {
                    **session,
                    "p_corpus": str(corpus),
                    "p_sequence": node,
                },
            )
            if result == {"status": "not_found"}:
                raise CorpusMetadataNotFound("Corpus version not found")
            try:
                if not isinstance(result, dict) or result.get("status") != "ok":
                    raise ValueError("Invalid restore source")
                source = CorpusReadHead.model_validate(
                    {k: v for k, v in result.items() if k != "status"}
                )
                if source.sequence != node:
                    raise ValueError("Restore sequence mismatch")
            except (TypeError, ValueError):
                raise CorpusMetadataError("Invalid restore source") from None
        data = bytearray()

        async def collect(chunk: bytes) -> None:
            data.extend(chunk)

        try:
            await hub_reader.transfer_authorized(
                access.row,
                source.commit_sha,
                ".corpora/source.corpus",
                max_bytes=source.archive_size,
                timeout=20,
                sink=collect,
            )
        except CorpusHubReadError:
            raise HTTPException(503, "Corpus storage is unavailable") from None
        if (
            len(data) != source.archive_size
            or hashlib.sha256(data).hexdigest() != source.archive_sha256
        ):
            raise HTTPException(503, "Corpus integrity verification failed")
        if isinstance(edit, RestoreEdit):
            archive = bytes(data)
        else:
            args = (
                {"manifest": edit.changes}
                if isinstance(edit, ManifestEdit)
                else {"node": node, "features": edit.features}
            )
            try:
                result = await run_in_threadpool(run_corpus, bytes(data), "edit", **args)
                archive = base64.b64decode(result["archive"], validate=True)
                if not 0 < len(archive) <= 3_000_000:
                    raise ValueError("Invalid edit archive")
            except CorpusProcessError as error:
                status_code = {"too_large": 413, "unavailable": 503}.get(error.kind, 422)
                raise HTTPException(status_code, "Corpus edit could not be prepared") from None
            except (KeyError, TypeError, ValueError, binascii.Error):
                raise HTTPException(503, "Corpus edit preparation failed") from None
        digest = hashlib.sha256(archive).hexdigest()
        admitted = await run_in_threadpool(
            rpc.call,
            "corpora_begin_corpus_edit",
            {
                **identity,
                "p_sequence": head.sequence,
                "p_parent": head.commit_sha,
                "p_digest": digest,
                "p_size": len(archive),
                "p_kind": kind,
                "p_restore": node if isinstance(edit, RestoreEdit) else None,
            },
        )
        if not isinstance(admitted, dict) or admitted.get("status") != "claimed":
            info = _info(admitted, corpus, edit.operation_id)
            return info, 200 if info.state == "completed" else 202
        if type(admitted.get("metadata_revision")) is not int or admitted["metadata_revision"] < 1:
            raise CorpusMetadataError("Invalid edit admission")
        claim = _claim(
            admitted,
            {
                "corpus_id": str(corpus),
                "operation_id": str(edit.operation_id),
                "actor_id": session["p_actor"],
                "session_id": session["p_session"],
                "expected_sequence": head.sequence,
                "expected_parent_sha": head.commit_sha,
                "hub_parent_sha": head.commit_sha,
                "input_sha256": digest,
                "input_format": "corpus",
                "repo_id": f"{os.environ['HF_CORPUS_ORGANIZATION']}/corpus-{corpus}",
                "repo_type": "dataset",
                "retained_ref": "corpora-op-" + str(edit.operation_id),
            },
        )
        try:
            await run_in_threadpool(run_import_worker, claim, "edit.corpus", archive, slot=slot)
        except Exception:
            try:
                await run_in_threadpool(
                    rpc.call,
                    "corpora_mark_version_save_uncertain",
                    {
                        "p_operation": str(edit.operation_id),
                        "p_claim": str(claim.claim_id),
                    },
                )
            except Exception:
                pass
            raise HTTPException(503, "Edit outcome is uncertain; check edit status") from None
        return CorpusEditInfo(
            corpus_id=corpus,
            operation_id=edit.operation_id,
            state="completed",
            sequence=head.sequence + 1,
        ), 201


@router.patch("/{corpus_id}/manifest", response_model=CorpusEditInfo)
async def edit_manifest(
    corpus_id: UUID,
    edit: ManifestEdit,
    request: Request,
    response: Response,
    _user: str = Depends(require_user),
) -> CorpusEditInfo:
    """Create a new version with changed manifest fields."""
    result, response.status_code = await _edit(
        request.state.user_claims, corpus_id, "manifest", None, edit
    )
    response.headers["Cache-Control"] = "no-store"
    return result


@router.patch("/{corpus_id}/nodes/{node}", response_model=CorpusEditInfo)
async def edit_node(
    corpus_id: UUID,
    node: int,
    edit: NodeEdit,
    request: Request,
    response: Response,
    _user: str = Depends(require_user),
) -> CorpusEditInfo:
    """Create a new version with changed Text-Fabric node features."""
    if node < 1:
        raise HTTPException(422, "Node must be positive")
    result, response.status_code = await _edit(
        request.state.user_claims, corpus_id, "node", node, edit
    )
    response.headers["Cache-Control"] = "no-store"
    return result


@router.post("/{corpus_id}/versions/{sequence}/restore", response_model=CorpusEditInfo)
async def restore_version(
    corpus_id: UUID,
    sequence: int,
    edit: RestoreEdit,
    request: Request,
    response: Response,
    _user: str = Depends(require_user),
) -> CorpusEditInfo:
    """Restore a previous snapshot as a new version with the current head as parent."""
    if sequence < 1:
        raise HTTPException(422, "Version sequence must be positive")
    result, response.status_code = await _edit(
        request.state.user_claims,
        corpus_id,
        "restore",
        sequence,
        edit,
    )
    response.headers["Cache-Control"] = "no-store"
    return result


@router.get("/{corpus_id}/edits/{operation_id}", response_model=CorpusEditInfo)
async def get_edit(
    corpus_id: UUID,
    operation_id: UUID,
    request: Request,
    response: Response,
    _user: str = Depends(require_user),
) -> CorpusEditInfo:
    """Inspect an edit without redispatching its worker."""
    require_creation_config()
    value = await run_in_threadpool(
        CreationRPC().call,
        "corpora_get_version_save",
        {
            **_session(request.state.user_claims),
            "p_corpus": str(corpus_id),
            "p_operation": str(operation_id),
        },
    )
    response.headers["Cache-Control"] = "no-store"
    return _info(value, corpus_id, operation_id)
