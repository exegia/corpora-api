"""Verified Supabase identity for user-owned API operations."""

from __future__ import annotations

import os
from contextvars import ContextVar
from functools import lru_cache
from typing import Any
from urllib.error import URLError

import jwt
from fastapi import HTTPException, Request
from jwt import PyJWKClient

current_user_id: ContextVar[str | None] = ContextVar("current_user_id", default=None)


class UserAuthError(Exception):
    """A bearer token did not establish a verified Supabase user."""


@lru_cache(maxsize=8)
def _jwks_client(url: str) -> PyJWKClient:
    return PyJWKClient(url, lifespan=300)


def verify_user_token(token: str) -> dict[str, Any]:
    """Verify signature and required access-token claims before trusting ``sub``."""
    project_ref = os.getenv("PROJECT_REF")
    jwks_url = os.getenv("SUPABASE_JWKS_URL") or (
        f"https://{project_ref}.supabase.co/auth/v1/.well-known/jwks.json" if project_ref else None
    )
    if not jwks_url:
        raise UserAuthError("Supabase JWKS is not configured")

    issuer = os.getenv("SUPABASE_JWT_ISSUER") or (
        f"https://{project_ref}.supabase.co/auth/v1" if project_ref else None
    )
    if not issuer:
        raise UserAuthError("Supabase JWT issuer is not configured")
    try:
        key = _jwks_client(jwks_url).get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            key.key,
            algorithms=["RS256", "ES256"],
            audience=os.getenv("SUPABASE_JWT_AUDIENCE", "authenticated"),
            issuer=issuer,
            options={"require": ["aud", "exp", "iss", "sub"]},
        )
    except (jwt.PyJWTError, OSError, URLError, TimeoutError) as error:
        raise UserAuthError("Invalid or expired user token") from error

    if not isinstance(claims.get("sub"), str) or not claims["sub"]:
        raise UserAuthError("User token has no subject")
    return claims


def require_user(request: Request) -> str:
    """Route dependency for future owner-scoped endpoints; service tokens are insufficient."""
    user_id = getattr(request.state, "user_id", None)
    if not isinstance(user_id, str) or not user_id:
        raise HTTPException(status_code=401, detail="Verified user token required")
    return user_id


def require_user_claims(request: Request) -> dict[str, Any]:
    """Return only the verified claims attached by authentication middleware."""
    user_id = require_user(request)
    claims = getattr(request.state, "user_claims", None)
    if not isinstance(claims, dict) or claims.get("sub") != user_id:
        raise HTTPException(status_code=401, detail="Verified user token required")
    return claims
