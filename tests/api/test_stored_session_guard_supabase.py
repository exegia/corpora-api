"""Real JWT and logout check against a disposable loopback Supabase stack."""

from __future__ import annotations

import os
from urllib.parse import urlparse
from uuid import uuid4

import httpx
import pytest

from corpora_api.auth import verify_user_token

pytestmark = pytest.mark.skipif(
    not os.getenv("CORPORA_TEST_LOCAL_SUPABASE"), reason="requires disposable local Supabase"
)


def test_guard_rejects_real_logged_out_supabase_session(monkeypatch):
    url = os.environ["API_URL"].rstrip("/")
    assert urlparse(url).hostname in {"localhost", "127.0.0.1", "::1"}
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
        create = client.post(
            url + "/rest/v1/rpc/corpora_stored_create",
            headers=service_headers,
            json={
                "p_owner": claims["sub"],
                "p_filename": name,
                "p_revision": initial,
                "p_digest": digest,
                "p_version": "v1.0",
            },
        )
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
        logout = client.post(
            url + "/auth/v1/logout", headers={"apikey": anon, "Authorization": "Bearer " + token}
        )
        assert logout.status_code in (200, 204)
        assert verify_user_token(token)["sub"] == claims["sub"]
        blocked = client.post(
            endpoint,
            headers=service_headers,
            json={**payload, "p_expected": published, "p_revision": second},
        )
        assert (
            blocked.status_code >= 400 and blocked.json().get("message") == "AUTH_SESSION_INVALID"
        )
        head = client.get(
            url + "/rest/v1/corpora_stored_heads",
            headers=service_headers,
            params={"owner": "eq." + claims["sub"], "filename": "eq." + name},
        )
        assert head.status_code == 200 and head.json()[0]["revision"] == published
