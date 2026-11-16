"""Unwired, deadline-bound transfer from an owner-authorized private Hub repo.

The caller must verify the session and authorize admission before this operation;
this adapter does not perform that check. The sink must be trusted, nonblocking,
and cancellation-cooperative. Bytes already delivered cannot be recalled.
The deadline allows up to 0.1 seconds each for response and client cleanup;
those bounds assume cooperative HTTPX transports.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
from collections.abc import Awaitable, Callable
from urllib.parse import quote, urljoin
from uuid import UUID

import httpx
from pydantic import ValidationError

from corpora_api.corpus_hub_read import (
    _HUB,
    _MAX_FILE,
    _MAX_METADATA,
    _MAX_REDIRECTS,
    CorpusHubReadError,
    _file_path,
    _request,
    _safe_url,
)
from corpora_api.corpus_metadata import CorpusMetadata, CorpusMetadataError, CorpusMetadataStore

_MAX_REGISTRY_RESPONSE = 4_000_000
_CLOSE_TIMEOUT = 0.1


class _SinkFailure(Exception):
    def __init__(self, error: Exception) -> None:
        self.error = error


async def _close(
    resource: httpx.Response | httpx.AsyncClient, *, suppress_errors: bool = False
) -> None:
    # Give cooperative HTTPX transports a short, bounded cleanup allowance.
    try:
        await asyncio.wait_for(resource.aclose(), timeout=_CLOSE_TIMEOUT)
    except Exception:
        if not suppress_errors:
            raise CorpusHubReadError("Corpus transfer cleanup failed") from None


async def _read_limited(response: httpx.Response, maximum: int) -> bytes:
    data = bytearray()
    async for chunk in response.aiter_raw():
        data.extend(chunk)
        if len(data) > maximum:
            raise CorpusHubReadError("Corpus metadata exceeds size limit")
    return bytes(data)


async def _get_metadata(
    client: httpx.AsyncClient, verified_user: str, corpus_id: str
) -> CorpusMetadata:
    actor = CorpusMetadataStore._actor(verified_user)
    try:
        identifier = str(UUID(corpus_id))
    except (ValueError, TypeError, AttributeError) as error:
        raise CorpusHubReadError("Corpus not found") from error
    params = {"id": "eq." + identifier, "limit": "1"}
    admin = CorpusMetadataStore._is_superadmin(actor)
    if not admin:
        params["owner_id"] = "eq." + actor
    url, headers = CorpusMetadataStore._config()
    headers["Accept-Encoding"] = "identity"
    request = httpx.Request("GET", httpx.URL(url, params=params), headers=headers)
    response = await client.send(request, stream=True, auth=None, follow_redirects=False)
    failed = False
    try:
        if (
            response.status_code != 200
            or response.headers.get("content-encoding", "identity") != "identity"
        ):
            raise CorpusHubReadError("Corpus metadata is unavailable")
        values = json.loads(await _read_limited(response, _MAX_REGISTRY_RESPONSE))
        if not isinstance(values, list) or len(values) != 1:
            raise CorpusHubReadError("Corpus not found")
        row = CorpusMetadata.model_validate(values[0])
        if str(row.id) != identifier or (str(row.owner_id) != actor and not admin):
            raise CorpusHubReadError("Corpus metadata is invalid")
        CorpusMetadataStore._check_link(row)
        return row
    except BaseException:
        failed = True
        raise
    finally:
        await _close(response, suppress_errors=failed)


class DeadlineCorpusHubReader:
    """Transfer a private file within one deadline, without a detached stream."""

    def __init__(
        self, transport_factory: Callable[[], httpx.AsyncBaseTransport] | None = None
    ) -> None:
        self._transport_factory = transport_factory

    async def transfer(
        self,
        verified_user: str,
        corpus_id: str,
        revision: str,
        path: str,
        *,
        max_bytes: int,
        timeout: float,
        sink: Callable[[bytes], Awaitable[None]],
    ) -> None:
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or not 0 < timeout <= 300
        ):
            raise CorpusHubReadError("Invalid corpus transfer deadline")
        ends_at = asyncio.get_running_loop().time() + timeout
        transport = self._transport_factory() if self._transport_factory else None
        client = httpx.AsyncClient(transport=transport)
        failed = False
        try:
            remaining = ends_at - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise CorpusHubReadError("Corpus transfer timed out")
            await self._transfer(
                client,
                verified_user,
                corpus_id,
                revision,
                path,
                max_bytes=max_bytes,
                timeout=remaining,
                sink=sink,
            )
        except BaseException:
            failed = True
            raise
        finally:
            await _close(client, suppress_errors=failed)

    async def _transfer(
        self,
        client: httpx.AsyncClient,
        verified_user: str,
        corpus_id: str,
        revision: str,
        path: str,
        *,
        max_bytes: int,
        timeout: float,
        sink: Callable[[bytes], Awaitable[None]],
    ) -> None:
        if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{40}", revision):
            raise CorpusHubReadError("Invalid corpus revision")
        if type(max_bytes) is not int or not 0 < max_bytes <= _MAX_FILE:
            raise CorpusHubReadError("Invalid corpus file size limit")
        file_path = _file_path(path)
        deadline = asyncio.timeout(timeout)
        try:
            async with deadline:
                record = await _get_metadata(client, verified_user, corpus_id)
                if record.state != "ready":
                    raise CorpusHubReadError("Corpus repository is not ready")
                token = os.getenv("HF_CORPUS_READ_TOKEN", "")
                if not token:
                    raise CorpusHubReadError("Corpus repository access is unavailable")
                org, name = record.hf_repo_id.split("/", 1)
                repo_path = f"{quote(org, safe='')}/{quote(name, safe='')}"
                kind = "datasets" if record.hf_repo_type == "dataset" else "models"
                metadata_url = (
                    f"{_HUB}/api/{kind}/{repo_path}/revision/{revision}?expand=sha&expand=private"
                )
                response = await client.send(
                    _request(
                        metadata_url,
                        {"Authorization": "Bearer " + token, "Accept-Encoding": "identity"},
                    ),
                    stream=True,
                    auth=None,
                    follow_redirects=False,
                )
                failed = False
                try:
                    if (
                        response.status_code != 200
                        or response.headers.get("content-encoding", "identity") != "identity"
                    ):
                        raise CorpusHubReadError("Corpus repository could not be verified")
                    info = json.loads(await _read_limited(response, _MAX_METADATA))
                    if (
                        not isinstance(info, dict)
                        or info.get("id") != record.hf_repo_id
                        or info.get("sha") != revision
                        or info.get("private") is not True
                    ):
                        raise CorpusHubReadError("Corpus repository could not be verified")
                except BaseException:
                    failed = True
                    raise
                finally:
                    await _close(response, suppress_errors=failed)
                prefix = "/datasets" if record.hf_repo_type == "dataset" else ""
                target = f"{_HUB}{prefix}/{repo_path}/resolve/{revision}/{file_path}"
                for attempt in range(_MAX_REDIRECTS + 1):
                    target, is_hub = _safe_url(target)
                    headers = {"Accept-Encoding": "identity"}
                    if is_hub:
                        headers["Authorization"] = "Bearer " + token
                    response = await client.send(
                        _request(target, headers), stream=True, auth=None, follow_redirects=False
                    )
                    failed = False
                    try:
                        if response.status_code in (301, 302, 303, 307, 308):
                            location = response.headers.get("location")
                            if not location or attempt == _MAX_REDIRECTS:
                                raise CorpusHubReadError("Corpus file redirect failed")
                            target = urljoin(target, location)
                            continue
                        if response.status_code != 200:
                            raise CorpusHubReadError("Corpus file is unavailable")
                        if response.headers.get("content-encoding", "identity") != "identity":
                            raise CorpusHubReadError("Corpus file encoding is unsupported")
                        length = response.headers.get("content-length")
                        if length is not None and (
                            not length.isdecimal() or int(length) > max_bytes
                        ):
                            raise CorpusHubReadError("Corpus file exceeds size limit")
                        size = 0
                        async for chunk in response.aiter_raw():
                            size += len(chunk)
                            if size > max_bytes:
                                raise CorpusHubReadError("Corpus file exceeds size limit")
                            try:
                                await sink(chunk)
                            except Exception as error:
                                raise _SinkFailure(error) from error
                        return
                    except BaseException:
                        failed = True
                        raise
                    finally:
                        await _close(response, suppress_errors=failed)
        except _SinkFailure as error:
            raise error.error from None
        except TimeoutError:
            if deadline.expired():
                raise CorpusHubReadError("Corpus transfer timed out") from None
            raise CorpusHubReadError("Corpus transfer cleanup failed") from None
        except (
            httpx.HTTPError,
            CorpusMetadataError,
            ValueError,
            TypeError,
            ValidationError,
            RecursionError,
        ):
            raise CorpusHubReadError("Corpus repository access failed") from None
