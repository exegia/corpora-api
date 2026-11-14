"""Reads from the existing Hub or owner-scoped Supabase corpus library."""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from urllib.parse import quote

import httpx
from huggingface_hub import HfApi
from huggingface_hub.errors import (
    BucketNotFoundError,
    EntryNotFoundError,
    HfHubHTTPError,
    RepositoryNotFoundError,
)

HUB_ACCESS_ERROR = "HF_TOKEN cannot access the configured Hub storage"


class StorageError(Exception):
    """The backing store failed or returned an invalid response."""


class StorageNotConfigured(StorageError):
    """The corpus bucket cannot be reached with this deployment's settings."""


class StoredCorpusNotFound(StorageError):
    """The requested archive was not found in the selected storage backend."""


@dataclass(frozen=True)
class StoredCorpus:
    filename: str
    size_bytes: int | None
    repo_id: str
    url: str


def archive_name(filename: str) -> str:
    """Keep a caller-supplied archive name inside one flat storage location."""
    if (
        not filename
        or filename in {".", ".."}
        or any(char in filename for char in ("/", "\\", "\x00", "\r", "\n"))
    ):
        raise StoredCorpusNotFound("Corpus archive not found")
    return filename if filename.endswith((".corpus", ".cusx")) else f"{filename}.corpus"


class SupabaseCorpusStorage:
    """Read flat archive names beneath the verified user's bucket prefix."""

    _PAGE_SIZE = 1000
    _SUFFIXES = (".corpus", ".cusx")

    def __init__(self, client: httpx.Client | None = None) -> None:
        self.client = client or httpx.Client(timeout=30)

    @staticmethod
    def _config() -> tuple[str, str, str]:
        project_ref = os.getenv("PROJECT_REF")
        url = os.getenv("SUPABASE_URL") or (
            f"https://{project_ref}.supabase.co" if project_ref else ""
        )
        key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
        bucket = os.getenv("SUPABASE_STORAGE_BUCKET", "")
        if not (url and key and bucket):
            raise StorageNotConfigured(
                "Corpus storage requires SUPABASE_URL (or PROJECT_REF), "
                "SUPABASE_SERVICE_ROLE_KEY, and SUPABASE_STORAGE_BUCKET"
            )
        return url.rstrip("/"), key, bucket

    @staticmethod
    def _owner(user_id: str) -> str:
        try:
            return str(uuid.UUID(user_id))
        except ValueError as error:
            raise StorageError("Verified user identity is not a UUID") from error

    @staticmethod
    def _headers(key: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {key}", "apikey": key}

    @staticmethod
    def _object_url(url: str, bucket: str, owner: str, name: str) -> str:
        parts = (bucket, owner, name)
        return f"{url}/storage/v1/object/" + "/".join(quote(part, safe="") for part in parts)

    def _request(self, method: str, url: str, **kwargs: object) -> httpx.Response:
        try:
            return self.client.request(method, url, **kwargs)
        except httpx.RequestError as error:
            raise StorageError("Corpus storage request failed") from error

    def list(self, user_id: str) -> list[StoredCorpus]:
        url, key, bucket = self._config()
        owner = self._owner(user_id)
        stored: list[StoredCorpus] = []
        offset = 0
        while True:
            response = self._request(
                "POST",
                f"{url}/storage/v1/object/list/{quote(bucket, safe='')}",
                json={"prefix": owner, "limit": self._PAGE_SIZE, "offset": offset},
                headers=self._headers(key),
            )
            if response.status_code in (400, 404) and offset == 0:
                return []
            if response.status_code != 200:
                raise StorageError("Could not list corpus archives")
            try:
                entries = response.json()
            except ValueError as error:
                raise StorageError("Corpus storage returned invalid JSON") from error
            if not isinstance(entries, list):
                raise StorageError("Corpus storage returned an invalid list")
            for entry in entries:
                if not isinstance(entry, dict) or entry.get("id") is None:
                    continue
                name = entry.get("name")
                if (
                    not isinstance(name, str)
                    or "/" in name
                    or "\\" in name
                    or not name.endswith(self._SUFFIXES)
                ):
                    continue
                metadata = entry.get("metadata") or {}
                size = metadata.get("size") if isinstance(metadata, dict) else None
                size_bytes = size if isinstance(size, int) and not isinstance(size, bool) else None
                stored.append(
                    StoredCorpus(
                        filename=name,
                        size_bytes=size_bytes,
                        repo_id=bucket,
                        url=self._object_url(url, bucket, owner, name),
                    )
                )
            if len(entries) < self._PAGE_SIZE:
                return stored
            offset += self._PAGE_SIZE

    def info(self, user_id: str, filename: str) -> StoredCorpus:
        name = archive_name(filename)
        for stored in self.list(user_id):
            if stored.filename == name:
                return stored
        raise StoredCorpusNotFound(f"No corpus named {name!r}")

    def open_download(self, user_id: str, filename: str) -> tuple[httpx.Response, str]:
        url, key, bucket = self._config()
        owner = self._owner(user_id)
        name = archive_name(filename)
        try:
            response = self.client.send(
                self.client.build_request(
                    "GET",
                    self._object_url(url, bucket, owner, name),
                    headers=self._headers(key),
                ),
                stream=True,
            )
        except httpx.RequestError as error:
            raise StorageError("Corpus storage request failed") from error
        if response.status_code in (400, 404):
            response.close()
            raise StoredCorpusNotFound(f"No corpus named {name!r}")
        if response.status_code != 200:
            response.close()
            raise StorageError("Could not download corpus archive")
        return response, name


class HubCorpusStorage:
    """Read the shared Hub repository with the same metadata as the legacy API."""

    def __init__(self, api: HfApi | None = None, client: httpx.Client | None = None) -> None:
        token = os.getenv("HF_TOKEN")
        self.api = api or HfApi(token=token or False)
        self.client = client or httpx.Client(timeout=30, follow_redirects=True)

    @staticmethod
    def _config() -> tuple[str, str, str | None]:
        repo_id = os.getenv("HF_STORAGE_REPO", "")
        repo_type = os.getenv("HF_STORAGE_REPO_TYPE", "bucket")
        if not repo_id or repo_type not in {"bucket", "model", "dataset", "space"}:
            raise StorageNotConfigured(
                "Hub storage requires HF_STORAGE_REPO and a valid HF_STORAGE_REPO_TYPE"
            )
        return repo_id, repo_type, os.getenv("HF_TOKEN")

    @staticmethod
    def _url(repo_id: str, repo_type: str, filename: str) -> str:
        path = quote(filename, safe="/")
        if repo_type == "bucket":
            return f"https://huggingface.co/buckets/{repo_id}/resolve/{path}"
        prefix = "" if repo_type == "model" else f"{repo_type}s/"
        return f"https://huggingface.co/{prefix}{repo_id}/resolve/main/{path}"

    def list(self, _user_id: str) -> list[StoredCorpus]:
        repo_id, repo_type, _token = self._config()
        try:
            if repo_type == "bucket":
                entries = self.api.list_bucket_tree(repo_id, recursive=True)
            else:
                entries = self.api.list_repo_tree(repo_id, repo_type=repo_type, recursive=True)
            return [
                StoredCorpus(
                    filename=entry.path,
                    size_bytes=getattr(entry, "size", None),
                    repo_id=repo_id,
                    url=self._url(repo_id, repo_type, entry.path),
                )
                for entry in entries
                if entry.path.endswith((".corpus", ".cusx"))
            ]
        except (RepositoryNotFoundError, BucketNotFoundError) as error:
            if getattr(error.response, "status_code", None) in (401, 403):
                raise StorageNotConfigured(HUB_ACCESS_ERROR) from error
            return []
        except HfHubHTTPError as error:
            if getattr(error.response, "status_code", None) in (401, 403):
                raise StorageNotConfigured(HUB_ACCESS_ERROR) from error
            raise StorageError("Could not list corpus archives") from error

    def info(self, _user_id: str, filename: str) -> StoredCorpus:
        repo_id, repo_type, _token = self._config()
        name = archive_name(filename)
        try:
            if repo_type == "bucket":
                entries = list(self.api.get_bucket_paths_info(repo_id, paths=[name]))
            else:
                entries = list(self.api.get_paths_info(repo_id, paths=[name], repo_type=repo_type))
        except (RepositoryNotFoundError, BucketNotFoundError, EntryNotFoundError) as error:
            if getattr(error.response, "status_code", None) in (401, 403):
                raise StorageNotConfigured(HUB_ACCESS_ERROR) from error
            raise StoredCorpusNotFound(f"No corpus named {name!r}") from error
        except HfHubHTTPError as error:
            if getattr(error.response, "status_code", None) in (401, 403):
                raise StorageNotConfigured(HUB_ACCESS_ERROR) from error
            raise StorageError("Could not inspect corpus archive") from error
        if not entries:
            raise StoredCorpusNotFound(f"No corpus named {name!r}")
        entry = entries[0]
        return StoredCorpus(
            filename=entry.path,
            size_bytes=getattr(entry, "size", None),
            repo_id=repo_id,
            url=self._url(repo_id, repo_type, entry.path),
        )

    def open_download(self, _user_id: str, filename: str) -> tuple[httpx.Response, str]:
        repo_id, repo_type, token = self._config()
        name = archive_name(filename)
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        try:
            response = self.client.send(
                self.client.build_request(
                    "GET", self._url(repo_id, repo_type, name), headers=headers
                ),
                stream=True,
                follow_redirects=True,
            )
        except httpx.RequestError as error:
            raise StorageError("Hub storage request failed") from error
        if response.status_code == 404:
            response.close()
            raise StoredCorpusNotFound(f"No corpus named {name!r}")
        if response.status_code in (401, 403):
            response.close()
            raise StorageNotConfigured(HUB_ACCESS_ERROR)
        if response.status_code != 200:
            response.close()
            raise StorageError("Could not download corpus archive")
        return response, name


class CorpusStorage:
    """Select the same storage backend that the legacy deployment uses."""

    def __init__(
        self,
        hub: HubCorpusStorage | None = None,
        supabase: SupabaseCorpusStorage | None = None,
    ) -> None:
        self.hub = hub or HubCorpusStorage()
        self.supabase = supabase or SupabaseCorpusStorage()

    def _backend(self) -> HubCorpusStorage | SupabaseCorpusStorage:
        backend = os.getenv("STORAGE_BACKEND", "huggingface")
        if backend == "huggingface":
            return self.hub
        if backend == "supabase":
            return self.supabase
        raise StorageNotConfigured("STORAGE_BACKEND must be huggingface or supabase")

    def list(self, user_id: str) -> list[StoredCorpus]:
        return self._backend().list(user_id)

    def info(self, user_id: str, filename: str) -> StoredCorpus:
        return self._backend().info(user_id, filename)

    def open_download(self, user_id: str, filename: str) -> tuple[httpx.Response, str]:
        return self._backend().open_download(user_id, filename)
