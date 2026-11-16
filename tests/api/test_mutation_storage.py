"""The hosted journal must never trust an upload or a foreign RPC row."""

from __future__ import annotations

import hashlib
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from corpora_api import mutation_drafts
from corpora_api.corpus_process import CorpusProcessError
from corpora_api.drafts import DraftStore
from corpora_api.mutation_journal import CurationError
from corpora_api.mutation_storage import MutationStorage, SupabaseMutationJournal


def test_oversized_mutation_worker_output_maps_to_413(monkeypatch):
    def fail(*_args, **_kwargs):
        raise CorpusProcessError("too_large", "result exceeds limit")

    monkeypatch.setattr(mutation_drafts, "run_corpus", fail)
    model = SimpleNamespace(model_dump=lambda **_: {})
    with pytest.raises(CurationError) as caught:
        mutation_drafts.ArchiveMutationDrafts._worker(b"archive", "mutation_edit", model, model)
    assert caught.value.status == 413


def test_immutable_stage_requires_exact_readback(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://example.invalid")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "placeholder-key")
    owner, document, previous, next_revision = (str(uuid4()) for _ in range(4))
    previous_data, data = b"original", b"changed"

    def digest(value):
        return "sha256:" + hashlib.sha256(value).hexdigest()

    objects = {f"{owner}/ai/{document}/{previous}.corpus": previous_data}
    calls = []

    def transport(request: httpx.Request) -> httpx.Response:
        key = request.url.path.split("/corpus-ai-drafts/", 1)[1]
        calls.append((request.method, key))
        if request.method == "POST":
            assert request.headers["x-upsert"] == "false"
            if key in objects:
                return httpx.Response(409)
            objects[key] = request.content
            return httpx.Response(201)
        if key not in objects:
            return httpx.Response(404)
        return httpx.Response(200, content=objects[key])

    drafts = DraftStore(httpx.Client(transport=httpx.MockTransport(transport)))
    head = drafts._head(
        {
            "document_id": document,
            "owner": owner,
            "corpus": "draft:" + document,
            "bucket": "corpus-ai-drafts",
            "object_key": f"{owner}/ai/{document}/{previous}.corpus",
            "revision": previous,
            "digest": digest(previous_data),
            "version": "1.0",
            "state": "draft",
        },
        owner,
        document,
    )
    storage = MutationStorage(drafts)
    key = storage.stage(head, next_revision, data, digest(data))
    assert key == f"{owner}/ai/{document}/{next_revision}.corpus"
    assert storage.stage(head, next_revision, data, digest(data)) == key
    assert calls[-2:] == [("POST", key), ("GET", key)]
    objects[key] = b"wrong"
    with pytest.raises(CurationError) as error:
        storage.stage(head, next_revision, data, digest(data))
    assert error.value.status == 409
    with pytest.raises(CurationError):
        storage.stage(head, "../elsewhere", data, digest(data))


def test_hosted_journal_rejects_foreign_or_mismatched_rows():
    owner, other, operation = (str(uuid4()) for _ in range(3))

    class Storage:
        def rpc(self, action, user, payload):
            assert action == "get" and user == owner and payload["id"] == operation
            return {"intent": {"owner": other}}

    with pytest.raises(CurationError) as error:
        SupabaseMutationJournal(Storage()).get(owner, operation)
    assert error.value.status == 503


def test_undo_rejects_inconsistent_change_link_before_publication(monkeypatch):
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from corpora_api.models import AIChatSuggestion
    from corpora_api.mutation_journal import Evidence, Intent, Plan, Record
    from corpora_api.mutations import MutationService

    owner, operation, wrong_change = (str(uuid4()) for _ in range(3))
    suggestion = AIChatSuggestion(
        id="s",
        scope={
            "corpus": "draft:fixture",
            "level": "word",
            "node_id": 1,
            "label": "word",
            "version": "1.0",
        },
        kind="annotation",
        target_node=1,
        diff=[{"field": "lemma", "old": "a", "new": "b"}],
        rationale="fixture",
        base_version="1.0",
        content_hash="h",
    )
    before = Evidence(
        revision="before", digest="d1", version="1.0", content_hash="h", values={"lemma": "a"}
    )
    after = Evidence(
        revision="after", digest="d2", version="1.1", content_hash="h", values={"lemma": "b"}
    )
    now = datetime.now(UTC)
    original = Record(
        intent=Intent(
            id=operation,
            owner=owner,
            suggestion=suggestion,
            plan=Plan(before=before, after=after),
            created_at=now,
        ),
        applied_at=now,
    )
    called = []
    service = MutationService(DraftStore(), SimpleNamespace())
    monkeypatch.setattr(service, "_enabled", lambda user: None)
    monkeypatch.setattr(
        service.storage, "rpc", lambda action, user, payload: original.model_dump(mode="json")
    )
    monkeypatch.setattr(service.storage, "resolve", lambda user, corpus: None)
    monkeypatch.setattr(service.engine, "undo", lambda *args: called.append(args))
    with pytest.raises(CurationError) as error:
        service.undo(owner, wrong_change)
    assert error.value.status == 503 and called == []
