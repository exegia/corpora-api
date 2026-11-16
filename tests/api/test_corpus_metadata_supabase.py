"""Opt-in registry checks against disposable local Supabase."""

from __future__ import annotations

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

import httpx
import pytest

from corpora_api.auth import verify_user_token
from corpora_api.corpus_metadata import (
    CorpusMetadataConflict,
    CorpusMetadataError,
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
            enriched = store.update_metadata(
                users[0][0],
                identifier,
                2,
                {
                    "description": "Example corpus",
                    "language": "grc",
                    "properties": {"source": {"edition": "2026"}},
                },
            )
            assert enriched.revision == 3 and enriched.language == "grc"
            assert enriched.description == "Example corpus"
            assert enriched.properties == {"source": {"edition": "2026"}}
            with pytest.raises(CorpusMetadataNotFound):
                store.update_metadata(users[1][0], identifier, 3, {"language": "en"})
            cleared = store.update_metadata(users[2][0], identifier, 3, {"description": None})
            assert cleared.revision == 4 and cleared.description is None
            assert CorpusMetadataStore().get(users[0][0], identifier) == cleared
            # PostgreSQL expands scientific notation in JSONB text, so the
            # database limit can reject a much smaller request serialization.
            expanded = {str(index): 1e-300 for index in range(80)}
            with pytest.raises(CorpusMetadataError, match="Invalid corpus metadata update"):
                store.update_metadata(users[0][0], identifier, 4, {"properties": expanded})
            assert store.get(users[0][0], identifier) == cleared
            barrier = threading.Barrier(2)

            def concurrent_update(language):
                candidate = CorpusMetadataStore()
                barrier.wait(timeout=10)
                try:
                    return candidate.update_metadata(
                        users[0][0], identifier, 4, {"language": language}
                    ).language
                except CorpusMetadataConflict:
                    return "conflict"
                finally:
                    candidate.client.close()

            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(concurrent_update, ("en", "la")))
            assert results.count("conflict") == 1
            winner = next(value for value in results if value != "conflict")
            after_race = store.get(users[0][0], identifier)
            assert after_race.language == winner and after_race.revision == 5
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
