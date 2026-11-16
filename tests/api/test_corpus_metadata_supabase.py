"""Opt-in registry checks against disposable local Supabase."""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

import httpx
import pytest

from corpora_api.auth import verify_user_token
from corpora_api.corpus_hub_deadline import DeadlineCorpusHubReader
from corpora_api.corpus_hub_read import CorpusHubReader, CorpusHubReadError
from corpora_api.corpus_metadata import (
    CorpusMetadataConflict,
    CorpusMetadataNotFound,
    CorpusMetadataStore,
)

pytestmark = pytest.mark.skipif(
    not os.getenv("CORPORA_TEST_LOCAL_SUPABASE"), reason="requires disposable local Supabase"
)


def test_owner_superadmin_cas_and_browser_denial(monkeypatch):
    psycopg = pytest.importorskip("psycopg")
    url, dsn = os.environ["API_URL"].rstrip("/"), os.environ["DB_URL"]
    assert urlparse(url).hostname in {"localhost", "127.0.0.1", "::1"}
    assert urlparse(dsn).hostname in {"localhost", "127.0.0.1", "::1"}
    anon, service = os.environ["ANON_KEY"], os.environ["SERVICE_ROLE_KEY"]
    monkeypatch.setenv("SUPABASE_URL", url)
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", service)
    monkeypatch.setenv("SUPABASE_JWKS_URL", url + "/auth/v1/.well-known/jwks.json")
    monkeypatch.setenv("SUPABASE_JWT_ISSUER", url + "/auth/v1")
    monkeypatch.setenv("HF_CORPUS_ORGANIZATION", "fixture-org")
    users = []
    migration = (
        Path(__file__).resolve().parents[2]
        / "supabase/migrations/20261116043420_corpus_metadata_registry.sql"
    )
    with psycopg.connect(dsn, autocommit=True) as db, httpx.Client(timeout=15) as client:
        assert db.execute(
            "SELECT to_regclass('public.corpora_corpus_metadata') IS NULL"
        ).fetchone()[0]
        try:
            db.execute(migration.read_text())
            db.execute("NOTIFY pgrst, 'reload schema'")
            for _ in range(3):
                response = client.post(
                    url + "/auth/v1/signup",
                    headers={"apikey": anon},
                    json={"email": f"{uuid4()}@example.org", "password": str(uuid4())},
                )
                response.raise_for_status()
                token = response.json()["access_token"]
                users.append((verify_user_token(token)["sub"], token))
            time.sleep(1)
            store = CorpusMetadataStore()
            identifier = str(uuid4())
            record = store.reserve(users[0][0], identifier, "Mini", repo_type="dataset")
            assert record.state == "provisioning"
            assert store.reserve(users[0][0], identifier, "Mini", repo_type="dataset") == record
            assert [item.id for item in store.list_owned(users[0][0])[0]] == [record.id]
            with pytest.raises(CorpusMetadataNotFound):
                store.get(users[1][0], identifier)
            with pytest.raises(CorpusMetadataNotFound):
                store.list_owned(users[1][0], owner_id=users[0][0])
            monkeypatch.setenv("CORPORA_SUPERADMIN_USER_ID", users[2][0])
            assert [item.id for item in store.list_owned(users[2][0], owner_id=users[0][0])[0]] == [
                record.id
            ]
            updated = store.update_title(users[2][0], identifier, 1, "Edited")
            assert updated.revision == 2 and updated.title == "Edited"
            with pytest.raises(CorpusMetadataConflict):
                store.update_title(users[0][0], identifier, 1, "Stale")

            # Direct seeding is test setup, not an implemented readiness transition.
            db.execute(
                "UPDATE public.corpora_corpus_metadata SET state='ready' WHERE id=%s",
                (identifier,),
            )
            monkeypatch.setenv("HF_CORPUS_READ_TOKEN", "test-corpus-read-token-placeholder")
            revision = "a" * 40
            files = {"otype.tf": b"Text-Fabric bytes", "cache.cfr": b"cache bytes"}
            hub_calls = []

            def hub_response(request):
                hub_calls.append(request)
                assert (
                    request.headers["authorization"] == "Bearer test-corpus-read-token-placeholder"
                )
                if request.url.path.startswith("/api/"):
                    payload = {"id": record.hf_repo_id, "sha": revision, "private": True}
                    return httpx.Response(
                        200, stream=httpx.ByteStream(json.dumps(payload).encode())
                    )
                name = request.url.path.rsplit("/", 1)[-1]
                return httpx.Response(200, stream=httpx.ByteStream(files[name]))

            with httpx.Client(transport=httpx.MockTransport(hub_response)) as hub:
                reader = CorpusHubReader(store, hub)
                for name, content in files.items():
                    with reader._open(
                        users[0][0], identifier, revision, name, max_bytes=100
                    ) as chunks:
                        assert b"".join(chunks) == content
                with reader._open(
                    users[2][0], identifier, revision, "otype.tf", max_bytes=100
                ) as chunks:
                    assert b"".join(chunks) == files["otype.tf"]
                previous_calls = len(hub_calls)
                with pytest.raises(CorpusMetadataNotFound):
                    with reader._open(users[1][0], identifier, revision, "otype.tf", max_bytes=100):
                        pytest.fail("foreign caller reached Hub")
                assert len(hub_calls) == previous_calls

            async def async_reader_check():
                class MixedTransport(httpx.AsyncBaseTransport):
                    def __init__(self):
                        self.real = httpx.AsyncHTTPTransport()

                    async def handle_async_request(self, request):
                        if request.url.host == "huggingface.co":
                            return hub_response(request)
                        return await self.real.handle_async_request(request)

                    async def aclose(self):
                        await self.real.aclose()

                async def read(actor):
                    output = bytearray()

                    async def sink(chunk):
                        output.extend(chunk)

                    await DeadlineCorpusHubReader(MixedTransport).transfer(
                        actor, identifier, revision, "otype.tf", max_bytes=100, timeout=5, sink=sink
                    )
                    return bytes(output)

                assert await read(users[0][0]) == files["otype.tf"]
                assert await read(users[2][0]) == files["otype.tf"]
                before = len(hub_calls)
                with pytest.raises(CorpusHubReadError):
                    await read(users[1][0])
                assert len(hub_calls) == before

            asyncio.run(async_reader_check())
            for _, token in users:
                response = client.get(
                    url + "/rest/v1/corpora_corpus_metadata",
                    headers={"apikey": anon, "Authorization": "Bearer " + token},
                )
                assert response.status_code in (401, 403)
        finally:
            db.execute("DROP TABLE IF EXISTS public.corpora_corpus_metadata")
            db.execute("DROP FUNCTION IF EXISTS public.corpora_corpus_metadata_stamp()")
            db.execute("NOTIFY pgrst, 'reload schema'")
            for user, _ in users:
                client.delete(
                    url + "/auth/v1/admin/users/" + user,
                    headers={"apikey": service, "Authorization": "Bearer " + service},
                ).raise_for_status()
