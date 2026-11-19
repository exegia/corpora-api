"""Public API visibility and one-shot deletion of private corpus storage."""

import asyncio
import json
import os
import re
from collections.abc import Callable, Mapping
from typing import Any, Literal
from urllib.parse import quote
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from corpora_api.auth import require_user
from corpora_api.corpus_creation import CreationRPC, _session, require_creation_config
from corpora_api.corpus_metadata import (
    CorpusMetadata,
    CorpusMetadataConflict,
    CorpusMetadataError,
    CorpusMetadataNotFound,
    CorpusMetadataStore,
)
from corpora_api.corpus_metadata_api import CorpusMetadataInfo, _metadata, read_metadata
from corpora_api.corpus_provision_observation import CorpusProvisionHubObserver

router = APIRouter(tags=["Corpora"])


class CorpusVisibility(BaseModel):
    model_config = ConfigDict(extra="forbid")
    corpus_id: UUID
    revision: int = Field(gt=0)
    visibility: Literal["private", "public"]


class VisibilityPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(strict=True, gt=0, lt=2**63 - 1)
    visibility: Literal["private", "public"]


class DeletionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation_id: UUID
    expected_revision: int = Field(strict=True, gt=0, lt=2**63 - 1)


class CorpusDeletion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    corpus_id: UUID
    operation_id: UUID
    state: Literal["executing", "uncertain", "completed"]


class CorpusReadHead(BaseModel):
    model_config = ConfigDict(extra="forbid")
    commit_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    sequence: int = Field(strict=True, gt=0)
    retained_ref: str = Field(pattern=r"^corpora-op-[0-9a-f-]{36}$")
    archive_size: int = Field(strict=True, gt=0, le=3_000_000)
    archive_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class CorpusReadAccess(BaseModel):
    model_config = ConfigDict(extra="forbid")
    row: CorpusMetadata
    visibility: Literal["private", "public"]
    head: CorpusReadHead | None


def _status(value: Any) -> None:
    if value == {"status": "not_found"}:
        raise CorpusMetadataNotFound("Corpus not found")
    if value == {"status": "conflict"}:
        raise CorpusMetadataConflict("Corpus request conflicts with its current state")


def read_access(claims: Mapping[str, Any] | None, corpus: UUID) -> CorpusReadAccess:
    require_creation_config()
    session = (
        _session(claims)
        if claims is not None
        else {"p_actor": None, "p_session": None, "p_token_exp": None}
    )
    value = CreationRPC(max_response_bytes=131072).call(
        "corpora_read_corpus_access", {**session, "p_corpus": str(corpus)}
    )
    _status(value)
    try:
        if not isinstance(value, dict) or value.get("status") != "ok":
            raise ValueError("Invalid access")
        access = CorpusReadAccess.model_validate({k: v for k, v in value.items() if k != "status"})
        if access.row.id != corpus:
            raise ValueError("Corpus identity mismatch")
        if claims is None and (
            access.visibility != "public" or access.row.state != "ready" or access.head is None
        ):
            raise ValueError("Anonymous access was not public")
        CorpusMetadataStore._check_link(access.row)
        return access
    except (ValueError, TypeError):
        raise CorpusMetadataError("Corpus access response is invalid") from None


def read_visible_metadata(claims: Mapping[str, Any] | None, corpus: UUID) -> CorpusMetadataInfo:
    access = read_access(claims, corpus)
    return _metadata({"status": "ok", "row": access.row.model_dump(mode="json")}, corpus)


class PrivateHubLifecycle:
    """Bounded direct HTTP requests. A deletion is sent at most once per claim."""

    def __init__(
        self,
        transport_factory: Callable[[], httpx.AsyncBaseTransport] | None = None,
        *,
        timeout: float = 20,
    ) -> None:
        self.transport_factory, self.timeout = transport_factory, timeout

    async def _request(self, client: httpx.AsyncClient, method: str, url: str, **kwargs):
        include_headers = kwargs.pop("include_headers", False)
        response = None
        try:
            response = await client.send(
                client.build_request(method, url, **kwargs), stream=True, follow_redirects=False
            )
            if response.headers.get("content-encoding", "identity").lower() != "identity":
                raise ValueError("Unsupported Hub encoding")
            body = bytearray()
            async for chunk in response.aiter_raw():
                body.extend(chunk)
                if len(body) > 65536:
                    raise ValueError("Hub response exceeds bound")
            if include_headers:
                return response.status_code, bytes(body), response.headers
            return response.status_code, bytes(body)
        finally:
            if response is not None:
                await CorpusProvisionHubObserver._close(response)

    async def operate(
        self,
        corpus: UUID,
        repo: str,
        kind: str,
        *,
        sha: str | None = None,
        delete=False,
        empty_only=False,
    ):
        token, organization = (
            os.getenv("HF_CORPUS_PROVISION_TOKEN", ""),
            os.getenv("HF_CORPUS_ORGANIZATION", ""),
        )
        if (
            not token
            or not organization
            or repo != f"{organization}/corpus-{corpus}"
            or kind != "dataset"
        ):
            raise CorpusMetadataError("Private corpus storage is unavailable")
        url = "https://huggingface.co/api/datasets/" + quote(repo, safe="/")
        if sha is not None and not empty_only:
            url += "/revision/" + quote(sha, safe="")
        client = httpx.AsyncClient(
            transport=self.transport_factory() if self.transport_factory else None,
            timeout=10,
            trust_env=False,
            headers={"Authorization": "Bearer " + token, "Accept-Encoding": "identity"},
        )
        try:
            async with asyncio.timeout(self.timeout):
                status, body = await self._request(client, "GET", url)
                if status != 200:
                    raise ValueError("Repository cannot be verified")
                value = json.loads(body)
                if (
                    value.get("id") != repo
                    or value.get("private") is not True
                    or (sha is not None and value.get("sha") != sha)
                ):
                    raise ValueError("Repository identity or privacy changed")
                if empty_only:
                    observed_sha = value.get("sha")
                    if not isinstance(observed_sha, str) or not re.fullmatch(
                        r"[0-9a-f]{40}", observed_sha
                    ):
                        raise ValueError("Invalid repository revision")
                    if sha is not None and observed_sha != sha:
                        raise ValueError("Repository revision changed")
                    status, body, headers = await self._request(
                        client,
                        "GET",
                        url + "/tree/" + observed_sha + "?recursive=true&limit=2",
                        include_headers=True,
                    )
                    tree = json.loads(body)
                    if (
                        status != 200
                        or headers.get("link")
                        or not isinstance(tree, list)
                        or len(tree) != 1
                        or not isinstance(tree[0], dict)
                        or tree[0].get("path") != ".gitattributes"
                        or tree[0].get("type") != "file"
                    ):
                        raise ValueError("Repository is not empty")
                if delete:
                    status, _ = await self._request(
                        client,
                        "DELETE",
                        "https://huggingface.co/api/repos/delete",
                        json={
                            "name": "corpus-" + str(corpus),
                            "organization": organization,
                            "type": "dataset",
                        },
                    )
                    if status != 200:
                        raise ValueError("Deletion was not acknowledged")
                    status, _ = await self._request(client, "GET", url)
                    if status != 404:
                        raise ValueError("Deletion was not verified")
                if empty_only:
                    return observed_sha
        except Exception:
            raise CorpusMetadataError("Private corpus storage outcome is unconfirmed") from None
        finally:
            await CorpusProvisionHubObserver._close(client)


async def change_visibility(
    claims: Mapping[str, Any], corpus: UUID, patch: VisibilityPatch
) -> CorpusVisibility:
    require_creation_config(creating=True)
    metadata = await run_in_threadpool(read_metadata, claims, corpus)
    if metadata.revision != patch.expected_revision:
        raise CorpusMetadataConflict("Corpus metadata revision changed")
    if patch.visibility == "public":
        access = await run_in_threadpool(read_access, claims, corpus)
        if access.row.state != "ready" or access.head is None:
            raise CorpusMetadataConflict("Corpus content is not ready")
        await PrivateHubLifecycle().operate(
            corpus, access.row.hf_repo_id, access.row.hf_repo_type, sha=access.head.commit_sha
        )
    value = await run_in_threadpool(
        CreationRPC().call,
        "corpora_set_corpus_visibility",
        {
            **_session(claims),
            "p_corpus": str(corpus),
            "p_revision": patch.expected_revision,
            "p_visibility": patch.visibility,
        },
    )
    _status(value)
    try:
        if not isinstance(value, dict) or value.get("status") != "ok":
            raise ValueError("Invalid visibility response")
        result = CorpusVisibility.model_validate({k: v for k, v in value.items() if k != "status"})
        if result.corpus_id != corpus or result.visibility != patch.visibility:
            raise ValueError("Invalid visibility identity")
        return result
    except (ValueError, TypeError):
        raise CorpusMetadataError("Corpus visibility response is invalid") from None


def _deletion(value: Any, corpus: UUID, operation: UUID) -> CorpusDeletion:
    _status(value)
    try:
        if not isinstance(value, dict) or value.get("status") != "existing":
            raise ValueError("Invalid deletion response")
        result = CorpusDeletion.model_validate({k: v for k, v in value.items() if k != "status"})
        if result.corpus_id != corpus or result.operation_id != operation:
            raise ValueError("Invalid deletion identity")
        return result
    except (ValueError, TypeError):
        raise CorpusMetadataError("Corpus deletion response is invalid") from None


async def delete_corpus(
    claims: Mapping[str, Any], corpus: UUID, request: DeletionRequest
) -> CorpusDeletion:
    require_creation_config(creating=True)
    rpc = CreationRPC()
    value = await run_in_threadpool(
        rpc.call,
        "corpora_begin_corpus_deletion",
        {
            **_session(claims),
            "p_corpus": str(corpus),
            "p_operation": str(request.operation_id),
            "p_revision": request.expected_revision,
        },
    )
    if not isinstance(value, dict) or value.get("status") != "claimed":
        return _deletion(value, corpus, request.operation_id)
    try:
        if set(value) != {
            "status",
            "corpus_id",
            "operation_id",
            "claim_id",
            "repo_id",
            "repo_type",
        } or (
            UUID(value["corpus_id"]) != corpus
            or UUID(value["operation_id"]) != request.operation_id
        ):
            raise ValueError("Invalid deletion claim")
        claim = UUID(value["claim_id"])
    except (ValueError, TypeError, KeyError):
        raise CorpusMetadataError("Corpus deletion admission is invalid") from None
    params = {"p_operation": str(request.operation_id), "p_claim": str(claim), "p_completed": True}
    try:
        await PrivateHubLifecycle().operate(
            corpus, value["repo_id"], value["repo_type"], delete=True
        )
        if await run_in_threadpool(rpc.call, "corpora_finish_corpus_deletion", params) is not True:
            raise CorpusMetadataError("Corpus deletion completion was rejected")
    except Exception:
        try:
            await run_in_threadpool(
                rpc.call, "corpora_finish_corpus_deletion", {**params, "p_completed": False}
            )
        except Exception:
            pass
        raise HTTPException(503, "Deletion outcome is uncertain; check deletion status") from None
    return CorpusDeletion(corpus_id=corpus, operation_id=request.operation_id, state="completed")


async def discard_uncertain_creation(
    claims: Mapping[str, Any], corpus: UUID, request: DeletionRequest
) -> CorpusDeletion:
    require_creation_config(creating=True)
    rpc = CreationRPC()
    params = {
        **_session(claims),
        "p_corpus": str(corpus),
        "p_operation": str(request.operation_id),
        "p_revision": request.expected_revision,
    }
    prior = await run_in_threadpool(rpc.call, "corpora_uncertain_discard_status", params)
    if prior != {"status": "absent"}:
        return _deletion(prior, corpus, request.operation_id)
    target = await run_in_threadpool(
        rpc.call,
        "corpora_uncertain_discard_target",
        {k: v for k, v in params.items() if k != "p_operation"},
    )
    _status(target)
    if (
        not isinstance(target, dict)
        or set(target) != {"status", "corpus_id", "repo_id", "repo_type"}
        or target.get("status") != "ok"
        or target.get("corpus_id") != str(corpus)
    ):
        raise CorpusMetadataError("Uncertain creation target is invalid")
    hub = PrivateHubLifecycle()
    sha = await hub.operate(corpus, target["repo_id"], target["repo_type"], empty_only=True)
    claim = await run_in_threadpool(
        rpc.call, "corpora_begin_uncertain_discard", {**params, "p_observed_sha": sha}
    )
    if not isinstance(claim, dict) or claim.get("status") != "claimed":
        return _deletion(claim, corpus, request.operation_id)
    try:
        if (
            set(claim)
            != {
                "status",
                "corpus_id",
                "operation_id",
                "claim_id",
                "repo_id",
                "repo_type",
                "observed_sha",
            }
            or claim["corpus_id"] != str(corpus)
            or claim["operation_id"] != str(request.operation_id)
            or claim["repo_id"] != target["repo_id"]
            or claim["repo_type"] != target["repo_type"]
            or claim["observed_sha"] != sha
        ):
            raise ValueError("Mismatched claim")
        claim_id = UUID(claim["claim_id"])
    except (ValueError, KeyError, TypeError):
        raise CorpusMetadataError("Uncertain creation deletion admission is invalid") from None
    finish = {
        "p_operation": str(request.operation_id),
        "p_claim": str(claim_id),
        "p_completed": True,
    }
    try:
        await hub.operate(
            corpus, target["repo_id"], target["repo_type"], sha=sha, delete=True, empty_only=True
        )
        if await run_in_threadpool(rpc.call, "corpora_finish_corpus_deletion", finish) is not True:
            raise CorpusMetadataError("Deletion completion was rejected")
    except Exception:
        try:
            await run_in_threadpool(
                rpc.call, "corpora_finish_corpus_deletion", {**finish, "p_completed": False}
            )
        except Exception:
            pass
        raise HTTPException(503, "Deletion outcome is uncertain; check deletion status") from None
    return CorpusDeletion(corpus_id=corpus, operation_id=request.operation_id, state="completed")


@router.post("/v1/corpora/{corpus_id}/discard-uncertain-creation", response_model=CorpusDeletion)
async def discard_creation(
    corpus_id: UUID,
    deletion: DeletionRequest,
    request: Request,
    response: Response,
    _user: str = Depends(require_user),
) -> CorpusDeletion:
    """Superadmin: discard an uncertain creation only when its private repository is empty."""
    response.headers["Cache-Control"] = "no-store"
    result = await discard_uncertain_creation(request.state.user_claims, corpus_id, deletion)
    response.status_code = 200 if result.state == "completed" else 202
    return result


@router.get("/v1/corpora/{corpus_id}/visibility", response_model=CorpusVisibility)
async def get_visibility(corpus_id: UUID, request: Request, response: Response) -> CorpusVisibility:
    """Read visibility as an owner, superadmin, or reader of a public ready corpus."""
    response.headers["Cache-Control"] = "no-store"
    access = await run_in_threadpool(
        read_access, getattr(request.state, "user_claims", None), corpus_id
    )
    return CorpusVisibility(
        corpus_id=corpus_id, revision=access.row.revision, visibility=access.visibility
    )


@router.patch("/v1/corpora/{corpus_id}/visibility", response_model=CorpusVisibility)
async def patch_visibility(
    corpus_id: UUID,
    patch: VisibilityPatch,
    request: Request,
    response: Response,
    _user: str = Depends(require_user),
) -> CorpusVisibility:
    """Change API access at an expected revision; the backing repository remains private."""
    response.headers["Cache-Control"] = "no-store"
    return await change_visibility(request.state.user_claims, corpus_id, patch)


@router.delete("/v1/corpora/{corpus_id}", response_model=CorpusDeletion)
async def remove_corpus(
    corpus_id: UUID,
    deletion: DeletionRequest,
    request: Request,
    response: Response,
    _user: str = Depends(require_user),
) -> CorpusDeletion:
    """Hide the corpus and delete its private repository once; retain the audit receipt."""
    response.headers["Cache-Control"] = "no-store"
    result = await delete_corpus(request.state.user_claims, corpus_id, deletion)
    response.status_code = 200 if result.state == "completed" else 202
    return result


@router.get("/v1/corpora/{corpus_id}/deletions/{operation_id}", response_model=CorpusDeletion)
async def deletion_status(
    corpus_id: UUID,
    operation_id: UUID,
    request: Request,
    response: Response,
    _user: str = Depends(require_user),
) -> CorpusDeletion:
    """Inspect a deletion after normal corpus reads disappear, without retrying it."""
    require_creation_config()
    response.headers["Cache-Control"] = "no-store"
    value = await run_in_threadpool(
        CreationRPC().call,
        "corpora_get_corpus_deletion",
        {
            **_session(request.state.user_claims),
            "p_corpus": str(corpus_id),
            "p_operation": str(operation_id),
        },
    )
    return _deletion(value, corpus_id, operation_id)
