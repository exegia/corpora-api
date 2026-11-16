"""Unwired, owner-bound reads from a private corpus Hub repository.

The caller must enforce a verified live session until the response stream closes.
No route uses this adapter; corpus readiness and revision authority are separate.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterator
from contextlib import contextmanager
from urllib.parse import quote, urljoin, urlsplit

import httpx

from corpora_api.corpus_metadata import CorpusMetadataStore

_HUB = "https://huggingface.co"
_MAX_METADATA = 65_536
_MAX_REDIRECTS = 4
_MAX_FILE = (1 << 63) - 1
_IO_TIMEOUT = 15.0


class CorpusHubReadError(Exception):
    """A private corpus file could not safely be retrieved."""


def _file_path(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 1024 or "\\" in value:
        raise CorpusHubReadError("Invalid corpus file path")
    parts = value.split("/")
    if any(not part or part in {".", ".."} for part in parts):
        raise CorpusHubReadError("Invalid corpus file path")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise CorpusHubReadError("Invalid corpus file path")
    try:
        return "/".join(quote(part, safe="") for part in parts)
    except UnicodeError as error:
        raise CorpusHubReadError("Invalid corpus file path") from error


def _safe_url(value: str) -> tuple[str, bool]:
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").lower()
        allowed = host == "huggingface.co" or host.endswith((".huggingface.co", ".hf.co"))
        if (
            parsed.scheme != "https"
            or not allowed
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in (None, 443)
            or parsed.fragment
        ):
            raise ValueError("Unsafe Hub URL")
        return value, host == "huggingface.co"
    except (ValueError, TypeError) as error:
        raise CorpusHubReadError("Corpus file redirect is unsafe") from error


def _request(url: str, headers: dict[str, str]) -> httpx.Request:
    timeout = {key: _IO_TIMEOUT for key in ("connect", "read", "write", "pool")}
    return httpx.Request("GET", url, headers=headers, extensions={"timeout": timeout})


class CorpusHubReader:
    """Read an explicit immutable commit after metadata authorization."""

    def __init__(self, metadata: CorpusMetadataStore, client: httpx.Client) -> None:
        self.metadata = metadata
        self.client = client

    @contextmanager
    def _open(
        self, verified_user: str, corpus_id: str, revision: str, path: str, *, max_bytes: int
    ) -> Iterator[Iterator[bytes]]:
        if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{40}", revision):
            raise CorpusHubReadError("Invalid corpus revision")
        if type(max_bytes) is not int or not 0 < max_bytes <= _MAX_FILE:
            raise CorpusHubReadError("Invalid corpus file size limit")
        file_path = _file_path(path)
        record = self.metadata.get(verified_user, corpus_id)
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
        response = None
        yielded = False
        try:
            request = _request(
                metadata_url,
                {"Authorization": "Bearer " + token, "Accept-Encoding": "identity"},
            )
            response = self.client.send(request, stream=True, auth=None, follow_redirects=False)
            if (
                response.status_code != 200
                or response.headers.get("content-encoding", "identity") != "identity"
            ):
                raise CorpusHubReadError("Corpus repository could not be verified")
            body = bytearray()
            for chunk in response.iter_raw(chunk_size=8192):
                body.extend(chunk)
                if len(body) > _MAX_METADATA:
                    raise CorpusHubReadError("Corpus repository metadata is too large")
            info = json.loads(body)
            if (
                not isinstance(info, dict)
                or info.get("id") != record.hf_repo_id
                or info.get("sha") != revision
                or info.get("private") is not True
            ):
                raise CorpusHubReadError("Corpus repository could not be verified")
            response.close()
            response = None
            prefix = "/datasets" if record.hf_repo_type == "dataset" else ""
            target = f"{_HUB}{prefix}/{repo_path}/resolve/{revision}/{file_path}"
            for attempt in range(_MAX_REDIRECTS + 1):
                target, is_hub = _safe_url(target)
                headers = {"Accept-Encoding": "identity"}
                if is_hub:
                    headers["Authorization"] = "Bearer " + token
                response = self.client.send(
                    _request(target, headers),
                    stream=True,
                    auth=None,
                    follow_redirects=False,
                )
                if response.status_code not in (301, 302, 303, 307, 308):
                    break
                location = response.headers.get("location")
                response.close()
                response = None
                if not location or attempt == _MAX_REDIRECTS:
                    raise CorpusHubReadError("Corpus file redirect failed")
                target = urljoin(target, location)
            if response is None or response.status_code != 200:
                raise CorpusHubReadError("Corpus file is unavailable")
            if response.headers.get("content-encoding", "identity") != "identity":
                raise CorpusHubReadError("Corpus file encoding is unsupported")
            length = response.headers.get("content-length")
            if length is not None and (not length.isdecimal() or int(length) > max_bytes):
                raise CorpusHubReadError("Corpus file exceeds size limit")

            def chunks() -> Iterator[bytes]:
                size = 0
                try:
                    for chunk in response.iter_raw(chunk_size=65_536):
                        size += len(chunk)
                        if size > max_bytes:
                            raise CorpusHubReadError("Corpus file exceeds size limit")
                        yield chunk
                except httpx.HTTPError:
                    # Upstream exceptions can include signed CDN URLs.
                    raise CorpusHubReadError("Corpus file download failed") from None

            yielded = True
            yield chunks()
        except CorpusHubReadError:
            raise
        except (httpx.HTTPError, ValueError, TypeError, RecursionError):
            if yielded:
                raise
            raise CorpusHubReadError("Corpus repository access failed") from None
        finally:
            if response is not None:
                response.close()
