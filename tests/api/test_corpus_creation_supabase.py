"""Real local Auth/PostgREST creation checks; only the Hub boundary is simulated."""

import importlib
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from corpora_api import corpus_creation as creation
from corpora_api.auth import verify_user_token
from corpora_api.corpus_hub_provision import VerifiedPrivateRepository
from corpora_api.corpus_metadata import CorpusMetadataError
from corpora_api.corpus_provision_dispatch import CorpusProvisionDispatcher
from corpora_api.corpus_provision_observation import CorpusProvisionHubObserver

pytestmark = pytest.mark.skipif(
    not os.getenv("CORPORA_TEST_LOCAL_SUPABASE"), reason="requires disposable local Supabase"
)


def test_creation_sessions_retries_and_privileges(monkeypatch):
    psycopg = pytest.importorskip("psycopg")
    url, dsn = os.environ["API_URL"].rstrip("/"), os.environ["DB_URL"]
    assert urlparse(url).hostname in {"localhost", "127.0.0.1", "::1"}
    assert urlparse(dsn).hostname in {"localhost", "127.0.0.1", "::1"}
    anon, service = os.environ["ANON_KEY"], os.environ["SERVICE_ROLE_KEY"]
    for key, value in {
        "SUPABASE_URL": url,
        "SUPABASE_SERVICE_ROLE_KEY": service,
        "SUPABASE_JWKS_URL": url + "/auth/v1/.well-known/jwks.json",
        "SUPABASE_JWT_ISSUER": url + "/auth/v1",
        "HF_CORPUS_ORGANIZATION": "fixture",
        "HF_CORPUS_PROVISION_TOKEN": "test-placeholder",
        "CORPORA_CORPUS_CREATION_ENABLED": "1",
    }.items():
        monkeypatch.setenv(key, value)
    migrations = Path(__file__).resolve().parents[2] / "supabase/migrations"
    users = []
    entered, release = threading.Event(), threading.Event()
    calls = []
    sha = "a" * 40

    def worker(actor, corpus, **_):
        calls.append(corpus)
        entered.set()
        assert release.wait(10)
        return VerifiedPrivateRepository(
            UUID(corpus), 1, "fixture/corpus-" + corpus, "dataset", sha
        )

    def hub(request):
        repo = request.url.path.removeprefix("/api/datasets/")
        return httpx.Response(
            200,
            stream=httpx.ByteStream(json.dumps({"id": repo, "private": True, "sha": sha}).encode()),
        )

    monkeypatch.setattr(
        creation,
        "CorpusProvisionDispatcher",
        lambda *args: CorpusProvisionDispatcher(*args, worker=worker),
    )

    class TestObserver(CorpusProvisionHubObserver):
        def __init__(self, token):
            super().__init__(token, transport_factory=lambda: httpx.MockTransport(hub))

    monkeypatch.setattr(creation, "CorpusProvisionHubObserver", TestObserver)
    app = importlib.import_module("corpora_api.app").app
    with psycopg.connect(dsn, autocommit=True) as db, httpx.Client(timeout=15) as auth:
        # This fixture owns only a new, empty local registry.
        assert db.execute(
            "SELECT to_regclass('public.corpora_corpus_metadata') IS NULL"
        ).fetchone()[0]
        assert db.execute("SELECT to_regnamespace('corpora_session_guard') IS NULL").fetchone()[0]
        existing_functions = {
            row[0]
            for row in db.execute(
                "SELECT oid FROM pg_proc WHERE pronamespace='public'::regnamespace"
            ).fetchall()
        }
        try:
            for suffix in (
                "corpus_metadata_registry",
                "corpus_content_versions",
                "restricted_session_guard",
                "restricted_provision_admission",
                "restricted_provision_outcomes",
                "corpus_creation_rpc",
            ):
                db.execute(next(migrations.glob("*_" + suffix + ".sql")).read_text())
            for _ in range(3):
                response = auth.post(
                    url + "/auth/v1/signup",
                    headers={"apikey": anon},
                    json={"email": f"{uuid4()}@example.org", "password": str(uuid4())},
                )
                assert response.status_code == 200
                token = response.json()["access_token"]
                users.append((verify_user_token(token), token))
            owner, foreign, admin = users
            db.execute(
                "INSERT INTO corpora_session_guard.superadmins(user_id) VALUES (%s)",
                (admin[0]["sub"],),
            )
            # Wait for PostgREST's schema reload without attempting admission.
            deadline = time.monotonic() + 10
            while True:
                response = auth.get(
                    url + "/rest/v1/",
                    headers={"apikey": service, "Authorization": "Bearer " + service},
                )
                if (
                    response.status_code == 200
                    and "/rpc/corpora_begin_creation" in response.json().get("paths", {})
                ):
                    break
                assert time.monotonic() < deadline
                time.sleep(0.1)
            with TestClient(app) as api:

                def headers(user):
                    return {"Authorization": "Bearer " + user[1]}

                corpus = str(uuid4())
                body = {"id": corpus, "title": "Local creation fixture"}
                path = "/v1/corpora/" + corpus
                assert api.post("/v1/corpora", json=body).status_code == 401
                with ThreadPoolExecutor(max_workers=1) as pool:
                    first = pool.submit(api.post, "/v1/corpora", headers=headers(owner), json=body)
                    if not entered.wait(10):
                        early = first.result(timeout=2)
                        pytest.fail(f"Creation did not dispatch: {early.status_code} {early.text}")
                    pending = api.post("/v1/corpora", headers=headers(owner), json=body)
                    assert pending.status_code == 202 and pending.json()["state"] == "provisioning"
                    assert api.get(path, headers=headers(foreign)).status_code == 404
                    assert (
                        api.post("/v1/corpora", headers=headers(foreign), json=body).status_code
                        == 409
                    )
                    assert (
                        api.post(
                            "/v1/corpora", headers=headers(owner), json={**body, "title": "Changed"}
                        ).status_code
                        == 409
                    )
                    release.set()
                    created = first.result(timeout=15)
                assert created.status_code == 201, created.text
                assert created.json()["bootstrap_sha"] == sha
                assert (
                    api.post("/v1/corpora", headers=headers(owner), json=body).json()
                    == created.json()
                )
                assert api.get(path, headers=headers(admin)).json() == created.json()
                assert calls == [corpus]

                # Browser JWTs cannot use service RPCs, including forged actor claims.
                params = {
                    "p_actor": owner[0]["sub"],
                    "p_session": owner[0]["session_id"],
                    "p_token_exp": owner[0]["exp"],
                    "p_corpus": corpus,
                }
                for token in (anon, foreign[1], owner[1]):
                    denied = auth.post(
                        url + "/rest/v1/rpc/corpora_get_creation",
                        headers={"apikey": anon, "Authorization": "Bearer " + token},
                        json=params,
                    )
                    assert denied.status_code in (401, 403, 404)
                for role in ("anon", "authenticated", "service_role"):
                    for table in (
                        "auth.sessions",
                        "corpora_session_guard.superadmins",
                        "public.corpora_corpus_provision_ops",
                        "public.corpora_corpus_bootstrap",
                    ):
                        assert not db.execute(
                            "SELECT has_table_privilege(%s,%s,'UPDATE')", (role, table)
                        ).fetchone()[0]
                for (signature,) in db.execute(
                    "SELECT oid::regprocedure::text FROM pg_proc "
                    "WHERE pronamespace='public'::regnamespace AND proname IN "
                    "('corpora_begin_creation','corpora_get_creation','corpora_observe_provision',"
                    "'corpora_finalize_creation','corpora_find_creation_result',"
                    "'corpora_mark_creation_uncertain')"
                ).fetchall():
                    for role in ("anon", "authenticated", "service_role"):
                        assert db.execute(
                            "SELECT has_function_privilege(%s,%s,'EXECUTE')", (role, signature)
                        ).fetchone()[0] == (role == "service_role")

                # A committed admission whose acknowledgement is lost must not dispatch.
                original_call = creation.CreationRPC.call
                lost_id = str(uuid4())

                def lost_ack(self, name, params):
                    result = original_call(self, name, params)
                    if name == "corpora_begin_creation":
                        raise CorpusMetadataError("Simulated lost acknowledgement")
                    return result

                with monkeypatch.context() as patch:
                    patch.setattr(creation.CreationRPC, "call", lost_ack)
                    assert (
                        api.post(
                            "/v1/corpora", headers=headers(owner), json={**body, "id": lost_id}
                        ).status_code
                        == 503
                    )
                retry = api.post(
                    "/v1/corpora", headers=headers(owner), json={**body, "id": lost_id}
                )
                assert retry.status_code == 202 and retry.json()["state"] == "provisioning"
                assert calls == [corpus]

                # Accepted work may start/finish after logout; new attempts may not.
                entered.clear()
                release.clear()
                after_logout = str(uuid4())
                with ThreadPoolExecutor(max_workers=1) as pool:
                    accepted = pool.submit(
                        api.post,
                        "/v1/corpora",
                        headers=headers(owner),
                        json={**body, "id": after_logout},
                    )
                    assert entered.wait(10)
                    logout = auth.post(
                        url + "/auth/v1/logout", headers={"apikey": anon, **headers(owner)}
                    )
                    assert logout.status_code == 204
                    assert api.get(path, headers=headers(owner)).status_code == 401
                    assert (
                        api.post(
                            "/v1/corpora", headers=headers(owner), json={**body, "id": str(uuid4())}
                        ).status_code
                        == 401
                    )
                    release.set()
                    assert accepted.result(timeout=15).status_code == 201
                assert (
                    api.get("/v1/corpora/" + after_logout, headers=headers(admin)).json()["state"]
                    == "repository_created"
                )
                assert len(calls) == 2
        finally:
            release.set()
            # Empty local registry was asserted above; remove only its fixture schema.
            db.execute("DROP SCHEMA IF EXISTS corpora_session_guard CASCADE")
            for table in (
                "heads",
                "versions",
                "publication_claims",
                "staged_inputs",
                "write_ops",
                "bootstrap",
                "provision_ops",
                "metadata",
            ):
                db.execute("DROP TABLE IF EXISTS public.corpora_corpus_" + table + " CASCADE")
            functions = db.execute(
                "SELECT oid, oid::regprocedure::text FROM pg_proc "
                "WHERE pronamespace='public'::regnamespace AND proname LIKE 'corpora_%'"
            ).fetchall()
            for oid, signature in functions:
                # Function identifiers are catalog-derived, not request input.
                if oid not in existing_functions:
                    db.execute("DROP FUNCTION " + signature + " CASCADE")
            for claims, _ in users:
                deleted = auth.delete(
                    url + "/auth/v1/admin/users/" + claims["sub"],
                    headers={"apikey": service, "Authorization": "Bearer " + service},
                )
                assert deleted.status_code == 200
                assert (
                    db.execute(
                        "SELECT count(*) FROM auth.users WHERE id=%s", (claims["sub"],)
                    ).fetchone()[0]
                    == 0
                )
