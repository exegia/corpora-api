"""Reservation identity is owner-scoped and survives uncertain inserts."""

import json
import uuid

import httpx
import pytest

from corpora_api.ingest_requests import IngestRequestConflict, input_fingerprint, reserve_request
from corpora_api.ingest_store import IngestStore, IngestStoreError

ALICE = "b9a94f14-dfc2-4699-ab8c-77378136f2b4"
BOB = "477db53d-74f4-4ae4-8c51-5f1d2282821d"


def test_fingerprint_covers_effective_input():
    def digest(filename="a.txt", source=b"abc", name="n", language="en", corpus_id=""):
        return input_fingerprint(
            filename, source, name=name, language=language, corpus_id=corpus_id
        )

    expected = digest()
    assert digest() == expected
    assert all(
        value != expected
        for value in (
            digest(filename="b.txt"),
            digest(source=b"abd"),
            digest(name="m"),
            digest(language="fr"),
            digest(corpus_id=str(uuid.uuid4())),
        )
    )


def test_reservations_handle_retries_and_lost_insert(monkeypatch):
    for name, value in {
        "SUPABASE_URL": "https://db.test",
        "SUPABASE_SERVICE_ROLE_KEY": "placeholder",
        "SUPABASE_JOBS_BUCKET": "jobs",
    }.items():
        monkeypatch.setenv(name, value)
    rows = {}
    lose_response = True

    def respond(request):
        nonlocal lose_response
        if request.method == "POST":
            row = json.loads(request.content)
            index = (row["owner_id"], row["request_key_hash"])
            if index in rows:
                return httpx.Response(409)
            rows[index] = row
            if lose_response:
                lose_response = False
                raise httpx.ReadTimeout("response lost", request=request)
            return httpx.Response(201)
        index = (request.url.params["owner_id"][3:], request.url.params["request_key_hash"][3:])
        return httpx.Response(200, json=[rows[index]] if index in rows else [])

    store = IngestStore(httpx.Client(transport=httpx.MockTransport(respond)))
    digest = input_fingerprint("a.txt", b"abc", name="", language="en", corpus_id="")
    first = reserve_request(store, ALICE, "retry-1", digest)
    assert reserve_request(store, ALICE, "retry-1", digest) == first
    assert reserve_request(store, BOB, "retry-1", digest) != first
    with pytest.raises(IngestRequestConflict):
        reserve_request(store, ALICE, "retry-1", "f" * 64)
    with pytest.raises(ValueError):
        reserve_request(store, ALICE, "white space", digest)


def test_unconfirmed_reservation_fails_closed(monkeypatch):
    for name, value in {
        "SUPABASE_URL": "https://db.test",
        "SUPABASE_SERVICE_ROLE_KEY": "placeholder",
        "SUPABASE_JOBS_BUCKET": "jobs",
    }.items():
        monkeypatch.setenv(name, value)
    store = IngestStore(
        httpx.Client(
            transport=httpx.MockTransport(
                lambda request: (
                    httpx.Response(503)
                    if request.method == "POST"
                    else httpx.Response(200, json=[])
                )
            )
        )
    )
    with pytest.raises(IngestStoreError):
        reserve_request(store, ALICE, "retry", "a" * 64)
