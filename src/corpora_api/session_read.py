"""Unwired session lease for a bounded, owner-scoped archive download.

The connection factory must return a fresh trusted PostgreSQL connection. The
callback must perform only a bounded, owner-scoped read with digest verification;
this helper does not verify JWT signatures or select a storage backend.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, Protocol
from uuid import UUID

from fastapi import HTTPException

from corpora_api.storage_detail import MAX_STORED_ARCHIVE_BYTES


class SessionBoundRead:
    """Keep the session row locked until the bounded object read finishes."""

    def __init__(self, connect: Callable[[], Any], *, assume_service_role: bool = False) -> None:
        self.connect = connect
        self.assume_service_role = assume_service_role

    def read(
        self,
        verified_claims: Mapping[str, Any],
        download: Callable[[str], bytes],
        *,
        max_bytes: int = MAX_STORED_ARCHIVE_BYTES,
    ) -> bytes:
        return self.read_with_connection(
            verified_claims, lambda owner, _db: download(owner), max_bytes=max_bytes
        )

    def read_with_connection(
        self,
        verified_claims: Mapping[str, Any],
        download: Callable[[str, Any], bytes],
        *,
        max_bytes: int = MAX_STORED_ARCHIVE_BYTES,
    ) -> bytes:
        if type(max_bytes) is not int or not 0 < max_bytes <= 4_000_000:
            raise ValueError("Invalid session read limit")
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
                    if self.assume_service_role:
                        db.execute("SET LOCAL ROLE service_role")
                    db.execute("SELECT pg_catalog.set_config('statement_timeout','5000',true)")
                    params = (str(owner), str(session), expiry)
                    db.execute("SELECT public.corpora_stored_lock_session(%s,%s,%s)", params)
                    data = download(str(owner), db)
                    if not isinstance(data, bytes):
                        raise HTTPException(503, "Archive reader returned invalid data")
                    if len(data) > max_bytes:
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


class RevisionReader(Protocol):
    """The registry adapter must bind owner, filename, revision and object digest."""

    def get(self, owner: str, filename: str, revision: str | None = None) -> Any: ...


class SessionBoundRevisions:
    """Compose the session lease with the bounded, verified revision reader."""

    def __init__(self, lease: SessionBoundRead, revisions: RevisionReader) -> None:
        self.lease = lease
        self.revisions = revisions

    def get(
        self, verified_claims: Mapping[str, Any], filename: str, revision: str | None = None
    ) -> Any:
        snapshots: list[Any] = []

        def download(owner: str) -> bytes:
            snapshot = self.revisions.get(owner, filename, revision)
            snapshots.append(snapshot)
            return snapshot.data

        # The snapshot is exposed only after the lease's final session check.
        self.lease.read(verified_claims, download)
        return snapshots[0]


class SessionBoundTimedRevisions:
    """Resolve metadata in the locked transaction, then run a timed download."""

    def __init__(self, lease: SessionBoundRead, config: Any, timeout: float = 30.0) -> None:
        self.lease, self.config, self.timeout = lease, config, timeout

    def get(
        self, verified_claims: Mapping[str, Any], filename: str, revision: str | None = None
    ) -> Any:
        from corpora_api.timed_revision_read import TimedRevisionReader

        snapshots: list[Any] = []

        def download(owner: str, db: Any) -> bytes:
            config = self.config

            class TransactionRegistry:
                def _rpc(self, name: str, payload: dict[str, object]) -> object:
                    if name != "corpora_stored_get":
                        raise ValueError("unexpected registry operation")
                    row = db.execute(
                        "SELECT public.corpora_stored_get(%s,%s,%s)",
                        (payload["p_owner"], payload["p_filename"], payload["p_revision"]),
                    ).fetchone()
                    return row[0] if row else None

                def _config(self) -> tuple[str, dict[str, str]]:
                    return config._config()

            snapshot = TimedRevisionReader(TransactionRegistry(), self.timeout).get(
                owner, filename, revision
            )
            snapshots.append(snapshot)
            return snapshot.data

        self.lease.read_with_connection(verified_claims, download)
        return snapshots[0]
