"""Unwired session lease for a bounded, owner-scoped archive download.

The connection factory must return a fresh trusted PostgreSQL connection. The
callback must perform only a bounded, owner-scoped read with digest verification;
this helper does not verify JWT signatures or select a storage backend.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any
from uuid import UUID

from fastapi import HTTPException

from corpora_api.storage_detail import MAX_STORED_ARCHIVE_BYTES


class SessionBoundRead:
    """Keep the session row locked until the bounded object read finishes."""

    def __init__(self, connect: Callable[[], Any]) -> None:
        self.connect = connect

    def read(self, verified_claims: Mapping[str, Any], download: Callable[[str], bytes]) -> bytes:
        try:
            owner = UUID(verified_claims["sub"])
            session = UUID(verified_claims["session_id"])
            expiry = verified_claims["exp"]
            if type(expiry) is not int or not 0 < expiry < 2**63:
                raise ValueError("invalid expiration")
        except (KeyError, TypeError, ValueError, AttributeError) as error:
            raise HTTPException(401, "Verified session is required") from error
        try:
            with self.connect() as db:
                with db.transaction():
                    params = (str(owner), str(session), expiry)
                    db.execute("SELECT public.corpora_stored_lock_session(%s,%s,%s)", params)
                    data = download(str(owner))
                    if not isinstance(data, bytes):
                        raise HTTPException(503, "Archive reader returned invalid data")
                    if len(data) > MAX_STORED_ARCHIVE_BYTES:
                        raise HTTPException(413, "Stored archive exceeds size limit")
                    db.execute("SELECT public.corpora_stored_lock_session(%s,%s,%s)", params)
            return data
        except HTTPException:
            raise
        except Exception as error:
            primary = getattr(getattr(error, "diag", None), "message_primary", None)
            if getattr(error, "sqlstate", None) == "P0001" and primary == "AUTH_SESSION_INVALID":
                raise HTTPException(401, "Verified session is unavailable") from error
            raise HTTPException(503, "Session-bound archive read is unavailable") from error
