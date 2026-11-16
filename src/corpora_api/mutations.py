"""Owner-scoped apply and undo through the durable draft transaction."""

from __future__ import annotations

import os

from corpora_api.drafts import DraftStore
from corpora_api.models import AIApplyResponse, AIChatSuggestion, AIUndoResponse
from corpora_api.mutation_drafts import ArchiveMutationDrafts
from corpora_api.mutation_journal import CurationError, MutationEngine, Record
from corpora_api.mutation_storage import MutationStorage, SupabaseMutationJournal, _uuid
from corpora_api.thread_store import ThreadStore


class MutationService:
    def __init__(self, drafts: DraftStore, threads: ThreadStore) -> None:
        self.storage = MutationStorage(drafts)
        self.threads = threads
        self.journal = SupabaseMutationJournal(self.storage)
        self.engine = MutationEngine(self.journal, ArchiveMutationDrafts(self.storage))

    def _enabled(self, owner: str) -> None:
        if os.getenv("CORPORA_AI_MUTATIONS_ENABLED", "false").lower() != "true":
            raise CurationError(501, "Hosted AI mutations are not enabled")
        capabilities = self.storage.rpc("capabilities", owner, {})
        if not isinstance(capabilities, dict) or capabilities.get("mutation_api") != 1:
            raise CurationError(503, "Mutation database migration is unavailable")

    def apply(self, owner: str, suggestion_id: str, confirmation: str | None) -> AIApplyResponse:
        self._enabled(owner)
        parts = suggestion_id.split(":")
        if len(parts) != 2:
            raise CurationError(404, "Suggestion not found")
        thread_id, local_id = (_uuid(part) for part in parts)
        suggestion_id = thread_id + ":" + local_id
        try:
            thread = self.threads.get(owner, thread_id)
        except Exception as error:
            from fastapi import HTTPException

            if isinstance(error, HTTPException):
                raise CurationError(error.status_code, str(error.detail)) from error
            raise
        if thread is None:
            raise CurationError(404, "Suggestion not found")
        row = next((s for s in thread.data.suggestions if s.suggestion.id == suggestion_id), None)
        if row is None or row.suggestion.scope.corpus != thread.corpus:
            raise CurationError(404, "Suggestion not found")
        self.storage.resolve(owner, thread.corpus)
        suggestion = AIChatSuggestion.model_validate(row.suggestion.model_dump(mode="json"))
        record = self.engine.apply(owner, suggestion, confirmation)
        if record.applied_at is None:
            raise CurationError(503, "Change finalization is unavailable")
        entries = record.intent.entries(record.applied_at)
        return AIApplyResponse(
            suggestion_id=suggestion_id,
            status="applied",
            change=entries[0],
            changes=entries,
            operation_id=record.intent.id,
        )

    def undo(self, owner: str, change_id: str) -> AIUndoResponse:
        self._enabled(owner)
        change_id = _uuid(change_id)
        value = self.storage.rpc("change", owner, {"id": change_id})
        if value is None:
            raise CurationError(404, "Change not found")
        try:
            original = Record.model_validate(value)
        except ValueError as error:
            raise CurationError(503, "Change storage returned invalid data") from error
        if original.intent.owner != _uuid(owner):
            raise CurationError(503, "Change storage returned invalid data")
        self.storage.resolve(owner, original.intent.suggestion.scope.corpus)
        if original.applied_at is None:
            receipt = self.storage.receipt(owner, original.intent.id)
            if receipt is not None:
                if receipt.proof != original.intent.proof:
                    raise CurationError(409, "Publication receipt conflicts with change")
                original = self.journal.finish(owner, original.intent.id, receipt.applied_at)
        if original.applied_at is not None and not any(
            entry.change_id == change_id for entry in original.intent.entries(original.applied_at)
        ):
            raise CurationError(503, "Change storage returned an invalid group")
        record = self.engine.undo(owner, original.intent.id)
        if record.applied_at is None:
            raise CurationError(503, "Change finalization is unavailable")
        entries = record.intent.entries(record.applied_at)
        selected = next((entry for entry in entries if entry.reverts == change_id), None)
        if selected is None:
            raise CurationError(503, "Change storage returned an invalid group")
        return AIUndoResponse(
            reverted_change_id=change_id,
            revert=selected,
            reverts=entries,
            operation_id=record.intent.id,
        )
