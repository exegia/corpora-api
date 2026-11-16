"""Owner-scoped AI conversation operations and idempotent aggregate updates."""

from __future__ import annotations

import base64
import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from fastapi import HTTPException
from fastapi.responses import JSONResponse

from corpora_api.ai_validation import owned_archive, validate_scope
from corpora_api.models import (
    AIMessageList,
    AINodeScope,
    AISuggestionList,
    AIThread,
    AIThreadList,
    AIThreadMessage,
    AIThreadSection,
)
from corpora_api.thread_store import ThreadRecord, ThreadState, ThreadStore


class ScopeConflict(Exception):
    def __init__(self, response: JSONResponse) -> None:
        self.response = response


def _id(value: str) -> str:
    try:
        return str(UUID(value))
    except (ValueError, TypeError, AttributeError) as error:
        raise HTTPException(404, "Conversation record not found") from error


def _operation_id(namespace: str, key: str | None) -> str:
    if key is None:
        return str(uuid4())
    try:
        normalized = str(UUID(key))
    except (ValueError, TypeError, AttributeError) as error:
        raise HTTPException(422, "Idempotency-Key must be a UUID") from error
    return str(uuid5(NAMESPACE_URL, namespace + ":" + normalized))


class ThreadService:
    def __init__(self, store: ThreadStore, storage: Any, jobs: Any, drafts: Any) -> None:
        self.store, self.storage, self.jobs, self.drafts = store, storage, jobs, drafts

    def _access(self, owner: str, corpus: str) -> None:
        owned_archive(self.storage, self.jobs, self.drafts, owner, corpus)

    def _validate(self, owner: str, scope: AINodeScope) -> None:
        result = validate_scope(self.storage, self.jobs, owner, scope, self.drafts)
        if isinstance(result, JSONResponse):
            raise ScopeConflict(result)

    def _record(self, owner: str, thread_id: str) -> ThreadRecord:
        record = self.store.get(owner, _id(thread_id))
        if record is None:
            raise HTTPException(404, "Thread not found")
        return record

    @staticmethod
    def _bounded(state: ThreadState) -> None:
        if (
            len(state.thread.sections) > 256
            or len(state.messages) > 2048
            or len(state.suggestions) > 512
            or len(state.model_dump_json().encode("utf-8")) > 2_000_000
        ):
            raise HTTPException(413, "Thread capacity reached; start a new thread")

    def _update(
        self, owner: str, thread_id: str, change: Callable[[ThreadState], None]
    ) -> ThreadState:
        for _ in range(5):
            record = self._record(owner, thread_id)
            self._access(owner, record.corpus)
            before = record.data.model_dump_json()
            change(record.data)
            self._bounded(record.data)
            if before == record.data.model_dump_json() or self.store.replace(record):
                return record.data
        raise HTTPException(409, "Thread changed concurrently; retry with the same Idempotency-Key")

    def create(self, owner: str, scope: AINodeScope, key: str | None) -> AIThread:
        thread_id = _operation_id("thread:" + owner, key)
        existing = self.store.get(owner, thread_id)
        if existing:
            self._access(owner, existing.corpus)
            if existing.data.thread.pinned_scope != scope:
                raise HTTPException(409, "Idempotency-Key was already used with another scope")
            return existing.data.thread
        self._validate(owner, scope)
        now = datetime.now(UTC)
        thread = AIThread(
            id=thread_id,
            corpus=scope.corpus,
            pinned_scope=scope,
            sections=[
                AIThreadSection(id=str(uuid5(UUID(thread_id), "root")), scope=scope, created_at=now)
            ],
            created_at=now,
        )
        record = ThreadRecord(
            id=thread_id,
            owner=owner,
            corpus=scope.corpus,
            created_at=now.isoformat(),
            data=ThreadState(thread=thread),
        )
        self._bounded(record.data)
        if self.store.create(record):
            return thread
        existing = self._record(owner, thread_id)
        self._access(owner, existing.corpus)
        if existing.data.thread.pinned_scope != scope:
            raise HTTPException(409, "Idempotency-Key was already used with another scope")
        return existing.data.thread

    def get(self, owner: str, thread_id: str) -> AIThread:
        record = self._record(owner, thread_id)
        self._access(owner, record.corpus)
        return record.data.thread

    def list(self, owner: str, corpus: str, limit: int, cursor: str | None) -> AIThreadList:
        self._access(owner, corpus)
        before = None
        if cursor:
            try:
                timestamp, thread_id = json.loads(base64.urlsafe_b64decode(cursor.encode("ascii")))
                parsed = datetime.fromisoformat(timestamp)
                if parsed.tzinfo is None:
                    raise ValueError("Timezone required")
                before = (parsed.astimezone(UTC).isoformat(), str(UUID(thread_id)))
            except (ValueError, TypeError, UnicodeError) as error:
                raise HTTPException(422, "Invalid thread cursor") from error
        rows = self.store.list(owner, corpus, limit + 1, before)
        page = rows[:limit]
        next_cursor = None
        if len(rows) > limit:
            last = page[-1]
            next_cursor = base64.urlsafe_b64encode(
                json.dumps([last.created_at, last.id]).encode()
            ).decode()
        return AIThreadList(threads=[row.data.thread for row in page], next_cursor=next_cursor)

    def fork(self, owner: str, thread_id: str, scope: AINodeScope, key: str | None) -> AIThread:
        section_id = _operation_id("section:" + _id(thread_id), key)

        def change(state: ThreadState) -> None:
            if scope.corpus != state.thread.corpus:
                raise HTTPException(422, "A section must stay in its thread's corpus")
            existing = next((s for s in state.thread.sections if s.id == section_id), None)
            if existing:
                if existing.scope != scope:
                    raise HTTPException(409, "Idempotency-Key was already used with another scope")
                return
            self._validate(owner, scope)
            state.thread.sections.append(
                AIThreadSection(id=section_id, scope=scope, created_at=datetime.now(UTC))
            )

        return self._update(owner, thread_id, change).thread

    def append(
        self, owner: str, thread_id: str, section_id: str, content: str, key: str | None
    ) -> AIThreadMessage:
        message = AIThreadMessage(
            id=_operation_id("message:" + _id(thread_id), key),
            section_id=_id(section_id),
            role="user",
            content=content,
            created_at=datetime.now(UTC),
        )

        def change(state: ThreadState) -> None:
            if not any(s.id == message.section_id for s in state.thread.sections):
                raise HTTPException(404, "Thread section not found")
            existing = next((m for m in state.messages if m.id == message.id), None)
            if existing:
                if (existing.content, existing.section_id, existing.role) != (
                    message.content,
                    message.section_id,
                    message.role,
                ):
                    raise HTTPException(
                        409, "Idempotency-Key was already used with another message"
                    )
                return
            state.messages.append(message)

        state = self._update(owner, thread_id, change)
        return next(m for m in state.messages if m.id == message.id)

    def messages(self, owner: str, thread_id: str, limit: int, offset: int) -> AIMessageList:
        record = self._record(owner, thread_id)
        self._access(owner, record.corpus)
        end = offset + limit
        return AIMessageList(
            messages=record.data.messages[offset:end],
            next_offset=end if end < len(record.data.messages) else None,
        )

    def suggestions(self, owner: str, thread_id: str, limit: int, offset: int) -> AISuggestionList:
        record = self._record(owner, thread_id)
        self._access(owner, record.corpus)
        end = offset + limit
        return AISuggestionList(
            suggestions=record.data.suggestions[offset:end],
            next_offset=end if end < len(record.data.suggestions) else None,
        )
