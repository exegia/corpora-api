"""Bridge mutation protocol to isolated archive workers and immutable storage."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime

from pydantic import ValidationError

from corpora_api.corpus_process import (
    MAX_CORPUS_WORKER_RESPONSE_BYTES,
    CorpusProcessError,
    run_corpus,
)
from corpora_api.models import AIApplyResponse, AIChatSuggestion, AIUndoResponse
from corpora_api.mutation_journal import (
    CurationError,
    Evidence,
    Intent,
    Plan,
    Receipt,
    _operation_id,
)
from corpora_api.mutation_storage import MutationStorage


def _require_recoverable_size(owner: str, suggestion: AIChatSuggestion, plan: Plan) -> None:
    # Inspection uses the public-read worker bound. Reject before staging if
    # either snapshot could not subsequently be inspected during recovery.
    for evidence in (plan.before, plan.after):
        response = {"result": {"evidence": evidence.model_dump(mode="json")}}
        size = len(json.dumps(response, ensure_ascii=False).encode("utf-8"))
        if size > MAX_CORPUS_WORKER_RESPONSE_BYTES:
            raise CurationError(413, "Change evidence exceeds the recovery limit")

    reverts = suggestion.id.removeprefix("undo:") if suggestion.id.startswith("undo:") else None
    intent = Intent(
        id=_operation_id(owner, suggestion.id, reverts),
        owner=owner,
        suggestion=suggestion,
        plan=plan,
        created_at=datetime.now(UTC),
        reverts=reverts,
    )
    entries = intent.entries(intent.created_at)
    if reverts:
        selected = max(entries, key=lambda entry: len(entry.model_dump_json().encode("utf-8")))
        response_model = AIUndoResponse(
            reverted_change_id=selected.reverts or "",
            revert=selected,
            reverts=entries,
            operation_id=intent.id,
        )
    else:
        response_model = AIApplyResponse(
            suggestion_id=suggestion.id,
            status="applied",
            change=entries[0],
            changes=entries,
            operation_id=intent.id,
        )
    # Reserve room for response serialization differences and timestamps.
    if len(response_model.model_dump_json().encode("utf-8")) > MAX_CORPUS_WORKER_RESPONSE_BYTES:
        raise CurationError(413, "Change result exceeds the response limit")


class ArchiveMutationDrafts:
    def __init__(self, storage: MutationStorage) -> None:
        self.storage = storage

    def authorize(self, owner: str, corpus: str) -> None:
        if self.storage.resolve(owner, corpus).state != "draft":
            raise CurationError(423, "Corpus is published or locked")

    def confirm(self, owner: str, suggestion: AIChatSuggestion, token: str | None) -> None:
        # A nonempty token does not prove owner, suggestion, scope, or version.
        raise CurationError(428, "Corpus-wide edits require the panel confirmation integration")

    @staticmethod
    def _worker(data: bytes, operation: str, head: object, suggestion: AIChatSuggestion) -> dict:
        try:
            return run_corpus(
                data,
                operation,
                head=head.model_dump(mode="json"),
                suggestion=suggestion.model_dump(mode="json"),
            )
        except CorpusProcessError as error:
            if error.kind == "mutation":
                status = error.attributes.get("status")
                if status in (409, 413, 422, 423, 428):
                    raise CurationError(status, str(error)) from error
            status = {"unavailable": 503, "too_large": 413}.get(error.kind, 422)
            raise CurationError(status, "Corpus archive could not be processed") from error

    def prepare(self, owner: str, suggestion: AIChatSuggestion) -> Plan:
        self.authorize(owner, suggestion.scope.corpus)
        head = self.storage.resolve(owner, suggestion.scope.corpus)
        data = self.storage.download(head)
        result = self._worker(data, "mutation_edit", head, suggestion)
        try:
            plan = Plan.model_validate(result["plan"])
            edited = base64.b64decode(result["data"], validate=True)
        except (ValueError, TypeError, KeyError, ValidationError) as error:
            raise CurationError(503, "Corpus worker returned invalid data") from error
        _require_recoverable_size(owner, suggestion, plan)
        self.storage.stage(head, plan.after.revision, edited, plan.after.digest)
        return plan

    def read(self, intent: Intent) -> Evidence:
        head = self.storage.resolve(intent.owner, intent.suggestion.scope.corpus)
        result = self._worker(
            self.storage.download(head), "mutation_inspect", head, intent.suggestion
        )
        try:
            return Evidence.model_validate(result["evidence"])
        except (KeyError, ValidationError) as error:
            raise CurationError(503, "Corpus worker returned invalid data") from error

    def receipt(self, intent: Intent) -> Receipt | None:
        return self.storage.receipt(intent.owner, intent.id)

    def commit(self, intent: Intent) -> bool:
        self.authorize(intent.owner, intent.suggestion.scope.corpus)
        return self.storage.publish(intent)
