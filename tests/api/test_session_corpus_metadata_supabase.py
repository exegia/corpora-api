"""Opt-in session-bound metadata writes using disposable local Supabase."""

from __future__ import annotations

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
from corpora_api.corpus_metadata import (
    CorpusMetadataConflict,
    CorpusMetadataError,
    CorpusMetadataNotFound,
    CorpusMetadataStore,
)
from corpora_api.session_corpus_metadata import (
    SessionBoundCorpusMetadata,
    SessionBoundMetadataTransaction,
)

pytestmark = pytest.mark.skipif(
    not os.getenv("CORPORA_TEST_LOCAL_SUPABASE") or not os.getenv("CORPORA_SESSION_GUARD_ROOT"),
    reason="requires disposable local Supabase and the session-guard draft",
)


def test_metadata_update_is_bound_to_real_session_and_transaction(monkeypatch):
    psycopg = pytest.importorskip("psycopg")
    root = Path(os.environ["CORPORA_SESSION_GUARD_ROOT"])
    url, dsn = os.environ["API_URL"].rstrip("/"), os.environ["DB_URL"]
    assert urlparse(url).hostname in {"localhost", "127.0.0.1", "::1"}
    assert urlparse(dsn).hostname in {"localhost", "127.0.0.1", "::1"}
    anon, service = os.environ["ANON_KEY"], os.environ["SERVICE_ROLE_KEY"]
    for name, value in {
        "SUPABASE_URL": url,
        "SUPABASE_SERVICE_ROLE_KEY": service,
        "SUPABASE_JWKS_URL": url + "/auth/v1/.well-known/jwks.json",
        "SUPABASE_JWT_ISSUER": url + "/auth/v1",
        "HF_CORPUS_ORGANIZATION": "fixture-org",
    }.items():
        monkeypatch.setenv(name, value)
    migration = (
        Path(__file__).resolve().parents[2]
        / "supabase/migrations/20261116043420_corpus_metadata_registry.sql"
    ).read_text()
    guard = (root / "supabase/migrations/20261115165514_stored_session_reads.sql").read_text()
    guard = guard.split("CREATE FUNCTION public.corpora_stored_session_get", 1)[0]
    auth_dsn = psycopg.conninfo.make_conninfo(dsn, user="supabase_auth_admin")
    users, granted = [], False
    with (
        psycopg.connect(dsn, autocommit=True) as db,
        psycopg.connect(auth_dsn, autocommit=True) as auth_db,
        httpx.Client(timeout=15) as api,
    ):
        assert db.execute(
            "SELECT to_regclass('public.corpora_corpus_metadata') IS NULL"
        ).fetchone()[0]
        assert db.execute(
            "SELECT to_regprocedure('public.corpora_stored_lock_session(uuid,uuid,bigint)') IS NULL"
        ).fetchone()[0]
        try:
            for column, privilege in (
                ("id", "SELECT"),
                ("user_id", "SELECT"),
                ("not_after", "SELECT"),
                ("not_after", "UPDATE"),
            ):
                assert not auth_db.execute(
                    "SELECT has_column_privilege('service_role','auth.sessions',%s,%s)",
                    (column, privilege),
                ).fetchone()[0]
            auth_db.execute(
                "GRANT SELECT(id,user_id,not_after), UPDATE(not_after) "
                "ON auth.sessions TO service_role"
            )
            granted = True
            db.execute(migration)
            db.execute(guard)
            db.execute(
                "REVOKE ALL ON FUNCTION public.corpora_stored_lock_session(uuid,uuid,bigint) "
                "FROM PUBLIC, anon, authenticated"
            )
            db.execute(
                "GRANT EXECUTE ON FUNCTION public.corpora_stored_lock_session(uuid,uuid,bigint) "
                "TO service_role"
            )
            db.execute("NOTIFY pgrst, 'reload schema'")
            for _ in range(3):
                response = api.post(
                    url + "/auth/v1/signup",
                    headers={"apikey": anon},
                    json={"email": f"{uuid4()}@example.org", "password": str(uuid4())},
                )
                response.raise_for_status()
                token = response.json()["access_token"]
                users.append((verify_user_token(token), token))
            monkeypatch.setenv("CORPORA_SUPERADMIN_USER_ID", users[2][0]["sub"])
            time.sleep(1)
            metadata = CorpusMetadataStore()
            identifier = str(uuid4())
            metadata.reserve(users[0][0]["sub"], identifier, "Original", repo_type="dataset")
            lease = SessionBoundMetadataTransaction(
                lambda: psycopg.connect(dsn, autocommit=True), assume_service_role=True
            )
            editor = SessionBoundCorpusMetadata(lease)
            updated = editor.update(
                users[0][0], identifier, 1, {"title": "Revised", "language": "grc"}
            )
            assert (
                updated.revision == 2 and updated.title == "Revised" and updated.language == "grc"
            )
            with pytest.raises(CorpusMetadataNotFound):
                editor.update(users[1][0], identifier, 2, {"title": "Foreign"})
            with pytest.raises(CorpusMetadataConflict):
                editor.update(users[0][0], identifier, 1, {"title": "Stale"})
            with pytest.raises(CorpusMetadataError):
                editor.update(users[0][0], identifier, 2, {"state": "ready"})
            admin_result = editor.update(users[2][0], identifier, 2, {"description": "Admin edit"})
            assert admin_result.revision == 3

            # A bad repository link in the returned row must abort the update.
            monkeypatch.setenv("HF_CORPUS_ORGANIZATION", "wrong-org")
            with pytest.raises(HTTPException) as bad_link:
                editor.update(users[0][0], identifier, 3, {"title": "Must roll back"})
            assert bad_link.value.status_code == 503
            monkeypatch.setenv("HF_CORPUS_ORGANIZATION", "fixture-org")
            assert metadata.get(users[0][0]["sub"], identifier).revision == 3

            # The database's JSONB text limit remains authoritative.
            expanded = {str(index): 1e-300 for index in range(80)}
            with pytest.raises(CorpusMetadataError, match="Invalid corpus metadata update"):
                editor.update(users[0][0], identifier, 3, {"properties": expanded})
            assert metadata.get(users[0][0]["sub"], identifier).revision == 3

            monkeypatch.setenv("CORPORA_SUPERADMIN_USER_ID", "invalid")
            with pytest.raises(HTTPException) as bad_admin:
                editor.update(users[0][0], identifier, 3, {"title": "Must roll back"})
            assert bad_admin.value.status_code == 503
            monkeypatch.setenv("CORPORA_SUPERADMIN_USER_ID", users[2][0]["sub"])
            assert metadata.get(users[0][0]["sub"], identifier).revision == 3

            # Terminating the lease connection after UPDATE must roll the write back.
            class DisconnectLease:
                def run(self, claims, operation):
                    def kill_after_write(owner, connection):
                        result = operation(owner, connection)
                        pid = connection.execute("SELECT pg_backend_pid()").fetchone()[0]
                        assert db.execute("SELECT pg_terminate_backend(%s)", (pid,)).fetchone()[0]
                        return result

                    return lease.run(claims, kill_after_write)

            with pytest.raises(HTTPException) as disconnected:
                SessionBoundCorpusMetadata(DisconnectLease()).update(
                    users[0][0], identifier, 3, {"title": "Must roll back"}
                )
            assert disconnected.value.status_code == 503
            assert metadata.get(users[0][0]["sub"], identifier).revision == 3
            assert metadata.get(users[0][0]["sub"], identifier).title == "Revised"

            entered, release, logout_done = threading.Event(), threading.Event(), threading.Event()
            results, failures = [], []

            class PausedLease:
                def run(self, claims, operation):
                    def pause_after_write(owner, connection):
                        result = operation(owner, connection)
                        entered.set()
                        if not release.wait(15):
                            raise RuntimeError("test release timed out")
                        return result

                    return lease.run(claims, pause_after_write)

            def edit():
                try:
                    results.append(
                        SessionBoundCorpusMetadata(PausedLease())
                        .update(users[0][0], identifier, 3, {"title": "Committed"})
                        .title
                    )
                except Exception as error:
                    failures.append(error)
                    entered.set()

            def logout():
                try:
                    with httpx.Client(timeout=15) as client:
                        result = client.post(
                            url + "/auth/v1/logout",
                            headers={"apikey": anon, "Authorization": "Bearer " + users[0][1]},
                        )
                        results.append(result.status_code)
                except Exception as error:
                    failures.append(error)
                finally:
                    logout_done.set()

            editor_thread = threading.Thread(target=edit, daemon=True)
            logout_thread = threading.Thread(target=logout, daemon=True)
            editor_thread.start()
            try:
                assert entered.wait(10) and not failures
                logout_thread.start()
                assert not logout_done.wait(0.5)
            finally:
                release.set()
                editor_thread.join(timeout=10)
                if logout_thread.ident is not None:
                    logout_thread.join(timeout=10)
            assert not editor_thread.is_alive() and not logout_thread.is_alive() and not failures
            assert sorted(str(value) for value in results) == ["204", "Committed"]
            with pytest.raises(HTTPException) as revoked:
                editor.update(users[0][0], identifier, 4, {"title": "After logout"})
            assert revoked.value.status_code == 401
            with pytest.raises(HTTPException) as revoked_invalid:
                editor.update(users[0][0], identifier, 4, {"state": "ready"})
            assert revoked_invalid.value.status_code == 401
            assert metadata.get(users[2][0]["sub"], identifier).title == "Committed"
        finally:
            db.execute("DROP TABLE IF EXISTS public.corpora_corpus_metadata")
            db.execute("DROP FUNCTION IF EXISTS public.corpora_corpus_metadata_stamp()")
            db.execute(
                "DROP FUNCTION IF EXISTS public.corpora_stored_lock_session(uuid,uuid,bigint)"
            )
            db.execute("NOTIFY pgrst, 'reload schema'")
            if granted:
                auth_db.execute(
                    "REVOKE SELECT(id,user_id,not_after), UPDATE(not_after) "
                    "ON auth.sessions FROM service_role"
                )
                for column, privilege in (
                    ("id", "SELECT"),
                    ("user_id", "SELECT"),
                    ("not_after", "SELECT"),
                    ("not_after", "UPDATE"),
                ):
                    assert not auth_db.execute(
                        "SELECT has_column_privilege('service_role','auth.sessions',%s,%s)",
                        (column, privilege),
                    ).fetchone()[0]
            for claims, _token in users:
                api.delete(
                    url + "/auth/v1/admin/users/" + claims["sub"],
                    headers={"apikey": service, "Authorization": "Bearer " + service},
                ).raise_for_status()
