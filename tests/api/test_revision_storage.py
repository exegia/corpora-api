"""Private snapshot reads require registry facts and digest-verified bytes."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException

from corpora_api.drafts import DraftStore
from corpora_api.models import AIChatSuggestion
from corpora_api.mutation_journal import CurationError, Evidence, Intent, Plan, _operation_id
from corpora_api.mutation_storage import MutationStorage


def test_revision_read_verifies_owner_receipt_and_immutable_object(monkeypatch):
    owner, other, document, current, revision = (str(uuid4()) for _ in range(5))
    corpus = "draft:" + document
    data = b"historical archive"
    digest = "sha256:" + hashlib.sha256(data).hexdigest()
    current_digest = "sha256:" + "a" * 64
    key = f"{owner}/ai/{document}/{revision}.corpus"
    suggestion = AIChatSuggestion(
        id="test",
        scope={"corpus": corpus, "level": "word", "node_id": 1, "label": "word", "version": "v1.0"},
        kind="annotation",
        target_node=1,
        diff=[{"field": "lemma", "old": "old", "new": "new"}],
        rationale="test",
        base_version="v1.0",
        content_hash="hash",
    )
    before = Evidence(
        revision=revision,
        digest=digest,
        version="v1.0",
        content_hash="hash",
        values={"lemma": "old"},
    )
    after = Evidence(
        revision=current,
        digest=current_digest,
        version="v1.2",
        content_hash="new",
        values={"lemma": "new"},
    )
    moment = datetime.now(UTC)
    intent = Intent(
        id=_operation_id(owner, suggestion.id, None),
        owner=owner,
        suggestion=suggestion,
        plan=Plan(before=before, after=after),
        created_at=moment,
    )
    result = {"revision": revision, "digest": digest, "version": "v1.0", "operations": [intent.id]}
    record = {"intent": intent.model_dump(mode="json"), "applied_at": moment.isoformat()}
    receipt = {"proof": intent.proof, "applied_at": moment.isoformat()}
    objects = {key: data}
    calls = []

    def transport(request):
        calls.append(request)
        if request.url.path.endswith("/rpc/corpora_ai_revision_lookup"):
            assert json.loads(request.read()) == {
                "p_owner": owner,
                "p_document": document,
                "p_revision": revision,
            }
            return httpx.Response(200, json=result)
        if request.url.path.endswith("/rpc/corpora_ai_storage"):
            body = json.loads(request.read())
            assert body["p_owner"] == owner and body["p_payload"] == {"id": intent.id}
            return httpx.Response(200, json=record if body["p_action"] == "get" else receipt)
        object_key = request.url.path.split("/corpus-ai-drafts/", 1)[1]
        return httpx.Response(200, content=objects.get(object_key, b"tampered"))

    monkeypatch.setenv("SUPABASE_URL", "https://example.invalid")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "placeholder-key")
    drafts = DraftStore(httpx.Client(transport=httpx.MockTransport(transport)))
    head = drafts._head(
        {
            "document_id": document,
            "owner": owner,
            "corpus": corpus,
            "bucket": "corpus-ai-drafts",
            "object_key": f"{owner}/ai/{document}/{current}.corpus",
            "revision": current,
            "digest": current_digest,
            "version": "v1.2",
            "state": "draft",
        },
        owner,
        document,
    )

    def resolve(principal, requested):
        if principal != owner or requested != document:
            raise HTTPException(404, "Working draft not found")
        return head

    monkeypatch.setattr(drafts, "resolve", resolve)
    storage = MutationStorage(drafts)
    selected, downloaded = storage.download_revision(owner, corpus, revision)
    assert downloaded == data and selected.object_key == key and selected.version == "v1.0"
    assert calls[-1].url.path.endswith("/corpus-ai-drafts/" + key)
    with pytest.raises(CurationError) as foreign:
        storage.download_revision(other, corpus, revision)
    assert foreign.value.status == 404
    objects[key] = b"wrong"
    with pytest.raises(CurationError) as mismatch:
        storage.download_revision(owner, corpus, revision)
    assert mismatch.value.status == 409
    objects[key] = data
    receipt["proof"] = "f" * 64
    with pytest.raises(CurationError) as bad_proof:
        storage.download_revision(owner, corpus, revision)
    assert bad_proof.value.status == 503
    receipt["proof"] = intent.proof
    record["intent"]["suggestion"]["rationale"] = "tampered"
    with pytest.raises(CurationError) as altered_intent:
        storage.download_revision(owner, corpus, revision)
    assert altered_intent.value.status == 503
    record["intent"]["suggestion"]["rationale"] = "test"
    result["operations"] = []
    with pytest.raises(CurationError) as no_witness:
        storage.download_revision(owner, corpus, revision)
    assert no_witness.value.status == 503
    result["operations"] = [intent.id]
    result["revision"] = str(uuid4())
    with pytest.raises(CurationError) as wrong:
        storage.download_revision(owner, corpus, revision)
    assert wrong.value.status == 503
    result["revision"] = revision
    result["digest"] = "../attacker"
    with pytest.raises(CurationError) as invalid:
        storage.download_revision(owner, corpus, revision)
    assert invalid.value.status == 503
