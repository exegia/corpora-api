"""Cross-branch local test of session locking and real revision verification."""

from __future__ import annotations

import hashlib
import importlib
import os
import threading
import time
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException

from corpora_api.auth import verify_user_token
from corpora_api.session_read import SessionBoundRead, SessionBoundRevisions

pytestmark = pytest.mark.skipif(
    not os.getenv("CORPORA_TEST_LOCAL_SUPABASE") or not os.getenv("CORPORA_STORED_ADAPTER_ROOT"),
    reason="requires disposable local Supabase and the draft revision adapter checkout",
)


def test_guarded_registered_snapshot_and_logout(monkeypatch):
    psycopg = pytest.importorskip("psycopg")
    import corpora_api

    adapter = Path(os.environ["CORPORA_STORED_ADAPTER_ROOT"]) / "src/corpora_api"
    monkeypatch.setattr(corpora_api, "__path__", [*corpora_api.__path__, str(adapter)])
    revisions_module = importlib.import_module("corpora_api.stored_revisions")
    assert Path(revisions_module.__file__).resolve() == (adapter / "stored_revisions.py").resolve()
    Revision, StoredRevisions = revisions_module.Revision, revisions_module.StoredRevisions
    url, dsn = os.environ["API_URL"].rstrip("/"), os.environ["DB_URL"]
    assert urlparse(url).hostname in {"localhost", "127.0.0.1", "::1"}
    assert urlparse(dsn).hostname in {"localhost", "127.0.0.1", "::1"}
    anon, service = os.environ["ANON_KEY"], os.environ["SERVICE_ROLE_KEY"]
    monkeypatch.setenv("SUPABASE_URL", url)
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", service)
    monkeypatch.setenv("SUPABASE_JWKS_URL", url + "/auth/v1/.well-known/jwks.json")
    monkeypatch.setenv("SUPABASE_JWT_ISSUER", url + "/auth/v1")
    service_headers = {"apikey": service, "Authorization": "Bearer " + service}
    ready, release, gate = threading.Event(), threading.Event(), threading.Event()
    backend_pid, results, errors, logout_result, calls = [], [], [], [], []

    def pause_storage(response):
        if (
            gate.is_set()
            and response.request.method == "GET"
            and "/storage/v1/object/corpus-stored-revisions/" in response.request.url.path
        ):
            ready.set()
            if not release.wait(15):
                raise TimeoutError("Storage read not released")

    def connect():
        db = psycopg.connect(dsn, autocommit=True)
        db.execute("SET ROLE service_role")
        backend_pid.append(db.info.backend_pid)
        return db

    with (
        httpx.Client(timeout=15) as auth,
        httpx.Client(timeout=15, event_hooks={"response": [pause_storage]}) as storage_client,
    ):

        def signup():
            response = auth.post(
                url + "/auth/v1/signup",
                headers={"apikey": anon, "Authorization": "Bearer " + anon},
                json={"email": f"{uuid4()}@example.org", "password": str(uuid4())},
            )
            assert response.status_code == 200
            token = response.json()["access_token"]
            return token, verify_user_token(token)

        token, claims = signup()
        other_token, other = signup()
        store = StoredRevisions(storage_client)
        data = b"registry-verified snapshot"
        fact = Revision(
            owner=claims["sub"],
            filename="owned.corpus",
            revision=uuid4(),
            digest="sha256:" + hashlib.sha256(data).hexdigest(),
            version="v1.0",
        )
        store.publish(fact, data, None)

        class CountedStore:
            def get(self, owner, filename, revision=None):
                calls.append((owner, filename, revision))
                return store.get(owner, filename, revision)

        guarded = SessionBoundRevisions(SessionBoundRead(connect), CountedStore())
        with pytest.raises(HTTPException) as wrong:
            guarded.get({**claims, "sub": other["sub"]}, "owned.corpus")
        assert wrong.value.status_code == 401 and not calls
        backend_pid.clear()

        def read():
            try:
                results.append(guarded.get(claims, "owned.corpus", str(fact.revision)))
            except Exception as error:
                errors.append(error)
                ready.set()

        def logout():
            try:
                response = auth.post(
                    url + "/auth/v1/logout",
                    headers={"apikey": anon, "Authorization": "Bearer " + token},
                )
                logout_result.append(response.status_code)
            except Exception as error:
                errors.append(error)

        gate.set()
        reader, revoker = threading.Thread(target=read), threading.Thread(target=logout)
        reader.start()
        try:
            assert ready.wait(15) and not errors
            revoker.start()
            with psycopg.connect(dsn, autocommit=True) as monitor:
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    blocked = monitor.execute(
                        "SELECT EXISTS(SELECT 1 FROM pg_stat_activity "
                        "WHERE %s = ANY(pg_blocking_pids(pid)))",
                        (backend_pid[0],),
                    ).fetchone()[0]
                    if blocked:
                        break
                    time.sleep(0.05)
                else:
                    pytest.fail("logout did not wait for the verified revision download")
        finally:
            release.set()
            reader.join(20)
            if revoker.ident:
                revoker.join(20)
        gate.clear()
        assert not reader.is_alive() and not revoker.is_alive() and not errors
        assert logout_result in ([200], [204]) and len(results) == 1
        assert results[0].data == data and results[0].fact.revision == fact.revision
        assert verify_user_token(token)["sub"] == claims["sub"]
        count = len(calls)
        with pytest.raises(HTTPException) as denied:
            guarded.get(claims, "owned.corpus", str(fact.revision))
        assert denied.value.status_code == 401 and len(calls) == count

        # A trusted-role object overwrite is detectable but not preventable by
        # hashing alone; the registry digest must still reject altered bytes.
        second_data = b"second snapshot"
        second = Revision(
            owner=other["sub"],
            filename="other.corpus",
            revision=uuid4(),
            digest="sha256:" + hashlib.sha256(second_data).hexdigest(),
            version="v1.0",
        )
        store.publish(second, second_data, None)
        corrupt_url = f"{url}/storage/v1/object/corpus-stored-revisions/{store._key(second)}"
        assert (
            auth.put(corrupt_url, headers=service_headers, content=b"tampered").status_code == 200
        )
        with pytest.raises(HTTPException) as mismatch:
            guarded.get(other, "other.corpus", str(second.revision))
        assert mismatch.value.status_code == 409
        assert verify_user_token(other_token)["sub"] == other["sub"]
