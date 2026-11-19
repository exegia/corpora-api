"""Real Supabase logout checks for the draft route admission gate."""

import importlib
import json
import os
import subprocess
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.skipif(
    os.getenv("CORPORA_TEST_DRAFT_SESSION") != "1", reason="requires disposable local Supabase"
)


def test_real_logout_blocks_every_new_draft_request(monkeypatch):
    workdir = Path(os.environ["CORPORA_TEST_DRAFT_SESSION_WORKDIR"]).resolve(strict=True)
    env = json.loads(
        subprocess.check_output(
            ["supabase", "status", "--workdir", str(workdir), "--output-format", "json"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    )["env"]
    url = env["API_URL"].rstrip("/")
    assert urlparse(url).hostname in {"localhost", "127.0.0.1", "::1"}
    assert urlparse(env["DB_URL"]).hostname in {"localhost", "127.0.0.1", "::1"}
    monkeypatch.setenv("SUPABASE_URL", url)
    monkeypatch.setenv("SUPABASE_INGEST_API_KEY", env["PUBLISHABLE_KEY"])
    monkeypatch.setenv("SUPABASE_JWKS_URL", url + "/auth/v1/.well-known/jwks.json")
    monkeypatch.setenv("SUPABASE_JWT_ISSUER", url + "/auth/v1")
    app_module = importlib.import_module("corpora_api.app")
    calls = []

    def list_drafts(actor, limit, offset):
        calls.append(actor)
        return {"drafts": [], "next_offset": None}

    monkeypatch.setattr(app_module.draft_store, "list", list_drafts)
    service = env["SERVICE_ROLE_KEY"]
    admin = {"apikey": service, "Authorization": "Bearer " + service}
    anon = env["ANON_KEY"]
    user_id = None
    with httpx.Client(timeout=20, trust_env=False) as client:
        try:
            email, password = f"{uuid4().hex}@example.test", uuid4().hex + "aA9!"
            created = client.post(
                url + "/auth/v1/admin/users",
                headers=admin,
                json={"email": email, "password": password, "email_confirm": True},
            )
            assert created.status_code in (200, 201)
            user_id = created.json()["id"]
            signed_in = client.post(
                url + "/auth/v1/token?grant_type=password",
                headers={"apikey": anon},
                json={"email": email, "password": password},
            )
            assert signed_in.status_code == 200
            token = signed_in.json()["access_token"]
            authorization = {"Authorization": "Bearer " + token}
            api = TestClient(app_module.app)
            assert api.get("/v1/ai/drafts", headers=authorization).status_code == 200
            assert calls == [user_id]
            signed_out = client.post(
                url + "/auth/v1/logout?scope=local",
                headers={"apikey": anon, **authorization},
            )
            assert signed_out.status_code == 204
            for method, path in (
                ("POST", "/v1/ai/drafts"),
                ("GET", "/v1/ai/drafts"),
                ("GET", f"/v1/ai/drafts/{uuid4()}"),
                ("GET", f"/v1/ai/drafts/{uuid4()}/archive"),
            ):
                assert api.request(method, path, headers=authorization).status_code == 401
            assert calls == [user_id]
        finally:
            if user_id:
                removed = client.delete(url + "/auth/v1/admin/users/" + user_id, headers=admin)
                assert removed.status_code in (200, 204, 404)
                checked = client.get(url + "/auth/v1/admin/users/" + user_id, headers=admin)
                assert checked.status_code == 404
