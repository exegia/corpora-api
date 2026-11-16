"""Owner-scoped durable ingestion jobs in the legacy Supabase job store."""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
from corpora_schema_types import validate_graph

from corpora_api.ingest import MAX_GRAPH_BYTES, _slug


class IngestStoreError(Exception):
    """The configured shared job or artifact store is unavailable."""


class IngestNotFound(Exception):
    """No job belongs to the authenticated user under this ID."""


class IngestNotReady(Exception):
    """A visible job has no completed graph yet."""


class IngestStoreCorrupt(Exception):
    """The stored artifact no longer satisfies the shared graph contract."""


class IngestStore:
    """Use the existing `conversion_jobs` table and private result bucket."""

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
            raise IngestStoreError(
                "Ingestion store requires SUPABASE_URL (or PROJECT_REF), "
                "SUPABASE_SERVICE_ROLE_KEY, and SUPABASE_JOBS_BUCKET "
                "(or SUPABASE_STORAGE_BUCKET)"
            )
        return url.rstrip("/"), key, bucket

    @staticmethod
    def _id(value: str) -> str:
        try:
            return str(uuid.UUID(value))
        except ValueError as error:
            raise IngestNotFound("Ingestion job not found") from error

    @staticmethod
    def _owner(value: str) -> str:
        try:
            return str(uuid.UUID(value))
        except ValueError as error:
            raise IngestStoreError("Verified user identity is not a UUID") from error

    @staticmethod
    def _headers(key: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {key}", "apikey": key}

    def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        try:
            return self.client.request(method, url, **kwargs)
        except httpx.RequestError as error:
            raise IngestStoreError("Ingestion store request failed") from error

    def recover(self, owner: str, job_id: str, filename: str) -> bool:
        """Finish a reserved job from its existing artifact without rerunning a worker."""
        url, key, bucket = self._config()
        owner = self._owner(owner)
        job_id = self._id(job_id)
        result_key = f"conversion-jobs/{job_id}.graph.json"
        response = self._request(
            "GET", f"{url}/storage/v1/object/{bucket}/{result_key}", headers=self._headers(key)
        )
        if response.status_code == 404:
            return False
        if response.status_code != 200 or len(response.content) > MAX_GRAPH_BYTES:
            raise IngestStoreError("Could not confirm ingestion artifact")
        try:
            graph = json.loads(response.content)
            validate_graph(graph)
            title = graph["work"]["title"]
            if not isinstance(title, str):
                raise ValueError("Invalid work title")
        except (ValueError, KeyError, TypeError) as error:
            raise IngestStoreError("Stored ingestion artifact failed validation") from error
        self.save(owner, job_id, filename, title, response.content, retry=True)
        return True

    def save(
        self,
        owner: str,
        job_id: str,
        filename: str,
        title: str,
        graph: bytes,
        *,
        retry: bool = False,
    ) -> None:
        """Publish result bytes first, then its completed metadata row."""
        url, key, bucket = self._config()
        owner = self._owner(owner)
        job_id = self._id(job_id)
        if len(graph) > MAX_GRAPH_BYTES:
            raise IngestStoreError("Generated graph exceeds the hosted response limit")
        result_key = f"conversion-jobs/{job_id}.graph.json"
        object_url = f"{url}/storage/v1/object/{bucket}/{result_key}"
        try:
            response = self._request(
                "POST",
                object_url,
                headers={**self._headers(key), "Content-Type": "application/json"},
                content=graph,
            )
            if response.status_code not in (200, 201):
                raise IngestStoreError(f"Ingestion artifact store returned {response.status_code}")
        except IngestStoreError:
            if not retry:
                raise
            # A different attempt for the same reserved request may have put
            # the immutable object first, or this upload response may be lost.
            existing = self._request("GET", object_url, headers=self._headers(key))
            if existing.status_code != 200 or len(existing.content) > MAX_GRAPH_BYTES:
                raise IngestStoreError("Could not confirm ingestion artifact") from None
            try:
                saved_graph = json.loads(existing.content)
                validate_graph(saved_graph)
                saved_title = saved_graph["work"]["title"]
                if not isinstance(saved_title, str):
                    raise ValueError("Invalid work title")
                title = saved_title
            except (ValueError, KeyError, TypeError) as error:
                raise IngestStoreError("Stored ingestion artifact failed validation") from error

        now = time.time()
        row = {
            "id": job_id,
            "source_format": Path(filename).suffix.lower().lstrip("."),
            "output_format": "corpus",
            "name": filename,
            "status": "succeeded",
            "created_at": now,
            "started_at": now,
            "finished_at": now,
            "result_key": result_key,
            "error": None,
            "logs": ["Ingestion complete."],
            "owner": owner,
            "display_name": title,
            "validation": None,
        }
        try:
            response = self._request(
                "POST",
                f"{url}/rest/v1/conversion_jobs",
                headers={**self._headers(key), "Prefer": "return=minimal"},
                json=row,
            )
            if response.status_code in (200, 201, 204):
                return
            error = IngestStoreError(f"Ingestion job store returned {response.status_code}")
        except IngestStoreError as request_error:
            error = request_error

        # The row may have committed even when the gateway failed. A successful
        # owner-scoped read can confirm this exact job; a missing or failed read
        # cannot prove the insert will not commit later, so retain the object.
        try:
            saved = self.get(owner, job_id)
        except (IngestNotFound, IngestStoreError):
            raise error from None
        fields = (
            "id",
            "owner",
            "source_format",
            "output_format",
            "name",
            "display_name",
            "status",
            "result_key",
            "error",
        )
        if all(field in saved and saved[field] == row[field] for field in fields):
            return
        raise error

    def get(self, owner: str, job_id: str) -> dict[str, Any]:
        """Query by both verified owner and UUID; foreign jobs appear absent."""
        url, key, _bucket = self._config()
        owner = self._owner(owner)
        job_id = self._id(job_id)
        response = self._request(
            "GET",
            f"{url}/rest/v1/conversion_jobs",
            params={"id": f"eq.{job_id}", "owner": f"eq.{owner}", "select": "*", "limit": "1"},
            headers=self._headers(key),
        )
        if response.status_code != 200:
            raise IngestStoreError(f"Ingestion job store returned {response.status_code}")
        try:
            rows = response.json()
        except ValueError as error:
            raise IngestStoreError("Ingestion job store returned invalid JSON") from error
        if not isinstance(rows, list):
            raise IngestStoreError("Ingestion job store returned invalid rows")
        if not rows:
            raise IngestNotFound("Ingestion job not found")
        row = rows[0]
        if not isinstance(row, dict) or row.get("owner") != owner or row.get("id") != job_id:
            raise IngestStoreError("Ingestion job store returned a mismatched row")
        if row.get("result_key") != f"conversion-jobs/{job_id}.graph.json":
            raise IngestNotFound("Ingestion job not found")
        return row

    def status(self, owner: str, job_id: str) -> dict[str, Any]:
        row = self.get(owner, job_id)
        logs = row.get("logs") or []
        if isinstance(logs, dict):
            logs = logs.get("logs") or []
        if not isinstance(logs, list):
            raise IngestStoreError("Ingestion job store returned invalid logs")
        ready = row.get("status") == "succeeded" and bool(row.get("result_key"))
        return {
            "id": row["id"],
            "source_format": row["source_format"],
            "output_format": row.get("output_format") or "corpus",
            "name": row["name"],
            "display_name": row.get("display_name"),
            "status": row["status"],
            "created_at": row["created_at"],
            "started_at": row.get("started_at"),
            "finished_at": row.get("finished_at"),
            "error": row.get("error"),
            "logs": logs,
            "last_log": logs[-1] if logs else None,
            "events": [],
            "steps": [],
            "validation": row.get("validation"),
            "result_filename": f"{_slug(row.get('display_name') or row['name'])}.graph.json",
            "download_ready": ready,
        }

    def download(self, owner: str, job_id: str) -> tuple[bytes, str]:
        row = self.get(owner, job_id)
        job_id = self._id(job_id)
        if row.get("status") != "succeeded":
            raise IngestNotReady(f"Job is {row.get('status')}, not ready")
        if row.get("result_key") != f"conversion-jobs/{job_id}.graph.json":
            raise IngestStoreError("Ingestion job has an invalid result key")
        url, key, bucket = self._config()
        response = self._request(
            "GET",
            f"{url}/storage/v1/object/{bucket}/{row['result_key']}",
            headers=self._headers(key),
        )
        if response.status_code != 200:
            raise IngestStoreError(f"Ingestion artifact store returned {response.status_code}")
        if len(response.content) > MAX_GRAPH_BYTES:
            raise IngestStoreError("Stored graph exceeds the hosted response limit")
        try:
            validate_graph(json.loads(response.content))
        except ValueError as error:
            raise IngestStoreCorrupt("Stored graph failed validation") from error
        filename = f"{_slug(row.get('display_name') or row['name'])}.graph.json"
        return response.content, filename
