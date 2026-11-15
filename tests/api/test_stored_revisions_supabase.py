"""Opt-in integration against a disposable, loopback-only Supabase stack."""

from __future__ import annotations

import hashlib
import os
from datetime import UTC, datetime
from urllib.parse import urlparse
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException

from corpora_api.stored_revisions import BUCKET, HistoryCursor, Revision, StoredRevisions

pytestmark = pytest.mark.skipif(
    not os.getenv("CORPORA_TEST_LOCAL_SUPABASE"), reason="requires disposable local Supabase"
)


def test_real_storage_postgrest_registry_and_browser_denial(monkeypatch):
    url = os.environ["API_URL"].rstrip("/")
    assert urlparse(url).hostname in {"localhost", "127.0.0.1", "::1"}
    service, anon = os.environ["SERVICE_ROLE_KEY"], os.environ["ANON_KEY"]
    monkeypatch.setenv("SUPABASE_URL", url)
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", service)
    with httpx.Client(timeout=30) as client:

        def signup():
            result = client.post(
                url + "/auth/v1/signup",
                headers={"apikey": anon, "Authorization": "Bearer " + anon},
                json={"email": f"{uuid4()}@example.org", "password": str(uuid4())},
            )
            assert result.status_code == 200
            body = result.json()
            return body["user"]["id"], body["access_token"]

        owner, token = signup()
        other, _ = signup()
        store = StoredRevisions(client)
        data1, data2 = b"first test snapshot", b"second test snapshot"

        def fact(data, version):
            return Revision(
                owner=owner,
                filename="test.corpus",
                revision=uuid4(),
                digest="sha256:" + hashlib.sha256(data).hexdigest(),
                version=version,
            )

        first, second = fact(data1, "v1.0"), fact(data2, "v1.1")
        store.publish(first, data1, None)
        assert store.get(owner, "test.corpus").data == data1
        store.publish(second, data2, str(first.revision))
        assert store.get(owner, "test.corpus").data == data2
        assert store.get(owner, "test.corpus", str(first.revision)).data == data1
        with pytest.raises(HTTPException) as denied:
            store.get(other, "test.corpus", str(first.revision))
        assert denied.value.status_code == 404
        with pytest.raises(HTTPException) as stale:
            store.publish(fact(b"stale", "v1.2"), b"stale", str(first.revision))
        assert stale.value.status_code == 409
        assert store.get(owner, "test.corpus").data == data2
        page = store.history(owner, "test.corpus", 1)
        assert [row.revision for row in page.entries] == [second.revision]
        assert page.next_cursor is not None
        data3 = b"third test snapshot"
        third = fact(data3, "v1.2")
        store.publish(third, data3, str(second.revision))
        older = store.history(owner, "test.corpus", 1, page.next_cursor)
        assert [row.revision for row in older.entries] == [first.revision]
        assert older.next_cursor is None
        with pytest.raises(HTTPException) as foreign_history:
            store.history(other, "test.corpus", 1)
        assert foreign_history.value.status_code == 404
        response = client.get(
            f"{url}/storage/v1/object/{BUCKET}/{store._key(first)}",
            headers={"apikey": anon, "Authorization": "Bearer " + token},
        )
        assert response.status_code in (400, 403, 404)
        direct = client.post(
            url + "/rest/v1/rpc/corpora_stored_get",
            headers={"apikey": anon, "Authorization": "Bearer " + token},
            json={"p_owner": owner, "p_filename": "test.corpus", "p_revision": None},
        )
        assert direct.status_code in (401, 403, 404)
        direct_history = client.post(
            url + "/rest/v1/rpc/corpora_stored_history",
            headers={"apikey": anon, "Authorization": "Bearer " + token},
            json={"p_owner": owner, "p_filename": "test.corpus", "p_limit": 1},
        )
        assert direct_history.status_code in (401, 403, 404)


def test_history_ties_and_missing_snapshot_fail_closed(monkeypatch):
    psycopg = pytest.importorskip("psycopg")
    url, dsn = os.environ["API_URL"].rstrip("/"), os.environ["DB_URL"]
    assert urlparse(url).hostname in {"localhost", "127.0.0.1", "::1"}
    assert urlparse(dsn).hostname in {"localhost", "127.0.0.1", "::1"}
    monkeypatch.setenv("SUPABASE_URL", url)
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", os.environ["SERVICE_ROLE_KEY"])
    owner, revisions = str(uuid4()), [uuid4() for _ in range(3)]
    with psycopg.connect(dsn) as db:
        for revision in revisions:
            db.execute(
                "INSERT INTO public.corpora_stored_revisions "
                "(owner,filename,revision,digest,version,created_at) VALUES (%s,%s,%s,%s,%s,%s)",
                (
                    owner,
                    "tie.corpus",
                    str(revision),
                    "sha256:" + "a" * 64,
                    "v1.0",
                    datetime(2026, 1, 1, tzinfo=UTC),
                ),
            )
        db.execute(
            "INSERT INTO public.corpora_stored_heads VALUES (%s,%s,%s)",
            (owner, "tie.corpus", str(revisions[0])),
        )
    store = StoredRevisions()
    cursor, seen = None, []
    while True:
        page = store.history(owner, "tie.corpus", 1, cursor)
        seen.extend(row.revision for row in page.entries)
        cursor = page.next_cursor
        if cursor is None:
            break
    assert seen == sorted(revisions, reverse=True)
    with pytest.raises(HTTPException) as missing:
        store.get(owner, "tie.corpus", str(seen[0]))
    assert missing.value.status_code == 503
    with pytest.raises(HTTPException) as naive:
        store.history(owner, "tie.corpus", 1, HistoryCursor(datetime(2026, 1, 1), uuid4()))
    assert naive.value.status_code == 422
