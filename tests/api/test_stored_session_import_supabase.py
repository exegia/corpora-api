"""Real local Auth logout racing the draft legacy import transaction."""

from __future__ import annotations

import hashlib
import os
import threading
import time
from urllib.parse import urlparse
from uuid import uuid4

import httpx
import pytest

from corpora_api.auth import verify_user_token

pytestmark = pytest.mark.skipif(
    not os.getenv("CORPORA_TEST_LOCAL_SUPABASE"), reason="requires disposable local Supabase"
)


def test_import_holds_session_through_real_logout(monkeypatch):
    psycopg = pytest.importorskip("psycopg")
    url, dsn = os.environ["API_URL"].rstrip("/"), os.environ["DB_URL"]
    assert urlparse(url).hostname in {"localhost", "127.0.0.1", "::1"}
    assert urlparse(dsn).hostname in {"localhost", "127.0.0.1", "::1"}
    anon, service = os.environ["ANON_KEY"], os.environ["SERVICE_ROLE_KEY"]
    monkeypatch.setenv("SUPABASE_JWKS_URL", url + "/auth/v1/.well-known/jwks.json")
    monkeypatch.setenv("SUPABASE_JWT_ISSUER", url + "/auth/v1")
    service_headers = {"apikey": service, "Authorization": "Bearer " + service}
    with httpx.Client(timeout=30) as client:
        signup = client.post(
            url + "/auth/v1/signup",
            headers={"apikey": anon, "Authorization": "Bearer " + anon},
            json={"email": f"{uuid4()}@example.org", "password": str(uuid4())},
        )
        assert signup.status_code == 200
        token = signup.json()["access_token"]
        claims = verify_user_token(token)
        owner = claims["sub"]
        bucket = "session-import-" + uuid4().hex
        created = client.post(
            url + "/storage/v1/bucket",
            headers=service_headers,
            json={"id": bucket, "name": bucket, "public": False},
        )
        assert created.status_code in (200, 201)
        source, later = "source.corpus", "after-logout.corpus"
        data = b"transaction test bytes"
        for name in (source, later):
            uploaded = client.post(
                f"{url}/storage/v1/object/{bucket}/{owner}/{name}",
                headers=service_headers,
                content=data,
            )
            assert uploaded.status_code in (200, 201)

        def stamp(name):
            response = client.post(
                url + "/rest/v1/rpc/corpora_legacy_source_stamp",
                headers=service_headers,
                json={"p_owner": owner, "p_bucket": bucket, "p_filename": name},
            )
            assert response.status_code == 200
            return response.json()

        first_stamp, later_stamp = stamp(source), stamp(later)
        digest = "sha256:" + hashlib.sha256(data).hexdigest()
        revision = str(uuid4())
        payload = {
            "p_owner": owner,
            "p_session": claims["session_id"],
            "p_token_exp": claims["exp"],
            "p_bucket": bucket,
            "p_filename": source,
            "p_source_id": first_stamp["id"],
            "p_source_version": first_stamp["version"],
            "p_revision": revision,
            "p_digest": digest,
            "p_version": "v1.0",
        }
        endpoint = url + "/rest/v1/rpc/corpora_stored_session_import"
        browser = client.post(
            endpoint, headers={"apikey": anon, "Authorization": "Bearer " + token}, json=payload
        )
        assert browser.status_code in (401, 403, 404)
        logout_result, errors = [], []

        def logout():
            try:
                response = client.post(
                    url + "/auth/v1/logout",
                    headers={"apikey": anon, "Authorization": "Bearer " + token},
                )
                logout_result.append(response.status_code)
            except Exception as error:
                errors.append(error)

        worker = threading.Thread(target=logout)
        with psycopg.connect(dsn) as importer:
            importer.execute("SET ROLE service_role")
            importer.execute(
                "SELECT public.corpora_stored_session_import(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    owner,
                    claims["session_id"],
                    claims["exp"],
                    bucket,
                    source,
                    first_stamp["id"],
                    first_stamp["version"],
                    revision,
                    digest,
                    "v1.0",
                ),
            )
            try:
                worker.start()
                with psycopg.connect(dsn, autocommit=True) as monitor:
                    deadline = time.monotonic() + 15
                    while time.monotonic() < deadline:
                        blocked = monitor.execute(
                            "SELECT EXISTS(SELECT 1 FROM pg_stat_activity "
                            "WHERE %s = ANY(pg_blocking_pids(pid)))",
                            (importer.info.backend_pid,),
                        ).fetchone()[0]
                        if blocked:
                            break
                        time.sleep(0.05)
                    else:
                        pytest.fail("local Auth logout did not wait for the import session lock")
            finally:
                importer.commit()
                worker.join(20)
        assert not worker.is_alive() and not errors and logout_result in ([200], [204])
        assert verify_user_token(token)["sub"] == owner
        denied = client.post(
            endpoint,
            headers=service_headers,
            json={
                **payload,
                "p_filename": later,
                "p_source_id": later_stamp["id"],
                "p_source_version": later_stamp["version"],
                "p_revision": str(uuid4()),
            },
        )
        assert denied.status_code >= 400 and denied.json().get("message") == "AUTH_SESSION_INVALID"
        heads = client.get(
            url + "/rest/v1/corpora_stored_heads",
            headers=service_headers,
            params={"owner": "eq." + owner, "filename": "eq." + later},
        )
        assert heads.status_code == 200 and heads.json() == []
