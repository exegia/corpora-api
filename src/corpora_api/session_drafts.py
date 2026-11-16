"""Session-locked draft reads with a time-bounded worker."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from typing import Any

from fastapi import HTTPException

from corpora_api.drafts import MAX_DRAFT_VIEW_RESPONSE_BYTES, DraftStore
from corpora_api.session_connection import SessionDatabase
from corpora_api.session_read import SessionBoundRead
from corpora_api.timed_draft_read import read_draft_timed


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
    draft_id: str,
    view: str,
    **params: Any,
) -> dict[str, Any]:
    """Release the result only after the session is checked a second time."""

    def read(owner: str) -> bytes:
        result = read_draft_timed(store, owner, draft_id, view, params)
        return json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    payload = lease.read(claims, read, max_bytes=MAX_DRAFT_VIEW_RESPONSE_BYTES)
    return json.loads(payload)
