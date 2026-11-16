"""Session-locked draft reads with a time-bounded worker."""

from __future__ import annotations

import base64
import json
import os
from collections.abc import Mapping
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

import psycopg
from fastapi import HTTPException
from psycopg.types.json import Jsonb

from corpora_api.conversion_artifacts import ConversionArtifactStore
from corpora_api.drafts import MAX_DRAFT_VIEW_RESPONSE_BYTES, DraftStore
from corpora_api.session_connection import SessionDatabase
from corpora_api.session_read import SessionBoundRead
from corpora_api.timed_draft_read import MAX_PREPARE_SECONDS, read_draft_timed
from corpora_api.validation import ValidationBridge


def configured_draft_lease() -> SessionBoundRead:
    """Fail closed unless the separately provisioned database login is configured."""
    try:
        database = SessionDatabase(
            host=os.environ["CORPORA_SESSION_DB_HOST"],
            hostaddr=os.environ["CORPORA_SESSION_DB_ADDRESS"],
            port=int(os.environ["CORPORA_SESSION_DB_PORT"]),
            database=os.environ["CORPORA_SESSION_DB_NAME"],
            user=os.environ["CORPORA_SESSION_DB_USER"],
            password=os.environ["CORPORA_SESSION_DB_PASSWORD"],
        )
    except (KeyError, ValueError) as error:
        raise HTTPException(503, "Session verification is unavailable") from error
    return SessionBoundRead(database.connect, assume_service_role=True)


def read_session_draft(
    lease: SessionBoundRead,
    store: DraftStore,
    claims: Mapping[str, Any],
    draft_id: str | None,
    view: str,
    **params: Any,
) -> dict[str, Any]:
    """Release the result only after the session is checked a second time."""

    def read(owner: str) -> bytes:
        result = read_draft_timed(store, owner, draft_id, view, params)
        return json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    payload = lease.read(claims, read, max_bytes=MAX_DRAFT_VIEW_RESPONSE_BYTES)
    return json.loads(payload)


def read_session_draft_archive(
    lease: SessionBoundRead, store: DraftStore, claims: Mapping[str, Any], draft_id: str
) -> tuple[dict[str, Any], bytes]:
    """Hold the session through the verified archive read, returning only after its final check."""
    heads: list[dict[str, Any]] = []

    def download(owner: str) -> bytes:
        result = read_draft_timed(store, owner, draft_id, "archive", {})
        heads.append(result["draft"])
        return base64.b64decode(result["archive"], validate=True)

    data = lease.read(claims, download)
    return heads[0], data


def create_session_draft(
    lease: SessionBoundRead,
    store: DraftStore,
    claims: Mapping[str, Any],
    job_id: str,
    jobs: ConversionArtifactStore,
    validator: ValidationBridge,
) -> dict[str, Any]:
    """Stage privately, then publish the registry entry in the locked session transaction."""
    lease.read(claims, lambda _owner: b"")
    try:
        owner, job_id = str(UUID(claims["sub"])), str(UUID(job_id))
    except (KeyError, ValueError, TypeError, AttributeError) as error:
        raise HTTPException(404, "Conversion job not found") from error
    job_url, job_key, bucket = jobs._config()
    staged = read_draft_timed(
        store,
        owner,
        job_id,
        "prepare",
        {
            "job_url": job_url,
            "job_key": job_key,
            "bucket": bucket,
            "validator": validator.executable,
        },
        timeout=MAX_PREPARE_SECONDS,
    )
    document_id = str(uuid5(NAMESPACE_URL, json.dumps(["ai-draft", owner, job_id])))

    def publish(principal: str, db: Any) -> bytes:
        if principal != owner:
            raise HTTPException(401, "Verified session is unavailable")
        if "existing" in staged:
            info = staged["existing"]
            if info.get("id") != document_id or info.get("corpus") != "draft:" + document_id:
                raise HTTPException(503, "Working draft preparation returned invalid data")
        else:
            payload = staged["provision"]
            fields = {
                "document_id",
                "corpus",
                "bucket",
                "object_key",
                "revision",
                "digest",
                "version",
                "job_id",
                "name",
            }
            if set(payload) != fields or payload.get("job_id") != job_id:
                raise HTTPException(503, "Working draft preparation returned invalid data")
            try:
                head = store._head(
                    {key: value for key, value in payload.items() if key not in {"job_id", "name"}}
                    | {"owner": owner},
                    owner,
                    document_id,
                )
                if (
                    head.revision != uuid5(UUID(document_id), head.digest)
                    or not isinstance(payload["name"], str)
                    or len(payload["name"]) > 512
                ):
                    raise ValueError("Invalid staged identity")
            except (ValueError, TypeError) as error:
                raise HTTPException(
                    503, "Working draft preparation returned invalid data"
                ) from error
            try:
                row = db.execute(
                    "SELECT public.corpora_ai_storage('provision',%s,%s)",
                    (owner, Jsonb(payload)),
                ).fetchone()
            except psycopg.Error as error:
                message = getattr(error.diag, "message_primary", None)
                status = {
                    "AI_NOT_FOUND": 404,
                    "AI_LOCKED": 423,
                    "AI_CONFLICT": 409,
                    "AI_INVALID": 422,
                }.get(message, 503)
                raise HTTPException(status, "Working draft is unavailable") from error
            info = store._info(store._head(row[0] if row else None, owner, document_id))
        return json.dumps(info, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    data = lease.read_with_connection(claims, publish, max_bytes=MAX_DRAFT_VIEW_RESPONSE_BYTES)
    return json.loads(data)
