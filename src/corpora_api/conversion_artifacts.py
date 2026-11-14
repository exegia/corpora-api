"""Resolve completed legacy conversion artifacts from the private Supabase store."""

from __future__ import annotations

import json
import os
import re
import uuid
from typing import Any
from urllib.parse import quote

import httpx

from corpora_api.validation import MAX_VALIDATION_ARCHIVE_BYTES

_ARCHIVE_SUFFIXES = (".corpus", ".cusx")
_SLUG_PART = re.compile(r"[^a-z0-9]+")


class ConversionArtifactError(Exception):
    """The shared legacy conversion job or artifact store failed."""


class ConversionArtifactNotFound(ConversionArtifactError):
    """No conversion artifact belongs to this user under this job ID."""


class ConversionArtifactNotReady(ConversionArtifactError):
    """The conversion has not completed successfully."""


class ConversionArtifactInvalid(ConversionArtifactError):
    """The conversion did not produce a validated corpus archive."""


class ConversionArtifactTooLarge(ConversionArtifactError):
    """The stored archive exceeds the bounded validation limit."""


def _safe_uuid(value: str, *, hidden: bool = False) -> str:
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError) as error:
        if hidden:
            raise ConversionArtifactNotFound("Conversion job not found") from error
        raise ConversionArtifactError("Verified user identity is not a UUID") from error


def _decode_validation(value: Any) -> dict[str, Any] | None:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return None
    return value if isinstance(value, dict) else None


def _result_filename(row: dict[str, Any], job_id: str, suffix: str) -> str:
    title = str(row.get("display_name") or row.get("name") or "")
    stem = _SLUG_PART.sub("-", title.strip().lower()).strip("-")
    if not stem:
        stem = _SLUG_PART.sub("-", job_id.lower()).strip("-") or job_id
    return f"{stem[:240].rstrip('-') or job_id}{suffix}"


class ConversionArtifactStore:
    """Read only succeeded, owner-matched `/convert` artifacts from Supabase."""

    def __init__(self, client: httpx.Client | None = None) -> None:
        self.client = client or httpx.Client(timeout=30)

    @staticmethod
    def _config() -> tuple[str, str, str]:
        project_ref = os.getenv("PROJECT_REF")
        url = os.getenv("SUPABASE_URL") or (
            f"https://{project_ref}.supabase.co" if project_ref else ""
        )
        key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
        bucket = os.getenv("SUPABASE_JOBS_BUCKET") or os.getenv("SUPABASE_STORAGE_BUCKET", "")
        if not (url and key and bucket):
            raise ConversionArtifactError(
                "Conversion artifact handoff requires Supabase URL, service key, and job bucket"
            )
        return url.rstrip("/"), key, bucket

    @staticmethod
    def _headers(key: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {key}", "apikey": key}

    @staticmethod
    def _object_url(url: str, bucket: str, result_key: str) -> str:
        parts = [bucket, *result_key.split("/")]
        return f"{url}/storage/v1/object/" + "/".join(quote(part, safe="") for part in parts)

    def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        try:
            return self.client.request(method, url, **kwargs)
        except httpx.RequestError as error:
            raise ConversionArtifactError("Conversion artifact store request failed") from error

    def _download(self, url: str, key: str) -> bytes:
        chunks: list[bytes] = []
        size = 0
        try:
            with self.client.stream("GET", url, headers=self._headers(key)) as response:
                if response.status_code in (400, 404):
                    raise ConversionArtifactNotFound("Conversion artifact not found")
                if response.status_code != 200:
                    raise ConversionArtifactError(
                        f"Conversion artifact store returned {response.status_code}"
                    )
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > MAX_VALIDATION_ARCHIVE_BYTES:
                        raise ConversionArtifactTooLarge(
                            f"Conversion archive exceeds {MAX_VALIDATION_ARCHIVE_BYTES} bytes"
                        )
                    chunks.append(chunk)
        except httpx.RequestError as error:
            raise ConversionArtifactError("Conversion artifact download failed") from error
        return b"".join(chunks)

    def fetch(self, owner_id: str, job_id: str, filename: str | None = None) -> tuple[str, bytes]:
        """Fetch one validated archive without accepting a client filesystem path."""
        url, key, bucket = self._config()
        owner = _safe_uuid(owner_id)
        job = _safe_uuid(job_id, hidden=True)
        response = self._request(
            "GET",
            f"{url}/rest/v1/conversion_jobs",
            params={
                "id": f"eq.{job}",
                "owner": f"eq.{owner}",
                "select": "id,owner,status,result_key,name,display_name,output_format,validation",
                "limit": "1",
            },
            headers=self._headers(key),
        )
        if response.status_code != 200:
            raise ConversionArtifactError(f"Conversion job store returned {response.status_code}")
        try:
            rows = response.json()
        except ValueError as error:
            raise ConversionArtifactError("Conversion job store returned invalid JSON") from error
        if not isinstance(rows, list):
            raise ConversionArtifactError("Conversion job store returned invalid rows")
        if not rows:
            raise ConversionArtifactNotFound("Conversion job not found")
        row = rows[0]
        if not isinstance(row, dict) or row.get("id") != job or row.get("owner") != owner:
            raise ConversionArtifactError("Conversion job store returned a mismatched row")
        if row.get("status") != "succeeded":
            raise ConversionArtifactNotReady("Conversion job is not complete")
        validation = _decode_validation(row.get("validation"))
        if validation is None or validation.get("valid") is not True:
            raise ConversionArtifactInvalid("Conversion job has no passing archive validation")

        result_key = row.get("result_key")
        if not isinstance(result_key, str):
            raise ConversionArtifactInvalid("Conversion job has no archive result")
        suffix = next(
            (
                suffix
                for suffix in _ARCHIVE_SUFFIXES
                if result_key == f"conversion-jobs/{job}{suffix}"
            ),
            None,
        )
        if suffix is None:
            raise ConversionArtifactInvalid("Conversion job result is not a publishable archive")

        result_filename = filename or _result_filename(row, job, suffix)
        if (
            not result_filename
            or len(result_filename.encode("utf-8")) > 255
            or result_filename in {".", ".."}
            or "/" in result_filename
            or "\\" in result_filename
            or any(ord(char) < 32 for char in result_filename)
            or not result_filename.endswith(suffix)
        ):
            raise ConversionArtifactInvalid("Provide a plain .corpus or .cusx filename")

        archive = self._download(self._object_url(url, bucket, result_key), key)
        return result_filename, archive
