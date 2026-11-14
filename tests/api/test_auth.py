"""Supabase user identity and legacy service-token compatibility."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException, Request
from fastapi.responses import Response
from fastapi.testclient import TestClient

from corpora_api import auth
from corpora_api.app import app, bearer_auth

USER_ID = "b9a94f14-dfc2-4699-ab8c-77378136f2b4"
ISSUER = "https://example.supabase.co/auth/v1"


@pytest.fixture
def signing_key(monkeypatch: pytest.MonkeyPatch) -> rsa.RSAPrivateKey:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    monkeypatch.setenv("PROJECT_REF", "example")
    monkeypatch.delenv("SUPABASE_JWKS_URL", raising=False)
    monkeypatch.delenv("SUPABASE_JWT_ISSUER", raising=False)
    monkeypatch.setattr(
        auth,
        "_jwks_client",
        lambda _url: SimpleNamespace(
            get_signing_key_from_jwt=lambda _token: SimpleNamespace(key=key.public_key())
        ),
    )
    return key


def user_token(key: rsa.RSAPrivateKey, **overrides: object) -> str:
    claims = {
        "sub": USER_ID,
        "aud": "authenticated",
        "iss": ISSUER,
        "exp": datetime.now(UTC) + timedelta(hours=1),
    }
    claims.update(overrides)
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": "test-key"})


def test_verified_user_reaches_protected_handler(
    monkeypatch: pytest.MonkeyPatch, signing_key: rsa.RSAPrivateKey
) -> None:
    monkeypatch.setenv("CORPORA_API_TOKEN", "service-token")
    with TestClient(app) as client:
        response = client.get(
            "/v1/auth/me", headers={"Authorization": f"Bearer {user_token(signing_key)}"}
        )
        schema = client.get("/v1/openapi.json").json()

    assert response.status_code == 200
    assert response.json() == {"user_id": USER_ID}
    assert auth.current_user_id.get() is None
    assert schema["paths"]["/v1/auth/me"]["get"]["security"] == [{"SupabaseUserJWT": []}]
    assert schema["paths"]["/v1/checks/structure"]["post"]["security"] == [{"BearerAuth": []}]


def test_user_token_cannot_call_service_route(
    monkeypatch: pytest.MonkeyPatch, signing_key: rsa.RSAPrivateKey
) -> None:
    monkeypatch.setenv("CORPORA_API_TOKEN", "service-token")
    with TestClient(app) as client:
        response = client.post(
            "/v1/checks/structure",
            headers={"Authorization": f"Bearer {user_token(signing_key)}"},
            json={"text": "<root/>"},
        )
        service_as_user = client.get(
            "/v1/auth/me", headers={"Authorization": "Bearer service-token"}
        )
    assert response.status_code == 401
    assert service_as_user.status_code == 401


@pytest.mark.parametrize(
    "overrides",
    [
        {"aud": "other"},
        {"iss": "https://other.supabase.co/auth/v1"},
        {"exp": datetime(2020, 1, 1, tzinfo=UTC)},
        {"sub": ""},
    ],
)
def test_invalid_user_claims_fail_closed(
    monkeypatch: pytest.MonkeyPatch, signing_key: rsa.RSAPrivateKey, overrides: dict[str, object]
) -> None:
    monkeypatch.setenv("CORPORA_API_TOKEN", "service-token")
    with TestClient(app) as client:
        response = client.get(
            "/v1/auth/me",
            headers={"Authorization": f"Bearer {user_token(signing_key, **overrides)}"},
        )
    assert response.status_code == 401


def test_unconfigured_jwks_and_missing_user_are_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PROJECT_REF", raising=False)
    monkeypatch.delenv("SUPABASE_JWKS_URL", raising=False)
    monkeypatch.setenv("CORPORA_API_TOKEN", "service-token")
    with TestClient(app) as client:
        missing = client.get("/v1/auth/me")
        unverified = client.get(
            "/v1/auth/me",
            headers={"Authorization": "Bearer unverified"},
        )
    assert missing.status_code == 401
    assert unverified.status_code == 401


def test_custom_jwks_requires_an_expected_issuer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PROJECT_REF", raising=False)
    monkeypatch.delenv("SUPABASE_JWT_ISSUER", raising=False)
    monkeypatch.setenv("SUPABASE_JWKS_URL", "https://auth.example.test/jwks.json")
    with pytest.raises(auth.UserAuthError, match="issuer is not configured"):
        auth.verify_user_token("unverified")


def test_wrong_signature_and_missing_expiry_are_rejected(
    monkeypatch: pytest.MonkeyPatch, signing_key: rsa.RSAPrivateKey
) -> None:
    monkeypatch.setenv("CORPORA_API_TOKEN", "service-token")
    other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    no_expiry = jwt.encode(
        {"sub": USER_ID, "aud": "authenticated", "iss": ISSUER},
        signing_key,
        algorithm="RS256",
        headers={"kid": "test-key"},
    )
    no_issuer = jwt.encode(
        {"sub": USER_ID, "aud": "authenticated", "exp": datetime.now(UTC) + timedelta(hours=1)},
        signing_key,
        algorithm="RS256",
        headers={"kid": "test-key"},
    )
    with TestClient(app) as client:
        for credential in (user_token(other_key), no_expiry, no_issuer):
            response = client.get(
                "/v1/auth/me",
                headers={"Authorization": f"Bearer {credential}"},
            )
            assert response.status_code == 401


def test_non_ascii_service_credential_is_unauthorized(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CORPORA_API_TOKEN", "service-token")
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/v1/checks/structure",
            "headers": [(b"authorization", b"Bearer caf\xe9")],
        }
    )

    async def should_not_run(_request: Request) -> Response:
        raise AssertionError("Invalid bearer credential reached the route")

    response = asyncio.run(bearer_auth(request, should_not_run))
    assert response.status_code == 401


def test_user_dependency_rejects_service_identity() -> None:
    request = Request({"type": "http", "state": {}})
    with pytest.raises(HTTPException, match="Verified user token required"):
        auth.require_user(request)
    request.state.user_id = USER_ID
    assert auth.require_user(request) == USER_ID


def test_health_and_capabilities_are_public(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CORPORA_API_TOKEN", "service-token")
    with TestClient(app) as client:
        assert client.get("/v1/health").json() == {"status": "ok"}
        assert client.get("/v1/capabilities").json() == {
            "auth_required": True,
            "hub_writable": False,
        }
