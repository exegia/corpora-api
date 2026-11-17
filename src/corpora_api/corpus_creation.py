"""Authenticated, one-shot creation backed by restricted Supabase RPCs."""

import asyncio
import json
import os
from collections.abc import Callable, Mapping
from typing import Any, Literal
from uuid import UUID

import httpx
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator

from corpora_api.corpus_hub_provision import CorpusHubProvisionUncertain
from corpora_api.corpus_metadata import (
    CorpusMetadata,
    CorpusMetadataConflict,
    CorpusMetadataError,
    CorpusMetadataNotFound,
    CorpusMetadataStore,
    _normalize_reservation,
)
from corpora_api.corpus_provision_dispatch import CorpusProvisionDispatcher
from corpora_api.corpus_provision_finalize import TrustedBootstrapAttestation
from corpora_api.corpus_provision_observation import (
    CorpusProvisionHubObserver,
    CorpusProvisionReconciler,
    ProvisionObservationTarget,
)
from corpora_api.session_corpus_provision import ClaimedCorpusProvision
from corpora_api.timed_corpus_hub_provision import _validate_registry_url


class CorpusCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    id: UUID
    title: str = Field(min_length=1, max_length=512, pattern=r"^[^\x00]+$")


class CorpusCreationInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: UUID
    title: str = Field(min_length=1, max_length=512)
    state: Literal["reserved", "provisioning", "repository_created", "needs_reconciliation"]
    bootstrap_sha: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")

    @model_validator(mode="after")
    def check_completed(self) -> "CorpusCreationInfo":
        if (self.state == "repository_created") != (self.bootstrap_sha is not None):
            raise ValueError("Creation receipt does not match its state")
        return self


class ExistingCreation(Exception):
    def __init__(self, info: CorpusCreationInfo):
        self.info = info


def _session(claims: Mapping[str, Any]) -> dict[str, Any]:
    try:
        actor, session = str(UUID(claims["sub"])), str(UUID(claims["session_id"]))
        expiry = claims["exp"]
        if type(expiry) is not int or not 0 < expiry < 2**63:
            raise ValueError("Invalid expiry")
        return {"p_actor": actor, "p_session": session, "p_token_exp": expiry}
    except (ValueError, TypeError, KeyError, AttributeError):
        raise HTTPException(401, "Verified session is required") from None


def _info(value: Any, corpus_id: str) -> CorpusCreationInfo:
    if value == {"status": "not_found"}:
        raise CorpusMetadataNotFound("Corpus not found")
    if value == {"status": "conflict"}:
        raise CorpusMetadataConflict("Corpus creation conflicts with an existing request")
    try:
        if not isinstance(value, dict) or value.get("status") != "existing":
            raise ValueError("Invalid status")
        info = CorpusCreationInfo.model_validate({k: v for k, v in value.items() if k != "status"})
        if info.id != UUID(corpus_id):
            raise ValueError("Mismatched corpus")
        return info
    except (ValueError, TypeError):
        raise CorpusMetadataError("Corpus creation returned invalid data") from None


class CreationRPC:
    """No automatic retries: a lost admission response must not dispatch work."""

    def __init__(self, transport_factory: Callable[[], httpx.AsyncBaseTransport] | None = None):
        self.transport_factory = transport_factory

    def call(self, name: str, params: dict[str, Any]) -> Any:
        return asyncio.run(self._call(name, params))

    async def _call(self, name: str, params: dict[str, Any]) -> Any:
        url, headers = CorpusMetadataStore._config()
        _validate_registry_url(url)
        url = url.rsplit("/", 1)[0] + "/rpc/" + name
        headers["Accept-Encoding"] = "identity"
        transport = self.transport_factory() if self.transport_factory else None
        client = httpx.AsyncClient(transport=transport)
        response = None
        try:
            async with asyncio.timeout(10):
                response = await client.send(
                    httpx.Request("POST", url, headers=headers, json=params),
                    stream=True,
                    auth=None,
                    follow_redirects=False,
                )
                if response.headers.get("content-encoding", "identity") != "identity":
                    raise ValueError("Unsupported response")
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > 65536:
                        raise ValueError("Response exceeds limit")
                value = json.loads(body)
                if response.status_code != 200:
                    if isinstance(value, dict) and value.get("code") == "P0001":
                        if value.get("message") == "AUTH_SESSION_INVALID":
                            raise HTTPException(401, "Session is no longer active")
                    raise ValueError("RPC failed")
                return value
        except HTTPException:
            raise
        except Exception:
            raise CorpusMetadataError("Corpus creation service is unavailable") from None
        finally:
            try:
                try:
                    if response is not None:
                        await CorpusProvisionHubObserver._close(response)
                finally:
                    await CorpusProvisionHubObserver._close(client)
            except Exception:
                raise CorpusMetadataError("Corpus creation service cleanup failed") from None


class CreationAdmission:
    def __init__(self, rpc: CreationRPC, title: str):
        self.rpc, self.title = rpc, title

    def claim(self, verified_claims: Mapping[str, Any], corpus_id: str) -> ClaimedCorpusProvision:
        params = _session(verified_claims)
        identifier, title, repo = _normalize_reservation(corpus_id, self.title, "dataset")
        value = self.rpc.call(
            "corpora_begin_creation",
            {
                **params,
                "p_corpus": str(identifier),
                "p_title": title,
                "p_organization": os.environ["HF_CORPUS_ORGANIZATION"],
            },
        )
        if not isinstance(value, dict) or value.get("status") != "claimed":
            info = _info(value, corpus_id)
            if info.title != title:
                raise CorpusMetadataConflict("Corpus creation input changed")
            raise ExistingCreation(info)
        try:
            if (
                set(value)
                != {
                    "status",
                    "id",
                    "title",
                    "operation_id",
                    "claim_id",
                    "repo_id",
                    "repo_type",
                    "revision",
                }
                or UUID(value["id"]) != identifier
                or value["title"] != title
                or value["repo_id"] != repo
                or value["repo_type"] != "dataset"
                or type(value["revision"]) is not int
                or value["revision"] < 1
            ):
                raise ValueError("Invalid admission")
            return ClaimedCorpusProvision(
                UUID(value["operation_id"]),
                UUID(value["claim_id"]),
                identifier,
                UUID(params["p_actor"]),
                UUID(params["p_session"]),
                repo,
                "dataset",
                value["revision"],
            )
        except (ValueError, TypeError, KeyError, AttributeError):
            raise CorpusMetadataError("Corpus creation admission failed") from None


class CreationOutcomes:
    def __init__(self, rpc: CreationRPC):
        self.rpc = rpc

    def record_trusted(self, operation_id: UUID, claim_id: UUID) -> None:
        if (
            self.rpc.call(
                "corpora_mark_creation_uncertain",
                {
                    "p_operation": str(operation_id),
                    "p_claim": str(claim_id),
                },
            )
            is not True
        ):
            raise CorpusMetadataConflict("Creation claim is unavailable")

    def load_trusted(self, operation_id: UUID) -> ProvisionObservationTarget:
        value = self.rpc.call("corpora_observe_provision", {"p_operation": str(operation_id)})
        try:
            row = value["operation"]
            metadata = CorpusMetadata.model_validate(value["metadata"])
            CorpusMetadataStore._check_link(metadata)
            if (
                UUID(row["id"]) != operation_id
                or UUID(row["corpus_id"]) != metadata.id
                or row["repo_id"] != metadata.hf_repo_id
                or row["repo_type"] != metadata.hf_repo_type
                or row["state"] not in ("executing", "uncertain")
                or metadata.state != "provisioning"
            ):
                raise ValueError("Mismatched operation")
            return ProvisionObservationTarget(
                operation_id,
                UUID(row["worker_claim"]),
                metadata.id,
                metadata.hf_repo_id,
                metadata.hf_repo_type,
                row["state"],
            )
        except (ValueError, TypeError, KeyError, AttributeError):
            raise CorpusMetadataError("Creation observation is unavailable") from None

    @staticmethod
    def _candidate(candidate: TrustedBootstrapAttestation) -> dict[str, str]:
        return {
            "p_corpus": str(candidate.corpus_id),
            "p_operation": str(candidate.operation_id),
            "p_claim": str(candidate.claim_id),
            "p_repo": candidate.repo_id,
            "p_type": candidate.repo_type,
            "p_sha": candidate.commit_sha,
        }

    def finalize_trusted(self, candidate: TrustedBootstrapAttestation) -> str:
        if self.rpc.call("corpora_finalize_creation", self._candidate(candidate)) is not True:
            raise CorpusMetadataConflict("Creation finalization requires reconciliation")
        return candidate.commit_sha

    def find_finalized_trusted(self, candidate: TrustedBootstrapAttestation) -> str | None:
        value = self.rpc.call("corpora_find_creation_result", self._candidate(candidate))
        if value is not None and value != candidate.commit_sha:
            raise CorpusMetadataError("Creation receipt does not match")
        return value


def require_creation_config(*, creating: bool = False) -> None:
    if os.getenv("CORPORA_CORPUS_CREATION_ENABLED") != "1":
        raise HTTPException(503, "Corpus creation is not enabled")
    if creating and (os.name != "posix" or not os.getenv("HF_CORPUS_PROVISION_TOKEN")):
        raise HTTPException(503, "Corpus creation is not configured")
    CorpusMetadataStore._config()


def create_corpus(
    claims: Mapping[str, Any], body: CorpusCreateRequest
) -> tuple[CorpusCreationInfo, int]:
    require_creation_config(creating=True)
    identifier, title, _ = _normalize_reservation(str(body.id), body.title, "dataset")
    rpc = CreationRPC()
    outcomes = CreationOutcomes(rpc)
    dispatcher = CorpusProvisionDispatcher(
        CreationAdmission(rpc, title),
        outcomes,
        CorpusProvisionReconciler(
            outcomes, CorpusProvisionHubObserver(os.environ["HF_CORPUS_PROVISION_TOKEN"])
        ),
        outcomes,
    )
    try:
        result = dispatcher.dispatch_trusted(claims, str(identifier))
    except ExistingCreation as existing:
        status = 202 if existing.info.state in ("reserved", "provisioning") else 200
        return existing.info, status
    except CorpusHubProvisionUncertain:
        # The operation remains reserved. A fresh authenticated GET determines
        # whether finalization committed; do not redispatch or adopt a repo.
        raise HTTPException(503, "Creation outcome is uncertain; check corpus status") from None
    return CorpusCreationInfo(
        id=identifier,
        title=title,
        state="repository_created",
        bootstrap_sha=result.bootstrap_sha,
    ), 201


def get_corpus_creation(claims: Mapping[str, Any], corpus_id: UUID) -> CorpusCreationInfo:
    require_creation_config()
    value = CreationRPC().call(
        "corpora_get_creation", {**_session(claims), "p_corpus": str(corpus_id)}
    )
    return _info(value, str(corpus_id))
