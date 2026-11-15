"""Unwired orchestration for owner-resolved archive edits and restores."""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID, uuid4

from fastapi import HTTPException

from corpora_api.stored_revisions import Revision, Snapshot, StoredRevisions
from corpora_api.version_process import VersionProcessError, run_version


class StoredVersions:
    """Use a verified head and publish each transformed snapshot by CAS.

    The caller must authenticate and authorize the owner, and coordinate all
    existing storage readers, imports and deletes before wiring these methods.
    """

    def __init__(self, revisions: StoredRevisions) -> None:
        self.revisions = revisions

    def _head(self, owner: str, filename: str, expected: str) -> Snapshot:
        try:
            expected_id = UUID(expected)
        except (ValueError, TypeError, AttributeError) as error:
            raise HTTPException(422, "Invalid expected revision") from error
        head = self.revisions.get(owner, filename)
        if head.fact.revision != expected_id:
            raise HTTPException(409, "Stored revision is stale")
        return head

    def _publish(self, head: Snapshot, revision: UUID, prepared: dict[str, Any]) -> dict[str, Any]:
        try:
            fact = Revision(
                owner=head.fact.owner,
                filename=head.fact.filename,
                revision=revision,
                digest=prepared["digest"],
                version=prepared["version"],
            )
            data = prepared["archive"]
            if not isinstance(data, bytes) or not isinstance(prepared["result"], dict):
                raise ValueError("invalid prepared archive")
        except (ValueError, KeyError, TypeError) as error:
            raise HTTPException(503, "Version worker returned invalid data") from error
        self.revisions.publish(fact, data, str(head.fact.revision))
        return {"revision": str(revision), "version": fact.version, "result": prepared["result"]}

    @staticmethod
    def _prepare(
        current: bytes, selected: bytes | None, worker_operation: str, **args: Any
    ) -> dict[str, Any]:
        try:
            return run_version(current, selected, worker_operation, **args)
        except VersionProcessError as error:
            status = {"invalid": 422, "too_large": 413}.get(error.kind, 503)
            raise HTTPException(status, "Version transformation failed") from error

    def edit(
        self,
        owner: str,
        filename: str,
        expected: str,
        operation: Literal["manifest", "node"],
        updates: dict[str, Any],
        node: int | None = None,
    ) -> dict[str, Any]:
        head = self._head(owner, filename, expected)
        revision = uuid4()
        prepared = self._prepare(
            head.data,
            None,
            "edit",
            name=filename,
            expected_digest=head.fact.digest,
            expected_version=head.fact.version,
            owner=str(head.fact.owner),
            current_revision=str(head.fact.revision),
            new_revision=str(revision),
            operation=operation,
            updates=updates,
            node=node,
        )
        return self._publish(head, revision, prepared)

    def restore(self, owner: str, filename: str, expected: str, selected: str) -> dict[str, Any]:
        head = self._head(owner, filename, expected)
        source = self.revisions.get(owner, filename, selected)
        revision = uuid4()
        prepared = self._prepare(
            head.data,
            source.data,
            "restore",
            name=filename,
            expected_digest=head.fact.digest,
            selected_digest=source.fact.digest,
            expected_version=head.fact.version,
            selected_version=source.fact.version,
            owner=str(head.fact.owner),
            current_revision=str(head.fact.revision),
            selected_revision=str(source.fact.revision),
            new_revision=str(revision),
        )
        return self._publish(head, revision, prepared)
