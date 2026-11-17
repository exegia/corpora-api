"""Inspect a stuck provisioning attempt without changing it or adopting a repo.

An observation cannot prove who created the repository or that a worker has
stopped. In particular, an inaccessible repository is not proof of absence.
"""

import asyncio
import json
import math
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import quote
from uuid import UUID

import httpx

from corpora_api.corpus_metadata import (
    CorpusMetadata,
    CorpusMetadataConflict,
    CorpusMetadataError,
    CorpusMetadataStore,
)


@dataclass(frozen=True)
class ProvisionObservationTarget:
    operation_id: UUID
    claim_id: UUID
    corpus_id: UUID
    repo_id: str
    repo_type: Literal["dataset", "model"]
    state: Literal["executing", "uncertain"]


@dataclass(frozen=True)
class ProvisionObservation:
    target: ProvisionObservationTarget
    status: Literal["private_repository_observed", "identity_or_privacy_mismatch", "inconclusive"]
    observed_head_sha: str | None = None


class CorpusProvisionObservationLookup:
    """Load an internal operation snapshot; this does not authorize a worker."""

    def __init__(self, connect: Callable[[], Any], *, assume_service_role: bool = False) -> None:
        self.connect = connect
        self.assume_service_role = assume_service_role

    def load_trusted(self, operation_id: UUID) -> ProvisionObservationTarget:
        if not isinstance(operation_id, UUID):
            raise CorpusMetadataError("Invalid provisioning operation")
        try:
            with self.connect() as db:
                with db.transaction():
                    if self.assume_service_role:
                        db.execute("SET LOCAL ROLE service_role")
                    db.execute("SELECT pg_catalog.set_config('statement_timeout','5000',true)")
                    result = db.execute(
                        "SELECT row_to_json(o),row_to_json(m) "
                        "FROM public.corpora_corpus_provision_ops o "
                        "JOIN public.corpora_corpus_metadata m ON m.id=o.corpus_id "
                        "WHERE o.id=%s::uuid AND o.state IN ('executing','uncertain') "
                        "AND o.worker_claim IS NOT NULL",
                        (str(operation_id),),
                    ).fetchone()
                    if result is None:
                        raise CorpusMetadataConflict("Provisioning observation is unavailable")
                    row, raw_metadata = result
                    metadata = CorpusMetadata.model_validate(raw_metadata)
                    CorpusMetadataStore._check_link(metadata)
                    if (
                        not isinstance(row, dict)
                        or UUID(row["id"]) != operation_id
                        or UUID(row["corpus_id"]) != metadata.id
                        or row["repo_id"] != metadata.hf_repo_id
                        or row["repo_type"] != metadata.hf_repo_type
                        or row["repo_type"] not in ("dataset", "model")
                        or row["state"] not in ("executing", "uncertain")
                        or metadata.state != "provisioning"
                    ):
                        raise ValueError("Invalid operation snapshot")
                    target = ProvisionObservationTarget(
                        operation_id,
                        UUID(row["worker_claim"]),
                        metadata.id,
                        metadata.hf_repo_id,
                        row["repo_type"],
                        row["state"],
                    )
            return target
        except CorpusMetadataConflict:
            raise
        except Exception:
            raise CorpusMetadataError("Provisioning observation lookup failed") from None


class CorpusProvisionHubObserver:
    """Read private-repository details with no mutation or retry.

    Call the synchronous entry point from the dispatcher's worker thread. Each
    observation owns its async client, so cancellation closes the response and
    connection before returning. Cooperative HTTPX transports get an additional
    0.1 seconds each for response and client cleanup. Client/transport construction
    must not block; this is not a process-level deadline.
    """

    def __init__(
        self,
        token: str,
        *,
        timeout: float = 15,
        transport_factory: Callable[[], httpx.AsyncBaseTransport] | None = None,
    ) -> None:
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or not 0 < timeout <= 15
        ):
            raise ValueError("Invalid provisioning observation timeout")
        self._token = token
        self._timeout = timeout
        self._transport_factory = transport_factory

    def observe_trusted(self, target: ProvisionObservationTarget) -> ProvisionObservation:
        if not isinstance(target, ProvisionObservationTarget) or not self._token:
            raise CorpusMetadataError("Invalid provisioning observation")
        # Revalidate even internally constructed targets; never send the token to an arbitrary host.
        try:
            organization, name = target.repo_id.split("/")
            if (
                target.repo_type not in ("dataset", "model")
                or target.state not in ("executing", "uncertain")
                or name != f"corpus-{target.corpus_id}"
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", organization)
            ):
                raise ValueError("Invalid target")
            kind = "datasets" if target.repo_type == "dataset" else "models"
            url = (
                f"https://huggingface.co/api/{kind}/"
                f"{quote(organization, safe='')}/{quote(name, safe='')}"
            )
            body = asyncio.run(self._read(url))
            info = json.loads(body)
            if not isinstance(info, dict):
                return ProvisionObservation(target, "inconclusive")
            if info.get("id") != target.repo_id or info.get("private") is not True:
                return ProvisionObservation(target, "identity_or_privacy_mismatch")
            sha = info.get("sha")
            if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha):
                return ProvisionObservation(target, "inconclusive")
            return ProvisionObservation(target, "private_repository_observed", sha)
        except (
            httpx.HTTPError,
            TimeoutError,
            ValueError,
            TypeError,
            AttributeError,
            RecursionError,
        ):
            return ProvisionObservation(target, "inconclusive")

    async def _read(self, url: str) -> bytes:
        ends_at = asyncio.get_running_loop().time() + self._timeout
        transport = self._transport_factory() if self._transport_factory else None
        client = httpx.AsyncClient(transport=transport)
        response = None
        try:
            if asyncio.get_running_loop().time() >= ends_at:
                raise TimeoutError("Repository observation deadline expired")
            async with asyncio.timeout_at(ends_at):
                request = httpx.Request(
                    "GET",
                    url,
                    headers={
                        "Authorization": "Bearer " + self._token,
                        "Accept-Encoding": "identity",
                    },
                )
                response = await client.send(
                    request, stream=True, auth=None, follow_redirects=False
                )
                if (
                    response.status_code != 200
                    or response.headers.get("content-encoding", "identity") != "identity"
                ):
                    raise ValueError("Repository observation is unavailable")
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > 65536:
                        raise ValueError("Repository observation exceeds size limit")
                return bytes(body)
        except Exception:
            # HTTPX may close the stream itself at EOF; failures from that
            # implicit cleanup are inconclusive too. Preserve cancellation.
            raise ValueError("Repository observation failed") from None
        finally:
            # Cleanup also runs on cancellation. A cleanup failure makes the
            # observation inconclusive, even if a valid body was received.
            try:
                if response is not None:
                    await self._close(response)
            finally:
                await self._close(client)

    @staticmethod
    async def _close(resource: httpx.Response | httpx.AsyncClient) -> None:
        try:
            await asyncio.wait_for(resource.aclose(), timeout=0.1)
        except Exception:
            raise ValueError("Repository observation cleanup failed") from None


class CorpusProvisionReconciler:
    """Connect a stored claim to a read-only Hub observation.

    Reconciliation reports evidence for an operator; it never changes the
    claim or treats a matching repository as proof of its creation.
    """

    def __init__(
        self, lookup: CorpusProvisionObservationLookup, observer: CorpusProvisionHubObserver
    ) -> None:
        self.lookup = lookup
        self.observer = observer

    def inspect_trusted(self, operation_id: UUID) -> ProvisionObservation:
        target = self.lookup.load_trusted(operation_id)
        observation = self.observer.observe_trusted(target)
        if self.lookup.load_trusted(operation_id) != target:
            raise CorpusMetadataConflict("Provisioning changed during observation")
        return observation
