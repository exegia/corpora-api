"""Real local Auth/PostgREST creation checks; only the Hub boundary is simulated."""

import hashlib
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
from test_corpus_hub_publish import real_archive

from corpora_api import corpus_creation as creation
from corpora_api.auth import verify_user_token
from corpora_api.corpus_hub_provision import VerifiedPrivateRepository
from corpora_api.corpus_metadata import CorpusMetadataError
from corpora_api.corpus_provision_dispatch import CorpusProvisionDispatcher
from corpora_api.corpus_provision_observation import CorpusProvisionHubObserver

pytestmark = pytest.mark.skipif(
    not os.getenv("CORPORA_TEST_LOCAL_SUPABASE"), reason="requires disposable local Supabase"
)


def test_creation_sessions_retries_and_privileges(monkeypatch, tmp_path):
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
        "HF_CORPUS_READ_TOKEN": "test-placeholder",
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
                "corpus_metadata_api",
                "corpus_initial_import",
                "corpus_access_lifecycle",
                "corpus_version_history",
                "corpus_version_save",
                "corpus_edit_intents",
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
                    and "/rpc/corpora_begin_version_save" in response.json().get("paths", {})
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

                metadata_path = path + "/metadata"
                assert not db.execute(
                    "SELECT has_column_privilege('service_role',"
                    "'public.corpora_corpus_metadata','title','UPDATE')"
                ).fetchone()[0]
                assert not db.execute(
                    "SELECT has_column_privilege('service_role',"
                    "'public.corpora_corpus_metadata','id','INSERT')"
                ).fetchone()[0]
                metadata = api.get(metadata_path, headers=headers(owner))
                assert metadata.status_code == 200
                assert metadata.json()["revision"] == 1
                assert "hf_repo_id" not in metadata.json()
                assert api.get(metadata_path, headers=headers(foreign)).status_code == 404
                assert api.get("/v1/corpora", headers=headers(foreign)).json()["items"] == []
                assert (
                    api.get(
                        "/v1/corpora",
                        headers=headers(foreign),
                        params={"owner_id": owner[0]["sub"]},
                    ).status_code
                    == 404
                )
                assert (
                    api.get(
                        "/v1/corpora", headers=headers(admin), params={"owner_id": owner[0]["sub"]}
                    ).json()["items"][0]["id"]
                    == corpus
                )
                renamed = api.patch(
                    metadata_path,
                    headers=headers(admin),
                    json={"expected_revision": 1, "title": "Renamed corpus"},
                )
                assert renamed.status_code == 200 and renamed.json()["revision"] == 2
                # Editing the title cannot alter creation-request idempotency.
                assert (
                    api.post("/v1/corpora", headers=headers(owner), json=body).json()
                    == created.json()
                )
                assert (
                    api.post(
                        "/v1/corpora",
                        headers=headers(owner),
                        json={**body, "title": "Renamed corpus"},
                    ).status_code
                    == 409
                )
                assert (
                    api.patch(
                        metadata_path,
                        headers=headers(foreign),
                        json={"expected_revision": 2, "title": "Foreign"},
                    ).status_code
                    == 404
                )
                barrier = threading.Barrier(2)

                def concurrent_edit(language):
                    barrier.wait(timeout=5)
                    return api.patch(
                        metadata_path,
                        headers=headers(owner),
                        json={"expected_revision": 2, "language": language},
                    )

                with ThreadPoolExecutor(max_workers=2) as pool:
                    edits = list(pool.map(concurrent_edit, ("en", "fr")))
                assert sorted(x.status_code for x in edits) == [200, 409]
                props = {"source": {"edition": "2026", "complete": True}, "tags": ["test"]}
                saved = api.patch(
                    metadata_path,
                    headers=headers(owner),
                    json={"expected_revision": 3, "properties": props, "description": "Example"},
                )
                assert saved.status_code == 200 and saved.json()["revision"] == 4
                assert api.get(metadata_path, headers=headers(owner)).json()["properties"] == props
                # Valid escaped text can exceed the default 64 KiB RPC bound.
                large_fields = {
                    "description": "\u0001" * 10000,
                    "properties": {"text": "x" * 16000},
                }
                large = api.patch(
                    metadata_path,
                    headers=headers(owner),
                    json={"expected_revision": 4, **large_fields},
                )
                assert large.status_code == 200 and large.json()["revision"] == 5
                large_read = api.get(metadata_path, headers=headers(owner))
                assert large_read.status_code == 200
                assert large_read.json()["description"] == large_fields["description"]
                assert (
                    api.patch(
                        metadata_path,
                        headers=headers(owner),
                        json={"expected_revision": 4, "owner_id": foreign[0]["sub"]},
                    ).status_code
                    == 422
                )

                # Browser JWTs cannot use service RPCs, including forged actor claims.
                params = {
                    "p_actor": owner[0]["sub"],
                    "p_session": owner[0]["session_id"],
                    "p_token_exp": owner[0]["exp"],
                    "p_corpus": corpus,
                }
                archive = real_archive(tmp_path)
                import_operation = _exercise_import_rpc(params, foreign, db, archive)
                history_path = path + "/versions"
                assert api.get(history_path).status_code == 401
                assert api.get(history_path, headers=headers(foreign)).status_code == 404
                for editor in (owner, admin):
                    version_page = api.get(
                        history_path, headers=headers(editor), params={"limit": 1}
                    )
                    assert version_page.status_code == 200, version_page.text
                    assert version_page.json()["current_sequence"] == 1
                    assert version_page.json()["versions"][0]["sequence"] == 1
                    assert version_page.json()["versions"][0]["operation_id"] == import_operation
                    assert version_page.json()["next_before"] is None
                    empty_page = api.get(
                        history_path, headers=headers(editor), params={"before": 1}
                    )
                    assert empty_page.status_code == 200 and empty_page.json()["versions"] == []
                assert (
                    api.get(history_path, headers=headers(owner), params={"limit": 101}).status_code
                    == 422
                )
                _exercise_history_paging(api, db, owner, foreign, admin)
                for token in (anon, foreign[1], owner[1]):
                    denied = auth.post(
                        url + "/rest/v1/rpc/corpora_get_creation",
                        headers={"apikey": anon, "Authorization": "Bearer " + token},
                        json=params,
                    )
                    assert denied.status_code in (401, 403, 404)
                    denied_history = auth.post(
                        url + "/rest/v1/rpc/corpora_list_corpus_versions",
                        headers={"apikey": anon, "Authorization": "Bearer " + token},
                        json={**params, "p_before": None, "p_limit": 10},
                    )
                    assert denied_history.status_code in (401, 403, 404)
                    denied_save = auth.post(
                        url + "/rest/v1/rpc/corpora_version_save_base",
                        headers={"apikey": anon, "Authorization": "Bearer " + token},
                        json={"p_operation": str(uuid4()), "p_claim": str(uuid4())},
                    )
                    assert denied_save.status_code in (401, 403, 404)
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
                    "'corpora_mark_creation_uncertain','corpora_get_metadata',"
                    "'corpora_list_metadata','corpora_patch_metadata','corpora_get_import',"
                    "'corpora_begin_import','corpora_stage_import','corpora_complete_import',"
                    "'corpora_find_import_result','corpora_mark_import_uncertain')"
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
                first_page = api.get(
                    "/v1/corpora", headers=headers(owner), params={"limit": 1}
                ).json()
                second_page = api.get(
                    "/v1/corpora",
                    headers=headers(owner),
                    params={"limit": 1, "after": first_page["next_cursor"]},
                ).json()
                assert [first_page["items"][0]["id"], second_page["items"][0]["id"]] == sorted(
                    [corpus, lost_id]
                )
                assert second_page["next_cursor"] is None

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
                    assert api.get(history_path, headers=headers(owner)).status_code == 401
                    assert api.get(metadata_path, headers=headers(owner)).status_code == 401
                    assert api.get("/v1/corpora", headers=headers(owner)).status_code == 401
                    from fastapi import HTTPException

                    with pytest.raises(HTTPException) as revoked:
                        creation.CreationRPC().call(
                            "corpora_get_import", {**params, "p_operation": import_operation}
                        )
                    assert revoked.value.status_code == 401
                    assert (
                        api.patch(
                            metadata_path,
                            headers=headers(owner),
                            json={"expected_revision": 4, "description": None},
                        ).status_code
                        == 401
                    )
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
                archive = _exercise_real_edit_route(
                    api, corpus, admin, foreign, monkeypatch, archive, db
                )
                _exercise_access_lifecycle(
                    api,
                    corpus,
                    after_logout,
                    owner,
                    foreign,
                    admin,
                    db,
                    monkeypatch,
                    archive,
                    head_sha="f" * 40,
                    version_count=3,
                )
                assert api.get(history_path, headers=headers(admin)).status_code == 404
        finally:
            release.set()
            # Empty local registry was asserted above; remove only its fixture schema.
            db.execute("DROP SCHEMA IF EXISTS corpora_session_guard CASCADE")
            for table in (
                "version_intents",
                "version_saves",
                "deletions",
                "access",
                "imports",
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


def _exercise_history_paging(api, db, owner, foreign, admin):
    """Create synthetic, constraint-valid rows to exercise actual SQL paging."""
    corpus, provision, provision_claim = (str(uuid4()) for _ in range(3))
    actor, session = owner[0]["sub"], owner[0]["session_id"]
    repo = "fixture/corpus-" + corpus
    db.execute(
        "INSERT INTO public.corpora_corpus_metadata (id,owner_id,title,hf_repo_id,hf_repo_type) "
        "VALUES (%s,%s,'History fixture',%s,'dataset')",
        (corpus, actor, repo),
    )
    db.execute(
        "INSERT INTO public.corpora_corpus_provision_ops "
        "(id,corpus_id,actor_id,session_id,repo_id,repo_type) VALUES (%s,%s,%s,%s,%s,'dataset')",
        (provision, corpus, actor, session, repo),
    )
    db.execute(
        "UPDATE public.corpora_corpus_provision_ops SET state='executing',worker_claim=%s "
        "WHERE id=%s",
        (provision_claim, provision),
    )
    with db.transaction():
        db.execute(
            "INSERT INTO public.corpora_corpus_bootstrap VALUES (%s,%s,%s,%s,DEFAULT)",
            (corpus, provision, provision_claim, "d" * 40),
        )
        db.execute(
            "UPDATE public.corpora_corpus_provision_ops SET state='completed' WHERE id=%s",
            (provision,),
        )
    parent = None
    for sequence in range(1, 4):
        operation, claim, publication = (str(uuid4()) for _ in range(3))
        commit, stage = str(sequence) * 40, "abc"[sequence - 1] * 40
        base = parent or "d" * 40
        db.execute(
            "INSERT INTO public.corpora_corpus_write_ops "
            "(id,corpus_id,actor_id,session_id,expected_sequence,expected_parent_sha,"
            "input_sha256,input_format) VALUES (%s,%s,%s,%s,%s,%s,%s,'corpus')",
            (operation, corpus, actor, session, sequence - 1, parent, "e" * 64),
        )
        db.execute(
            "UPDATE public.corpora_corpus_write_ops SET state='executing',worker_claim=%s "
            "WHERE id=%s",
            (claim, operation),
        )
        db.execute(
            "INSERT INTO public.corpora_corpus_staged_inputs "
            "(operation_id,corpus_id,worker_claim,repo_id,repo_type,input_format,input_sha256,"
            "archive_size,requested_base_sha,stage_sha,stage_ref,stage_path) "
            "VALUES (%s,%s,%s,%s,'dataset','corpus',%s,100,%s,%s,%s,%s)",
            (
                operation,
                corpus,
                claim,
                repo,
                "e" * 64,
                base,
                stage,
                "corpora-stage-" + operation,
                ".corpora-staging/" + operation + "/" + claim + ".corpus",
            ),
        )
        db.execute(
            "INSERT INTO public.corpora_corpus_publication_claims "
            "(operation_id,corpus_id,stage_claim,publication_claim) VALUES (%s,%s,%s,%s)",
            (operation, corpus, claim, publication),
        )
        with db.transaction():
            db.execute(
                "INSERT INTO public.corpora_corpus_versions "
                "(corpus_id,sequence,commit_sha,parent_sha,operation_id,publication_claim,"
                "retained_ref) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (
                    corpus,
                    sequence,
                    commit,
                    parent,
                    operation,
                    publication,
                    "corpora-op-" + operation,
                ),
            )
            if sequence == 1:
                db.execute(
                    "INSERT INTO public.corpora_corpus_heads VALUES (%s,%s,%s)",
                    (corpus, sequence, commit),
                )
                db.execute(
                    "UPDATE public.corpora_corpus_metadata SET state='ready' WHERE id=%s", (corpus,)
                )
            else:
                db.execute(
                    "UPDATE public.corpora_corpus_heads SET sequence=%s,commit_sha=%s "
                    "WHERE corpus_id=%s",
                    (sequence, commit, corpus),
                )
            db.execute(
                "UPDATE public.corpora_corpus_write_ops SET state='completed' WHERE id=%s",
                (operation,),
            )
        parent = commit
    db.execute(
        "UPDATE public.corpora_corpus_access SET visibility='public' WHERE corpus_id=%s", (corpus,)
    )
    path = f"/v1/corpora/{corpus}/versions"

    def headers(user):
        return {"Authorization": "Bearer " + user[1]}

    assert api.get(path).status_code == 401
    assert api.get(path, headers=headers(foreign)).status_code == 404
    first = api.get(path, headers=headers(owner), params={"limit": 2}).json()
    assert first["current_sequence"] == 3
    assert [version["sequence"] for version in first["versions"]] == [3, 2]
    assert first["next_before"] == 2
    second = api.get(
        path, headers=headers(admin), params={"limit": 2, "before": first["next_before"]}
    ).json()
    assert [version["sequence"] for version in second["versions"]] == [1]
    assert second["next_before"] is None
    rpc = creation.CreationRPC()
    operation = str(uuid4())
    params = {
        **creation._session(owner[0]),
        "p_corpus": corpus,
        "p_operation": operation,
        "p_sequence": 3,
        "p_parent": "3" * 40,
        "p_digest": "f" * 64,
        "p_size": 123,
    }
    assert rpc.call("corpora_begin_version_save", {**params, **creation._session(foreign[0])}) == {
        "status": "not_found"
    }
    barrier = threading.Barrier(2)

    def begin(_):
        barrier.wait(timeout=5)
        return creation.CreationRPC().call("corpora_begin_version_save", params)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(begin, range(2)))
    assert sorted(result["status"] for result in results) == ["claimed", "existing"]
    claim = next(result["claim_id"] for result in results if result["status"] == "claimed")
    assert rpc.call("corpora_begin_version_save", {**params, "p_size": 124})["status"] == "conflict"
    base = rpc.call("corpora_version_save_base", {"p_operation": operation, "p_claim": claim})
    assert base["sequence"] == 3 and base["commit_sha"] == "3" * 40
    assert base["archive_size"] == 100 and base["archive_sha256"] == "e" * 64
    assert (
        rpc.call(
            "corpora_version_save_base",
            {
                "p_operation": operation,
                "p_claim": str(uuid4()),
            },
        )
        is None
    )
    publication = str(uuid4())
    staged = {
        "p_operation": operation,
        "p_claim": claim,
        "p_stage_sha": "9" * 40,
        "p_publication": publication,
    }
    assert rpc.call("corpora_stage_version_save", staged) is True
    assert rpc.call("corpora_stage_version_save", staged) is False
    completed = {
        "p_operation": operation,
        "p_claim": claim,
        "p_publication": publication,
        "p_sha": "4" * 40,
    }
    assert rpc.call("corpora_complete_version_save", completed) is True
    assert rpc.call("corpora_complete_version_save", completed) is True
    assert rpc.call("corpora_complete_version_save", {**completed, "p_sha": "5" * 40}) is False
    assert rpc.call("corpora_find_version_save_result", completed) is True
    access = rpc.call(
        "corpora_read_corpus_access",
        {
            **creation._session(owner[0]),
            "p_corpus": corpus,
        },
    )
    assert access["head"]["sequence"] == 4
    assert access["head"]["archive_size"] == 123
    assert access["head"]["archive_sha256"] == "f" * 64
    assert api.get(path, headers=headers(owner)).json()["current_sequence"] == 4
    assert (
        rpc.call(
            "corpora_begin_version_save",
            {
                **params,
                "p_operation": str(uuid4()),
            },
        )["status"]
        == "conflict"
    )
    assert (
        rpc.call(
            "corpora_version_save_base",
            {
                "p_operation": operation,
                "p_claim": claim,
            },
        )
        is None
    )
    old = rpc.call(
        "corpora_get_restore_source",
        {
            **creation._session(owner[0]),
            "p_corpus": corpus,
            "p_sequence": 2,
        },
    )
    assert old["sequence"] == 2 and old["commit_sha"] == "2" * 40
    assert old["archive_size"] == 100 and old["archive_sha256"] == "e" * 64
    assert rpc.call(
        "corpora_get_restore_source",
        {
            **creation._session(foreign[0]),
            "p_corpus": corpus,
            "p_sequence": 2,
        },
    ) == {"status": "not_found"}
    edit_operation = str(uuid4())
    intent_params = {
        **creation._session(owner[0]),
        "p_corpus": corpus,
        "p_operation": edit_operation,
        "p_intent": "a" * 64,
    }
    assert rpc.call("corpora_get_edit_intent", intent_params) == {"status": "absent"}
    edit_params = {
        **intent_params,
        "p_sequence": 4,
        "p_parent": "4" * 40,
        "p_digest": "b" * 64,
        "p_size": 200,
        "p_kind": "restore",
        "p_restore": 2,
    }
    barrier = threading.Barrier(2)

    def begin_edit(index):
        barrier.wait(timeout=5)
        # The same edit may compile to different archive bytes on a retry.
        return creation.CreationRPC().call(
            "corpora_begin_corpus_edit",
            {
                **edit_params,
                "p_digest": ("b" if index == 0 else "c") * 64,
            },
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        edits = list(pool.map(begin_edit, range(2)))
    assert sorted(edit["status"] for edit in edits) == ["claimed", "existing"]
    assert db.execute(
        "SELECT kind,restore_sequence FROM public.corpora_corpus_version_intents "
        "WHERE operation_id=%s",
        (edit_operation,),
    ).fetchone() == ("restore", 2)
    assert rpc.call("corpora_get_edit_intent", intent_params)["status"] == "existing"
    assert rpc.call("corpora_get_edit_intent", {**intent_params, "p_intent": "d" * 64}) == {
        "status": "conflict"
    }
    assert rpc.call(
        "corpora_get_edit_intent",
        {
            **intent_params,
            **creation._session(foreign[0]),
        },
    ) == {"status": "not_found"}
    db.execute(
        "UPDATE public.corpora_corpus_access SET visibility='private',deleted_at=now() "
        "WHERE corpus_id=%s",
        (corpus,),
    )
    assert api.get(path, headers=headers(admin)).status_code == 404


def _exercise_import_rpc(session, foreign, db, archive):
    """Real admission/finalization SQL with synthetic, internal Hub attestations."""
    rpc = creation.CreationRPC()
    operation = str(uuid4())
    params = {
        **session,
        "p_operation": operation,
        "p_revision": 5,
        "p_digest": hashlib.sha256(archive).hexdigest(),
        "p_size": len(archive),
    }
    denied = {**params, **creation._session(foreign[0])}
    assert rpc.call("corpora_begin_import", denied) == {"status": "not_found"}
    barrier = threading.Barrier(2)

    def begin(_):
        barrier.wait(timeout=5)
        return creation.CreationRPC().call("corpora_begin_import", params)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(begin, range(2)))
    assert sorted(r["status"] for r in results) == ["claimed", "existing"]
    claim = next(r["claim_id"] for r in results if r["status"] == "claimed")
    assert rpc.call("corpora_begin_import", {**params, "p_digest": "e" * 64}) == {
        "status": "conflict"
    }
    assert rpc.call("corpora_begin_import", {**params, "p_operation": str(uuid4())}) == {
        "status": "conflict"
    }
    publication = str(uuid4())
    complete = {
        "p_operation": operation,
        "p_claim": claim,
        "p_publication": publication,
        "p_sha": "c" * 40,
    }
    assert rpc.call("corpora_complete_import", complete) is False
    stage = {
        "p_operation": operation,
        "p_claim": claim,
        "p_stage_sha": "b" * 40,
        "p_publication": publication,
    }
    assert rpc.call("corpora_stage_import", {**stage, "p_claim": str(uuid4())}) is False
    assert rpc.call("corpora_stage_import", stage) is True
    assert rpc.call("corpora_stage_import", stage) is False  # one publication dispatch
    assert rpc.call("corpora_complete_import", {**complete, "p_publication": str(uuid4())}) is False
    assert rpc.call("corpora_complete_import", complete) is True
    assert rpc.call("corpora_complete_import", complete) is True
    assert rpc.call("corpora_find_import_result", complete) is True
    assert rpc.call("corpora_find_import_result", {**complete, "p_sha": "f" * 40}) is False
    assert (
        rpc.call("corpora_mark_import_uncertain", {"p_operation": operation, "p_claim": claim})
        is False
    )
    result = rpc.call("corpora_get_import", {**session, "p_operation": operation})
    assert result["state"] == "completed" and result["sequence"] == 1
    assert rpc.call("corpora_begin_import", params) == result
    assert rpc.call("corpora_begin_import", {**params, "p_operation": str(uuid4())}) == {
        "status": "conflict"
    }
    assert db.execute(
        "SELECT m.state,m.revision,h.sequence,o.state FROM public.corpora_corpus_metadata m "
        "JOIN public.corpora_corpus_heads h ON h.corpus_id=m.id "
        "JOIN public.corpora_corpus_write_ops o ON o.corpus_id=m.id "
        "WHERE m.id=%s",
        (session["p_corpus"],),
    ).fetchone() == ("ready", 6, 1, "completed")
    for table in (
        "imports",
        "write_ops",
        "staged_inputs",
        "publication_claims",
        "versions",
        "heads",
    ):
        for privilege in ("INSERT", "UPDATE", "DELETE"):
            assert not db.execute(
                "SELECT has_table_privilege('service_role',%s,%s)",
                ("public.corpora_corpus_" + table, privilege),
            ).fetchone()[0]
    return operation


def _exercise_real_edit_route(api, corpus, admin, foreign, monkeypatch, archive, db):
    """Actual Auth, SQL and core edit; only remote Hub publication is simulated."""
    from corpus_validate import read_archive

    from corpora_api import corpus_edit_api as edit_api

    uploads = []

    async def transfer(_row, sha, path, *, max_bytes, timeout, sink):
        assert sha == "c" * 40 and path == ".corpora/source.corpus"
        assert max_bytes == len(archive)
        await sink(archive)

    def publish(claim, _filename, data, *, slot):
        uploads.append(data)
        publication = str(uuid4())
        rpc = creation.CreationRPC()
        params = {
            "p_operation": str(claim.operation_id),
            "p_claim": str(claim.claim_id),
            "p_publication": publication,
        }
        stage_sha, final_sha = ("d" * 40, "e" * 40) if len(uploads) == 1 else ("6" * 40, "f" * 40)
        assert rpc.call("corpora_stage_version_save", {**params, "p_stage_sha": stage_sha}) is True
        assert rpc.call("corpora_complete_version_save", {**params, "p_sha": final_sha}) is True

    body = {
        "operation_id": str(uuid4()),
        "expected_sequence": 1,
        "expected_commit": "c" * 40,
        "changes": {"description": "Edited through the API"},
    }
    path = f"/v1/corpora/{corpus}/manifest"
    with monkeypatch.context() as patch:
        patch.setattr(edit_api.hub_reader, "transfer_authorized", transfer)
        patch.setattr(edit_api, "run_import_worker", publish)
        assert (
            api.patch(
                path, headers={"Authorization": "Bearer " + foreign[1]}, json=body
            ).status_code
            == 404
        )
        saved = api.patch(path, headers={"Authorization": "Bearer " + admin[1]}, json=body)
        assert saved.status_code == 201, saved.text
        assert saved.json()["sequence"] == 2
        retried = api.patch(path, headers={"Authorization": "Bearer " + admin[1]}, json=body)
        assert retried.status_code == 200 and retried.json() == saved.json()
        assert (
            api.patch(
                path,
                headers={"Authorization": "Bearer " + admin[1]},
                json={**body, "changes": {"description": "different"}},
            ).status_code
            == 409
        )
        restore_id = str(uuid4())
        restore_body = {
            "operation_id": restore_id,
            "expected_sequence": 2,
            "expected_commit": "e" * 40,
        }
        restored = api.post(
            f"/v1/corpora/{corpus}/versions/1/restore",
            headers={"Authorization": "Bearer " + admin[1]},
            json=restore_body,
        )
        assert restored.status_code == 201, restored.text
        assert restored.json()["sequence"] == 3
        assert (
            api.post(
                f"/v1/corpora/{corpus}/versions/1/restore",
                headers={"Authorization": "Bearer " + admin[1]},
                json=restore_body,
            ).status_code
            == 200
        )
        assert db.execute(
            "SELECT restore_sequence FROM public.corpora_corpus_version_intents "
            "WHERE operation_id=%s",
            (restore_id,),
        ).fetchone() == (1,)
    assert len(uploads) == 2
    assert read_archive(uploads[0], "manifest")["description"] == "Edited through the API"
    assert uploads[1] == archive
    return uploads[1]


def _exercise_access_lifecycle(
    api,
    corpus,
    empty_corpus,
    owner,
    foreign,
    admin,
    db,
    monkeypatch,
    archive,
    *,
    head_sha,
    version_count,
):
    from corpora_api import corpus_access_api as access
    from corpora_api import corpus_detail_api as detail
    from corpora_api.corpus_hub_deadline import DeadlineCorpusHubReader

    path = "/v1/corpora/" + corpus

    def header(user):
        return {"Authorization": "Bearer " + user[1]}

    hub_deleted, hub_calls = False, []

    def hub(request):
        nonlocal hub_deleted
        hub_calls.append(request.method)
        assert request.headers["authorization"] == "Bearer test-placeholder"
        if "/resolve/" in request.url.path:
            assert request.url.path.endswith("/resolve/" + head_sha + "/.corpora/source.corpus")
            return httpx.Response(200, stream=httpx.ByteStream(archive))
        if request.method == "DELETE":
            assert json.loads(request.content) == {
                "name": "corpus-" + corpus,
                "organization": "fixture",
                "type": "dataset",
            }
            assert db.execute(
                "SELECT deleted_at IS NOT NULL FROM public.corpora_corpus_access "
                "WHERE corpus_id=%s",
                (corpus,),
            ).fetchone()[0]
            hub_deleted = True
            return httpx.Response(200, stream=httpx.ByteStream(b"{}"))
        return httpx.Response(
            404 if hub_deleted else 200,
            stream=httpx.ByteStream(
                json.dumps(
                    {"id": "fixture/corpus-" + corpus, "private": True, "sha": head_sha}
                ).encode()
            ),
        )

    lifecycle = access.PrivateHubLifecycle
    monkeypatch.setattr(
        access, "PrivateHubLifecycle", lambda: lifecycle(lambda: httpx.MockTransport(hub))
    )
    monkeypatch.setattr(
        detail, "hub_reader", DeadlineCorpusHubReader(lambda: httpx.MockTransport(hub))
    )
    views = ("manifest", "index", "sections", "content", "nodes/3")

    def denied_reads(headers, code):
        before = len(hub_calls)
        for view in views:
            assert api.get(path + "/" + view, headers=headers).status_code == code
        assert len(hub_calls) == before

    def allowed_reads(headers):
        for view in views:
            result = api.get(path + "/" + view, headers=headers)
            assert result.status_code == 200, result.text
            assert result.headers["cache-control"] == "no-store"
            if view == "content":
                assert result.json()["passages"][0]["text"] == "In the gap and out."
                assert [t["node"] for p in result.json()["passages"] for t in p["tokens"]] == [
                    1,
                    2,
                    3,
                    4,
                    5,
                ]

    denied_reads({}, 404)
    denied_reads(header(foreign), 404)
    allowed_reads(header(admin))
    assert api.get(path + "/metadata").status_code == 404
    assert api.get(path + "/visibility").status_code == 404
    assert api.get(path + "/metadata", headers=header(foreign)).status_code == 404
    patch = {"expected_revision": 6, "visibility": "public"}
    assert api.patch(path + "/visibility", headers=header(foreign), json=patch).status_code == 404
    published = api.patch(path + "/visibility", headers=header(admin), json=patch)
    assert published.status_code == 200 and published.json()["revision"] == 7
    assert api.get(path + "/metadata").status_code == 200
    assert api.get(path + "/metadata", headers=header(foreign)).status_code == 200
    assert (
        api.get(path + "/metadata", headers=header(owner)).status_code == 401
    )  # revoked is not anonymous
    assert api.get(path + "/metadata", headers={"Authorization": ""}).status_code == 401
    assert api.get(path + "/visibility").json() == published.json()
    allowed_reads({})
    allowed_reads(header(foreign))
    denied_reads(header(owner), 401)  # Logged-out tokens are never anonymous.
    assert api.patch(path + "/visibility", headers=header(admin), json=patch).status_code == 409
    private = api.patch(
        path + "/visibility",
        headers=header(admin),
        json={"expected_revision": 7, "visibility": "private"},
    )
    assert private.status_code == 200 and private.json()["revision"] == 8
    assert api.get(path + "/metadata").status_code == 404
    assert api.get(path + "/metadata", headers=header(admin)).status_code == 200
    denied_reads({}, 404)
    denied_reads(header(foreign), 404)
    published = api.patch(
        path + "/visibility",
        headers=header(admin),
        json={"expected_revision": 8, "visibility": "public"},
    )
    assert published.status_code == 200 and published.json()["revision"] == 9
    operation = str(uuid4())
    deletion = {"operation_id": operation, "expected_revision": 9}
    assert api.request("DELETE", path, headers=header(foreign), json=deletion).status_code == 404
    deleted = api.request("DELETE", path, headers=header(admin), json=deletion)
    assert deleted.status_code == 200 and deleted.json()["state"] == "completed"
    assert hub_deleted and hub_calls.count("DELETE") == 1
    assert (
        api.request("DELETE", path, headers=header(admin), json=deletion).json() == deleted.json()
    )
    assert hub_calls.count("DELETE") == 1
    assert api.get(path + "/deletions/" + operation, headers=header(admin)).json() == deleted.json()
    assert api.get(path + "/deletions/" + operation, headers=header(foreign)).status_code == 404
    assert api.get(path).status_code == 401
    for user_headers in ({}, header(admin), header(foreign)):
        assert api.get(path + "/metadata", headers=user_headers).status_code == 404
        assert api.get(path + "/visibility", headers=user_headers).status_code == 404
        denied_reads(user_headers, 404)
    assert api.get(path, headers=header(admin)).status_code == 404
    assert (
        api.patch(
            path + "/metadata",
            headers=header(admin),
            json={"expected_revision": 10, "title": "Revived"},
        ).status_code
        == 404
    )
    listed = api.get(
        "/v1/corpora", headers=header(admin), params={"owner_id": owner[0]["sub"]}
    ).json()
    assert corpus not in [x["id"] for x in listed["items"]]
    with pytest.raises(Exception, match="CORPUS_ACCESS_IMMUTABLE"):
        with db.transaction():
            db.execute(
                "UPDATE public.corpora_corpus_access SET deleted_at=NULL WHERE corpus_id=%s",
                (corpus,),
            )
    with pytest.raises(Exception, match="CORPUS_DELETED"):
        with db.transaction():
            db.execute(
                "UPDATE public.corpora_corpus_metadata SET title=title WHERE id=%s", (corpus,)
            )
    assert (
        db.execute(
            "SELECT count(*) FROM public.corpora_corpus_versions WHERE corpus_id=%s", (corpus,)
        ).fetchone()[0]
        == version_count
    )

    # Metadata is the common lock: deletion and an initial import cannot both win.
    params = {
        **creation._session(admin[0]),
        "p_corpus": empty_corpus,
        "p_operation": str(uuid4()),
        "p_revision": 1,
    }
    barrier = threading.Barrier(2)

    def compete(kind):
        barrier.wait(timeout=5)
        if kind == "import":
            return creation.CreationRPC().call(
                "corpora_begin_import", {**params, "p_digest": "d" * 64, "p_size": 123}
            )
        return creation.CreationRPC().call("corpora_begin_corpus_deletion", params)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(compete, ("import", "delete")))
    assert sum(r["status"] == "claimed" for r in results) == 1
    tombstoned = db.execute(
        "SELECT deleted_at IS NOT NULL FROM public.corpora_corpus_access WHERE corpus_id=%s",
        (empty_corpus,),
    ).fetchone()[0]
    writes = db.execute(
        "SELECT count(*) FROM public.corpora_corpus_write_ops WHERE corpus_id=%s", (empty_corpus,)
    ).fetchone()[0]
    assert (tombstoned, writes) in ((True, 0), (False, 1))
    for table in ("access", "deletions"):
        for role in ("anon", "authenticated", "service_role"):
            for privilege in ("INSERT", "UPDATE", "DELETE"):
                assert not db.execute(
                    "SELECT has_table_privilege(%s,%s,%s)",
                    (role, "public.corpora_corpus_" + table, privilege),
                ).fetchone()[0]
    for (signature,) in db.execute(
        "SELECT oid::regprocedure::text FROM pg_proc WHERE pronamespace='public'::regnamespace "
        "AND proname IN ('corpora_read_corpus_access','corpora_set_corpus_visibility',"
        "'corpora_get_corpus_deletion','corpora_begin_corpus_deletion','corpora_finish_corpus_deletion')"
    ).fetchall():
        for role in ("anon", "authenticated", "service_role"):
            assert db.execute(
                "SELECT has_function_privilege(%s,%s,'EXECUTE')", (role, signature)
            ).fetchone()[0] == (role == "service_role")
