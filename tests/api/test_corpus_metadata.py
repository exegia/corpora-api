from __future__ import annotations

import json
from uuid import uuid4

import httpx
import pytest

from corpora_api.corpus_metadata import (
    CorpusMetadataConflict,
    CorpusMetadataError,
    CorpusMetadataNotFound,
    CorpusMetadataStore,
)


@pytest.fixture
def registry(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://example.test")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "test-service-role-placeholder")
    monkeypatch.setenv("HF_CORPUS_ORGANIZATION", "exegia-test")
    monkeypatch.delenv("CORPORA_SUPERADMIN_USER_ID", raising=False)
    records = {}
    requests = []

    def respond(request):
        assert request.headers["authorization"] == "Bearer test-service-role-placeholder"
        requests.append(request)
        if request.method == "POST":
            value = json.loads(request.content)
            if value["id"] in records:
                return httpx.Response(409)
            value.update(
                description=None,
                language=None,
                properties={},
                state="provisioning",
                revision=1,
                created_at="2026-01-01T00:00:00Z",
                updated_at="2026-01-01T00:00:00Z",
            )
            records[value["id"]] = value
            return httpx.Response(201, json=[value])
        if "order" in request.url.params:
            owner = request.url.params["owner_id"].removeprefix("eq.")
            after = request.url.params.get("id", "gt.").removeprefix("gt.")
            values = sorted(
                (row for row in records.values() if row["owner_id"] == owner and row["id"] > after),
                key=lambda row: row["id"],
            )
            return httpx.Response(200, json=values[: int(request.url.params["limit"])])
        identifier = request.url.params["id"].removeprefix("eq.")
        row = records.get(identifier)
        if row and request.url.params.get("owner_id", "eq." + row["owner_id"]) != (
            "eq." + row["owner_id"]
        ):
            row = None
        if request.method == "PATCH" and row:
            if request.url.params["revision"] != "eq." + str(row["revision"]):
                return httpx.Response(200, json=[])
            row.update(json.loads(request.content))
            row["revision"] += 1
        return httpx.Response(200, json=[row] if row else [])

    store = CorpusMetadataStore(httpx.Client(transport=httpx.MockTransport(respond)))
    return store, records, requests


def test_owner_reservation_creates_unique_provisioning_link(registry):
    store, _, _ = registry
    owner = str(uuid4())
    first = store.reserve(owner, str(uuid4()), " Mini ", repo_type="dataset")
    second = store.reserve(owner, str(uuid4()), "Mini", repo_type="dataset")
    assert store.reserve(owner, str(first.id), "Mini", repo_type="dataset") == first
    with pytest.raises(CorpusMetadataConflict):
        store.reserve(owner, str(first.id), "Different", repo_type="dataset")
    with pytest.raises(CorpusMetadataConflict):
        store.reserve(str(uuid4()), str(first.id), "Mini", repo_type="dataset")
    assert first.owner_id == second.owner_id
    assert first.hf_repo_id == f"exegia-test/corpus-{first.id}"
    assert first.hf_repo_id != second.hf_repo_id
    assert first.title == "Mini" and first.state == "provisioning"


def test_lost_reservation_response_retries_same_identity(registry, monkeypatch):
    store, records, _ = registry
    owner, identifier = str(uuid4()), str(uuid4())
    transport = store.client._transport
    original = transport.handler
    lost = []

    def fail_after_commit(request):
        response = original(request)
        if request.method == "POST" and not lost:
            lost.append(True)
            raise httpx.ReadTimeout("simulated lost response", request=request)
        return response

    monkeypatch.setattr(transport, "handler", fail_after_commit)
    with pytest.raises(CorpusMetadataError):
        store.reserve(owner, identifier, "Mini", repo_type="dataset")
    recovered = store.reserve(owner, identifier, "Mini", repo_type="dataset")
    assert str(recovered.id) == identifier and len(records) == 1


def test_owner_and_server_configured_superadmin_only(registry, monkeypatch):
    store, _, requests = registry
    owner, foreign, admin = (str(uuid4()) for _ in range(3))
    row = store.reserve(owner, str(uuid4()), "Mini", repo_type="dataset")
    assert store.get(owner, str(row.id)).id == row.id
    with pytest.raises(CorpusMetadataNotFound):
        store.get(foreign, str(row.id))
    assert requests[-1].url.params["owner_id"] == "eq." + foreign
    monkeypatch.setenv("CORPORA_SUPERADMIN_USER_ID", admin)
    assert store.get(admin, str(row.id)).id == row.id
    updated = store.update_title(admin, str(row.id), 1, "Changed")
    assert updated.title == "Changed" and updated.revision == 2
    monkeypatch.setenv("CORPORA_SUPERADMIN_USER_ID", "not-a-user-id")
    with pytest.raises(CorpusMetadataError):
        store.get(foreign, str(row.id))


def test_stale_edits_do_not_overwrite_and_foreign_actor_cannot_edit(registry):
    store, _, _ = registry
    owner, foreign = str(uuid4()), str(uuid4())
    row = store.reserve(owner, str(uuid4()), "Mini", repo_type="dataset")
    assert store.update_title(owner, str(row.id), 1, "First").revision == 2
    with pytest.raises(CorpusMetadataConflict):
        store.update_title(owner, str(row.id), 1, "Stale")
    with pytest.raises(CorpusMetadataNotFound):
        store.update_title(foreign, str(row.id), 2, "Foreign")
    assert store.get(owner, str(row.id)).title == "First"


def test_owner_listing_is_bounded_and_superadmin_may_list_another_owner(registry, monkeypatch):
    store, _, _ = registry
    owner, foreign, admin = (str(uuid4()) for _ in range(3))
    for _ in range(3):
        store.reserve(owner, str(uuid4()), "Mini", repo_type="dataset")
    store.reserve(foreign, str(uuid4()), "Other", repo_type="model")
    page, cursor = store.list_owned(owner, limit=2)
    following, end = store.list_owned(owner, limit=2, after=cursor)
    assert len(page) == 2 and len(following) == 1 and end is None
    assert {item.owner_id for item in page + following} == {page[0].owner_id}
    with pytest.raises(CorpusMetadataNotFound):
        store.list_owned(foreign, owner_id=owner)
    monkeypatch.setenv("CORPORA_SUPERADMIN_USER_ID", admin)
    assert len(store.list_owned(admin, owner_id=owner)[0]) == 3


def test_unconfigured_namespace_and_invalid_actor_fail_before_request(registry, monkeypatch):
    store, _, requests = registry
    monkeypatch.setenv("HF_CORPUS_ORGANIZATION", "wrong/namespace")
    with pytest.raises(CorpusMetadataError):
        store.reserve(str(uuid4()), str(uuid4()), "Mini", repo_type="dataset")
    with pytest.raises(CorpusMetadataError):
        store.get("not-a-verified-uuid", str(uuid4()))
    assert not requests


def test_mismatched_owner_from_service_is_rejected(registry):
    store, records, _ = registry
    owner = str(uuid4())
    row = store.reserve(owner, str(uuid4()), "Mini", repo_type="dataset")
    records[str(row.id)]["id"] = str(uuid4())
    with pytest.raises(CorpusMetadataError):
        store.get(owner, str(row.id))


def test_links_outside_the_configured_organization_fail_closed(registry):
    store, records, _ = registry
    owner = str(uuid4())
    row = store.reserve(owner, str(uuid4()), "Mini", repo_type="dataset")
    records[str(row.id)]["hf_repo_id"] = f"someone-else/corpus-{row.id}"
    with pytest.raises(CorpusMetadataError):
        store.get(owner, str(row.id))
    with pytest.raises(CorpusMetadataError):
        store.list_owned(owner)


def test_owner_and_superadmin_update_multiple_metadata_fields(registry, monkeypatch):
    store, _, _ = registry
    owner, admin, foreign, identifier = (str(uuid4()) for _ in range(4))
    row = store.reserve(owner, identifier, "Original", repo_type="dataset")
    monkeypatch.setenv("CORPORA_SUPERADMIN_USER_ID", admin)
    edited = store.update_metadata(
        owner,
        identifier,
        row.revision,
        {
            "title": "  Revised  ",
            "description": "A corpus",
            "language": "grc",
            "properties": {"source": {"edition": "2026"}, "tags": ["text", "tf"]},
        },
    )
    assert edited.title == "Revised"
    assert edited.description == "A corpus" and edited.language == "grc"
    assert edited.properties == {"source": {"edition": "2026"}, "tags": ["text", "tf"]}
    assert edited.revision == 2 and edited.hf_repo_id == row.hf_repo_id
    cleared = store.update_metadata(
        admin, identifier, edited.revision, {"description": None, "properties": {}}
    )
    assert cleared.description is None and cleared.properties == {} and cleared.language == "grc"
    with pytest.raises(CorpusMetadataConflict):
        store.update_metadata(owner, identifier, edited.revision, {"language": "en"})
    with pytest.raises(CorpusMetadataNotFound):
        store.update_metadata(foreign, identifier, cleared.revision, {"title": "Foreign"})


@pytest.mark.parametrize(
    "fields",
    [
        {},
        {"owner_id": str(uuid4())},
        {"state": "ready"},
        {"hf_repo_id": "other/repository"},
        {"title": "   "},
        {"title": None},
        {"description": "x" * 10001},
        {"language": "x" * 129},
        {"properties": []},
        {"properties": {"key": "x" * 16384}},
        {"properties": {"key": float("nan")}},
        {"properties": {"nested": {"bad": "null\x00byte"}}},
        {"properties": {1: "non-string key"}},
        {"description": "\ud800"},
    ],
)
def test_invalid_metadata_updates_fail_before_registry_access(registry, fields):
    store, _, requests = registry
    with pytest.raises(CorpusMetadataError):
        store.update_metadata(str(uuid4()), str(uuid4()), 1, fields)
    assert requests == []


def test_metadata_update_rejects_changed_boolean_in_server_response(registry, monkeypatch):
    store, _, _ = registry
    owner, identifier = str(uuid4()), str(uuid4())
    store.reserve(owner, identifier, "Original", repo_type="dataset")
    transport = store.client._transport
    original = transport.handler

    def changed_response(request):
        response = original(request)
        if request.method != "PATCH":
            return response
        rows = response.json()
        rows[0]["properties"]["enabled"] = 1
        return httpx.Response(200, json=rows)

    monkeypatch.setattr(transport, "handler", changed_response)
    with pytest.raises(CorpusMetadataError, match="invalid data"):
        store.update_metadata(owner, identifier, 1, {"properties": {"enabled": True}})


@pytest.mark.parametrize("revision", [True, 0, -1, (1 << 63) - 1, 1 << 80])
def test_invalid_revision_fails_before_registry_access(registry, revision):
    store, _, requests = registry
    with pytest.raises(CorpusMetadataError):
        store.update_metadata(str(uuid4()), str(uuid4()), revision, {"title": "New"})
    assert requests == []
