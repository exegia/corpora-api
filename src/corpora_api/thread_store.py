"""Owner-filtered, compare-and-swap storage for bounded AI conversations."""

from __future__ import annotations

import os
from typing import Any

import httpx
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from corpora_api.models import AIThread, AIThreadMessage, AIThreadSuggestion


class ThreadState(BaseModel):
    model_config = ConfigDict(extra="forbid")
    thread: AIThread
    messages: list[AIThreadMessage] = Field(default_factory=list)
    suggestions: list[AIThreadSuggestion] = Field(default_factory=list)


class ThreadRecord(BaseModel):
    id: str
    owner: str
    corpus: str
    created_at: str
    revision: int = 0
    data: ThreadState


class ThreadStore:
    def __init__(self, client: httpx.Client | None = None) -> None:
        self.client = client or httpx.Client(timeout=30)

    def _request(
        self,
        method: str,
        *,
        params: dict[str, str],
        body: dict[str, Any] | None = None,
        create: bool = False,
    ) -> list[ThreadRecord]:
        project = os.getenv("PROJECT_REF")
        url = os.getenv("SUPABASE_URL") or (f"https://{project}.supabase.co" if project else "")
        key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
        if not url or not key:
            raise HTTPException(503, "Conversation storage is unavailable")
        try:
            response = self.client.request(
                method,
                url.rstrip("/") + "/rest/v1/corpus_ai_threads",
                params=params,
                json=body,
                headers={
                    "apikey": key,
                    "Authorization": f"Bearer {key}",
                    "Prefer": "return=representation"
                    + (",resolution=ignore-duplicates" if create else ""),
                },
            )
            response.raise_for_status()
            rows = response.json()
            if not isinstance(rows, list):
                raise ValueError("invalid rows")
            return [ThreadRecord.model_validate(row) for row in rows]
        except (httpx.HTTPError, ValueError, TypeError, ValidationError) as error:
            raise HTTPException(503, "Conversation storage is unavailable") from error

    def get(self, owner: str, thread_id: str) -> ThreadRecord | None:
        rows = self._request(
            "GET", params={"owner": f"eq.{owner}", "id": f"eq.{thread_id}", "limit": "1"}
        )
        if len(rows) > 1:
            raise HTTPException(503, "Conversation storage is unavailable")
        if rows:
            self._check(rows[0], owner, thread_id=thread_id)
        return rows[0] if rows else None

    @staticmethod
    def _check(
        record: ThreadRecord, owner: str, *, thread_id: str | None = None, corpus: str | None = None
    ) -> None:
        if (
            record.owner != owner
            or (thread_id is not None and record.id != thread_id)
            or (corpus is not None and record.corpus != corpus)
            or record.data.thread.id != record.id
            or record.data.thread.corpus != record.corpus
            or record.data.thread.pinned_scope.corpus != record.corpus
            or any(section.scope.corpus != record.corpus for section in record.data.thread.sections)
        ):
            raise HTTPException(503, "Conversation storage is unavailable")

    def create(self, record: ThreadRecord) -> bool:
        rows = self._request(
            "POST", params={"on_conflict": "id"}, body=record.model_dump(mode="json"), create=True
        )
        if len(rows) > 1:
            raise HTTPException(503, "Conversation storage is unavailable")
        if rows:
            self._check(rows[0], record.owner, thread_id=record.id, corpus=record.corpus)
        return bool(rows)

    def replace(self, record: ThreadRecord) -> bool:
        rows = self._request(
            "PATCH",
            params={
                "owner": f"eq.{record.owner}",
                "id": f"eq.{record.id}",
                "revision": f"eq.{record.revision}",
            },
            body={"revision": record.revision + 1, "data": record.data.model_dump(mode="json")},
        )
        if len(rows) > 1:
            raise HTTPException(503, "Conversation storage is unavailable")
        if rows:
            self._check(rows[0], record.owner, thread_id=record.id, corpus=record.corpus)
            if rows[0].revision != record.revision + 1:
                raise HTTPException(503, "Conversation storage is unavailable")
        return bool(rows)

    def list(
        self, owner: str, corpus: str, limit: int, before: tuple[str, str] | None = None
    ) -> list[ThreadRecord]:
        params = {
            "owner": f"eq.{owner}",
            "corpus": f"eq.{corpus}",
            "order": "created_at.desc,id.desc",
            "limit": str(limit),
        }
        if before:
            timestamp, thread_id = before
            params["or"] = (
                f"(created_at.lt.{timestamp},and(created_at.eq.{timestamp},id.lt.{thread_id}))"
            )
        rows = self._request("GET", params=params)
        for row in rows:
            self._check(row, owner, corpus=corpus)
        return rows
