"""Real JWT and logout check against a disposable loopback Supabase stack."""

from __future__ import annotations

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


def test_guard_rejects_real_logged_out_supabase_session(monkeypatch):
    psycopg = pytest.importorskip("psycopg")
    url = os.environ["API_URL"].rstrip("/")
    dsn = os.environ["DB_URL"]
    assert urlparse(url).hostname in {"localhost", "127.0.0.1", "::1"}
    assert urlparse(dsn).hostname in {"localhost", "127.0.0.1", "::1"}
    anon, service = os.environ["ANON_KEY"], os.environ["SERVICE_ROLE_KEY"]
    monkeypatch.setenv("SUPABASE_JWKS_URL", url + "/auth/v1/.well-known/jwks.json")
    monkeypatch.setenv("SUPABASE_JWT_ISSUER", url + "/auth/v1")
    service_headers = {"apikey": service, "Authorization": "Bearer " + service}
    with httpx.Client(timeout=30) as client:

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
        _, foreign = signup()
        initial, published, second = str(uuid4()), str(uuid4()), str(uuid4())
        digest, name = "sha256:" + "a" * 64, "session.corpus"
        create_endpoint = url + "/rest/v1/rpc/corpora_stored_session_create"
        create_payload = {
            "p_owner": claims["sub"],
            "p_session": claims["session_id"],
            "p_token_exp": claims["exp"],
            "p_filename": name,
            "p_revision": initial,
            "p_digest": digest,
            "p_version": "v1.0",
        }
        browser_create = client.post(
            create_endpoint,
            headers={"apikey": anon, "Authorization": "Bearer " + token},
            json=create_payload,
        )
        assert browser_create.status_code in (401, 403, 404)
        wrong_create = client.post(
            create_endpoint,
            headers=service_headers,
            json={**create_payload, "p_session": foreign["session_id"]},
        )
        assert wrong_create.status_code >= 400
        assert wrong_create.json().get("message") == "AUTH_SESSION_INVALID"
        create = client.post(create_endpoint, headers=service_headers, json=create_payload)
        assert create.status_code in (200, 204)
        payload = {
            "p_owner": claims["sub"],
            "p_session": claims["session_id"],
            "p_token_exp": claims["exp"],
            "p_filename": name,
            "p_expected": initial,
            "p_revision": published,
            "p_digest": digest,
            "p_version": "v1.1",
        }
        endpoint = url + "/rest/v1/rpc/corpora_stored_session_publish"
        browser = client.post(
            endpoint, headers={"apikey": anon, "Authorization": "Bearer " + token}, json=payload
        )
        assert browser.status_code in (401, 403, 404)
        wrong_session = client.post(
            endpoint, headers=service_headers, json={**payload, "p_session": foreign["session_id"]}
        )
        assert wrong_session.status_code >= 400
        assert wrong_session.json().get("message") == "AUTH_SESSION_INVALID"
        assert client.post(endpoint, headers=service_headers, json=payload).status_code in (
            200,
            204,
        )
        common = {
            "p_owner": claims["sub"],
            "p_session": claims["session_id"],
            "p_token_exp": claims["exp"],
            "p_filename": name,
        }
        reads = [
            ("get", {**common, "p_revision": published}),
            ("history", {**common, "p_limit": 10, "p_before": None, "p_before_revision": None}),
            ("facts", {**common, "p_revisions": [initial, published]}),
        ]
        for action, read_payload in reads:
            read_endpoint = url + "/rest/v1/rpc/corpora_stored_session_" + action
            valid = client.post(read_endpoint, headers=service_headers, json=read_payload)
            assert valid.status_code == 200
            if action == "get":
                assert valid.json()["revision"] == published
            else:
                assert len(valid.json()) == 2
            wrong = client.post(
                read_endpoint,
                headers=service_headers,
                json={**read_payload, "p_session": foreign["session_id"]},
            )
            assert (
                wrong.status_code >= 400 and wrong.json().get("message") == "AUTH_SESSION_INVALID"
            )
            browser_read = client.post(
                read_endpoint,
                headers={"apikey": anon, "Authorization": "Bearer " + token},
                json=read_payload,
            )
            assert browser_read.status_code in (401, 403, 404)
        logout_status, errors = [], []

        def logout():
            try:
                result = client.post(
                    url + "/auth/v1/logout",
                    headers={"apikey": anon, "Authorization": "Bearer " + token},
                )
                logout_status.append(result.status_code)
            except Exception as error:
                errors.append(error)

        worker = threading.Thread(target=logout)
        with psycopg.connect(dsn) as reader:
            reader.execute("SET ROLE service_role")
            value = reader.execute(
                "SELECT public.corpora_stored_session_get(%s,%s,%s,%s,%s)",
                (claims["sub"], claims["session_id"], claims["exp"], name, published),
            ).fetchone()[0]
            assert value["revision"] == published
            try:
                worker.start()
                with psycopg.connect(dsn, autocommit=True) as monitor:
                    deadline = time.monotonic() + 15
                    while time.monotonic() < deadline:
                        blocked = monitor.execute(
                            "SELECT EXISTS(SELECT 1 FROM pg_stat_activity "
                            "WHERE %s = ANY(pg_blocking_pids(pid)))",
                            (reader.info.backend_pid,),
                        ).fetchone()[0]
                        if blocked:
                            break
                        time.sleep(0.05)
                    else:
                        pytest.fail("local logout did not wait for the guarded metadata read")
            finally:
                reader.commit()
                worker.join(20)
        assert not worker.is_alive() and not errors and logout_status in ([200], [204])
        assert verify_user_token(token)["sub"] == claims["sub"]
        for action, read_payload in reads:
            denied = client.post(
                url + "/rest/v1/rpc/corpora_stored_session_" + action,
                headers=service_headers,
                json=read_payload,
            )
            assert (
                denied.status_code >= 400 and denied.json().get("message") == "AUTH_SESSION_INVALID"
            )
        blocked = client.post(
            endpoint,
            headers=service_headers,
            json={**payload, "p_expected": published, "p_revision": second},
        )
        assert (
            blocked.status_code >= 400 and blocked.json().get("message") == "AUTH_SESSION_INVALID"
        )
        blocked_create = client.post(
            create_endpoint,
            headers=service_headers,
            json={**create_payload, "p_filename": "after-logout.corpus", "p_revision": second},
        )
        assert blocked_create.status_code >= 400
        assert blocked_create.json().get("message") == "AUTH_SESSION_INVALID"
        head = client.get(
            url + "/rest/v1/corpora_stored_heads",
            headers=service_headers,
            params={"owner": "eq." + claims["sub"], "filename": "eq." + name},
        )
        assert head.status_code == 200 and head.json()[0]["revision"] == published
