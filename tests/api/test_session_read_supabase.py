"""Exercise session locks across real Storage reads on a disposable local stack."""

from __future__ import annotations

import os
import threading
import time
from urllib.parse import urlparse
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException

from corpora_api.auth import verify_user_token
from corpora_api.session_read import SessionBoundRead

pytestmark = pytest.mark.skipif(
    not os.getenv("CORPORA_TEST_LOCAL_SUPABASE"), reason="requires disposable local Supabase"
)


def test_session_lock_covers_storage_download_and_logout(monkeypatch):
    psycopg = pytest.importorskip("psycopg")
    url, dsn = os.environ["API_URL"].rstrip("/"), os.environ["DB_URL"]
    assert urlparse(url).hostname in {"localhost", "127.0.0.1", "::1"}
    assert urlparse(dsn).hostname in {"localhost", "127.0.0.1", "::1"}
    anon, service = os.environ["ANON_KEY"], os.environ["SERVICE_ROLE_KEY"]
    monkeypatch.setenv("SUPABASE_JWKS_URL", url + "/auth/v1/.well-known/jwks.json")
    monkeypatch.setenv("SUPABASE_JWT_ISSUER", url + "/auth/v1")
    service_headers = {"apikey": service, "Authorization": "Bearer " + service}
    with httpx.Client(timeout=15) as client:

        def signup():
            response = client.post(
                url + "/auth/v1/signup",
                headers={"apikey": anon, "Authorization": "Bearer " + anon},
                json={"email": f"{uuid4()}@example.org", "password": str(uuid4())},
            )
            assert response.status_code == 200
            token = response.json()["access_token"]
            return token, verify_user_token(token)

        token, claims = signup()
        bucket = "session-read-" + uuid4().hex
        created = client.post(
            url + "/storage/v1/bucket",
            headers=service_headers,
            json={"id": bucket, "name": bucket, "public": False},
        )
        assert created.status_code in (200, 201)
        path = f"{url}/storage/v1/object/{bucket}/{claims['sub']}/sample.corpus"
        payload = b"owned storage bytes"
        assert client.post(path, headers=service_headers, content=payload).status_code in (200, 201)
        backend_pid, ready, release = [], threading.Event(), threading.Event()
        result, errors, logout_result = [], [], []

        def connect():
            db = psycopg.connect(dsn, autocommit=True)
            db.execute("SET ROLE service_role")
            backend_pid.append(db.info.backend_pid)
            return db

        lease = SessionBoundRead(connect)
        other_owner = {**claims, "sub": str(uuid4())}
        foreign_calls = []
        with pytest.raises(HTTPException) as foreign:
            lease.read(other_owner, lambda _owner: foreign_calls.append(True))
        assert foreign.value.status_code == 401 and not foreign_calls
        backend_pid.clear()

        def download(owner):
            assert owner == claims["sub"]
            response = client.get(path, headers=service_headers)
            assert response.status_code == 200
            ready.set()
            if not release.wait(15):
                raise TimeoutError("download not released")
            return response.content

        def read():
            try:
                result.append(lease.read(claims, download))
            except Exception as error:
                errors.append(error)
                ready.set()

        def logout():
            try:
                response = client.post(
                    url + "/auth/v1/logout",
                    headers={"apikey": anon, "Authorization": "Bearer " + token},
                )
                logout_result.append(response.status_code)
            except Exception as error:
                errors.append(error)

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
                    pytest.fail("logout did not wait for the Storage read session lock")
        finally:
            release.set()
            reader.join(20)
            if revoker.ident:
                revoker.join(20)
        assert not reader.is_alive() and not revoker.is_alive() and not errors
        assert result == [payload] and logout_result in ([200], [204])
        assert verify_user_token(token)["sub"] == claims["sub"]
        calls = []
        with pytest.raises(HTTPException) as denied:
            lease.read(claims, lambda _owner: calls.append(True))
        assert denied.value.status_code == 401 and not calls

        # Use a shortened expiration to exercise expiry during a bounded read.
        _, other_claims = signup()
        deadline = int(time.time()) + 3
        short_claims = {**other_claims, "exp": deadline}

        def slow_read(_owner):
            while time.time() <= deadline:
                time.sleep(0.05)
            return b"must not be returned"

        with pytest.raises(HTTPException) as expired:
            lease.read(short_claims, slow_read)
        assert expired.value.status_code == 401
