"""Opt-in session-bound metadata writes using disposable local Supabase."""

from __future__ import annotations

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
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
from corpora_api.corpus_save_claim import CorpusSaveClaimer
from corpora_api.corpus_save_finalize import CorpusSaveFinalizer, TrustedCandidateAttestation
from corpora_api.session_corpus_metadata import (
    SessionBoundCorpusMetadata,
    SessionBoundMetadataTransaction,
)
from corpora_api.session_corpus_versions import SessionBoundCorpusSaveAdmission

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
    users, credentials, granted = [], [], False
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
            version_migration = next(
                (Path(__file__).resolve().parents[2] / "supabase/migrations").glob(
                    "*_corpus_content_versions.sql"
                )
            )
            db.execute(version_migration.read_text())
            assert not db.execute(
                "SELECT has_column_privilege('service_role',"
                "'public.corpora_corpus_write_ops','id','INSERT')"
            ).fetchone()[0]
            # Test-only DML privilege; this is not a proposed hosted grant.
            db.execute(
                "GRANT INSERT (id,corpus_id,actor_id,session_id,expected_sequence,"
                "expected_parent_sha,input_sha256) ON public.corpora_corpus_write_ops "
                "TO service_role"
            )
            db.execute(
                "GRANT UPDATE (state,worker_claim) ON public.corpora_corpus_write_ops "
                "TO service_role"
            )
            db.execute(
                "GRANT INSERT (corpus_id,sequence,commit_sha,parent_sha,operation_id,retained_ref) "
                "ON public.corpora_corpus_versions TO service_role"
            )
            db.execute(
                "GRANT INSERT (corpus_id,sequence,commit_sha), UPDATE (sequence,commit_sha) "
                "ON public.corpora_corpus_heads TO service_role"
            )
            db.execute("GRANT UPDATE (state) ON public.corpora_corpus_metadata TO service_role")
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
                email, password = f"{uuid4()}@example.org", str(uuid4())
                response = api.post(
                    url + "/auth/v1/signup",
                    headers={"apikey": anon},
                    json={"email": email, "password": password},
                )
                response.raise_for_status()
                token = response.json()["access_token"]
                users.append((verify_user_token(token), token))
                credentials.append((email, password))
            monkeypatch.setenv("CORPORA_SUPERADMIN_USER_ID", users[2][0]["sub"])
            time.sleep(1)
            metadata = CorpusMetadataStore()
            identifier = str(uuid4())
            lease = SessionBoundMetadataTransaction(
                lambda: psycopg.connect(dsn, autocommit=True), assume_service_role=True
            )
            editor = SessionBoundCorpusMetadata(lease)
            reserved = editor.reserve(users[0][0], identifier, " Original ", repo_type="dataset")
            assert reserved.state == "provisioning" and reserved.revision == 1
            assert (
                editor.reserve(users[0][0], identifier, "Original", repo_type="dataset") == reserved
            )
            with pytest.raises(CorpusMetadataConflict):
                editor.reserve(users[0][0], identifier, "Different", repo_type="dataset")
            with pytest.raises(CorpusMetadataConflict):
                editor.reserve(users[1][0], identifier, "Original", repo_type="dataset")
            duplicate_id = str(uuid4())
            barrier = threading.Barrier(2)

            def concurrent_reserve(_):
                barrier.wait(timeout=10)
                return editor.reserve(users[0][0], duplicate_id, "Concurrent", repo_type="dataset")

            with ThreadPoolExecutor(max_workers=2) as pool:
                duplicates = list(pool.map(concurrent_reserve, range(2)))
            assert duplicates[0] == duplicates[1]
            assert (
                db.execute(
                    "SELECT count(*) FROM public.corpora_corpus_metadata WHERE id=%s",
                    (duplicate_id,),
                ).fetchone()[0]
                == 1
            )
            admission = SessionBoundCorpusSaveAdmission(lease)
            operation_id = str(uuid4())
            saved = admission.admit(
                users[0][0],
                identifier,
                operation_id,
                expected_sequence=0,
                expected_parent_sha=None,
                input_sha256="a" * 64,
            )
            assert str(saved.operation_id) == operation_id and saved.state == "admitted"
            assert saved.retained_ref == "corpora-op-" + operation_id
            assert (
                admission.admit(
                    users[0][0],
                    identifier,
                    operation_id,
                    expected_sequence=0,
                    expected_parent_sha=None,
                    input_sha256="a" * 64,
                )
                == saved
            )
            login = api.post(
                url + "/auth/v1/token?grant_type=password",
                headers={"apikey": anon},
                json={"email": credentials[0][0], "password": credentials[0][1]},
            )
            login.raise_for_status()
            second_session = verify_user_token(login.json()["access_token"])
            assert second_session["session_id"] != users[0][0]["session_id"]
            assert (
                admission.admit(
                    second_session,
                    identifier,
                    operation_id,
                    expected_sequence=0,
                    expected_parent_sha=None,
                    input_sha256="a" * 64,
                )
                == saved
            )
            with pytest.raises(CorpusMetadataConflict):
                admission.admit(
                    users[0][0],
                    identifier,
                    operation_id,
                    expected_sequence=0,
                    expected_parent_sha=None,
                    input_sha256="b" * 64,
                )
            with pytest.raises(CorpusMetadataConflict):
                admission.admit(
                    users[0][0],
                    identifier,
                    str(uuid4()),
                    expected_sequence=0,
                    expected_parent_sha=None,
                    input_sha256="a" * 64,
                )
            with pytest.raises(CorpusMetadataNotFound):
                admission.admit(
                    users[1][0],
                    identifier,
                    str(uuid4()),
                    expected_sequence=0,
                    expected_parent_sha=None,
                    input_sha256="a" * 64,
                )
            admin_save = admission.admit(
                users[2][0],
                duplicate_id,
                str(uuid4()),
                expected_sequence=0,
                expected_parent_sha=None,
                input_sha256="a" * 64,
            )
            assert str(admin_save.actor_id) == users[2][0]["sub"]
            contested_corpus = str(uuid4())
            editor.reserve(users[0][0], contested_corpus, "Contested", repo_type="dataset")
            contest_barrier = threading.Barrier(2)

            def compete(_):
                contest_barrier.wait(timeout=10)
                try:
                    return admission.admit(
                        users[0][0],
                        contested_corpus,
                        str(uuid4()),
                        expected_sequence=0,
                        expected_parent_sha=None,
                        input_sha256="a" * 64,
                    ).state
                except CorpusMetadataConflict:
                    return "conflict"

            with ThreadPoolExecutor(max_workers=2) as pool:
                assert sorted(pool.map(compete, range(2))) == ["admitted", "conflict"]
            assert str(editor.get(users[0][0], identifier).id) == identifier
            with pytest.raises(CorpusMetadataNotFound):
                editor.get(users[1][0], identifier)
            assert str(editor.get(users[2][0], identifier).id) == identifier
            expected_ids = sorted([identifier, duplicate_id, contested_corpus])
            first_page, cursor = editor.list_owned(users[0][0], limit=1)
            assert [str(row.id) for row in first_page] == expected_ids[:1]
            assert cursor == expected_ids[0]
            second_page, cursor = editor.list_owned(users[0][0], limit=1, after=cursor)
            assert [str(row.id) for row in second_page] == expected_ids[1:2]
            assert cursor == expected_ids[1]
            third_page, cursor = editor.list_owned(users[0][0], limit=1, after=cursor)
            assert [str(row.id) for row in third_page] == expected_ids[2:] and cursor is None
            admin_page, _ = editor.list_owned(users[2][0], owner_id=users[0][0]["sub"])
            assert [str(row.id) for row in admin_page] == expected_ids
            with pytest.raises(CorpusMetadataNotFound):
                editor.list_owned(users[1][0], owner_id=users[0][0]["sub"])
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

            expiring_claims = {**users[0][0], "exp": int(time.time()) + 1}

            class ExpiringLease:
                def run(self, claims, operation):
                    def expire_after_write(owner, connection):
                        result = operation(owner, connection)
                        while time.time() <= claims["exp"]:
                            time.sleep(0.05)
                        return result

                    return lease.run(claims, expire_after_write)

            expired_id = str(uuid4())
            with pytest.raises(HTTPException) as expired:
                SessionBoundCorpusMetadata(ExpiringLease()).reserve(
                    expiring_claims, expired_id, "Expired", repo_type="dataset"
                )
            assert expired.value.status_code == 401
            assert (
                db.execute(
                    "SELECT count(*) FROM public.corpora_corpus_metadata WHERE id=%s", (expired_id,)
                ).fetchone()[0]
                == 0
            )

            expired_save_corpus, expired_save_id = str(uuid4()), str(uuid4())
            editor.reserve(users[0][0], expired_save_corpus, "Expired save", repo_type="dataset")
            expiring_save_claims = {**users[0][0], "exp": int(time.time()) + 1}
            with pytest.raises(HTTPException) as expired_save:
                SessionBoundCorpusSaveAdmission(ExpiringLease()).admit(
                    expiring_save_claims,
                    expired_save_corpus,
                    expired_save_id,
                    expected_sequence=0,
                    expected_parent_sha=None,
                    input_sha256="a" * 64,
                )
            assert expired_save.value.status_code == 401
            assert (
                db.execute(
                    "SELECT count(*) FROM public.corpora_corpus_write_ops WHERE id=%s",
                    (expired_save_id,),
                ).fetchone()[0]
                == 0
            )

            rolled_back_id = str(uuid4())
            with pytest.raises(HTTPException) as disconnected_reservation:
                SessionBoundCorpusMetadata(DisconnectLease()).reserve(
                    users[0][0], rolled_back_id, "No commit", repo_type="dataset"
                )
            assert disconnected_reservation.value.status_code == 503
            assert (
                db.execute(
                    "SELECT count(*) FROM public.corpora_corpus_metadata WHERE id=%s",
                    (rolled_back_id,),
                ).fetchone()[0]
                == 0
            )
            with pytest.raises(HTTPException) as disconnected:
                SessionBoundCorpusMetadata(DisconnectLease()).update(
                    users[0][0], identifier, 3, {"title": "Must roll back"}
                )
            assert disconnected.value.status_code == 503
            assert metadata.get(users[0][0]["sub"], identifier).revision == 3
            assert metadata.get(users[0][0]["sub"], identifier).title == "Revised"

            disconnected_corpus, disconnected_op = str(uuid4()), str(uuid4())
            editor.reserve(users[0][0], disconnected_corpus, "Disconnected", repo_type="dataset")
            with pytest.raises(HTTPException) as disconnected_admission:
                SessionBoundCorpusSaveAdmission(DisconnectLease()).admit(
                    users[0][0],
                    disconnected_corpus,
                    disconnected_op,
                    expected_sequence=0,
                    expected_parent_sha=None,
                    input_sha256="a" * 64,
                )
            assert disconnected_admission.value.status_code == 503
            assert (
                db.execute(
                    "SELECT count(*) FROM public.corpora_corpus_write_ops WHERE id=%s",
                    (disconnected_op,),
                ).fetchone()[0]
                == 0
            )

            lost_claim_corpus, lost_claim_op = str(uuid4()), str(uuid4())
            editor.reserve(users[0][0], lost_claim_corpus, "Lost claim", repo_type="dataset")
            admission.admit(
                users[0][0],
                lost_claim_corpus,
                lost_claim_op,
                expected_sequence=0,
                expected_parent_sha=None,
                input_sha256="a" * 64,
            )
            rollback_claim_corpus, rollback_claim_op = str(uuid4()), str(uuid4())
            editor.reserve(
                users[0][0], rollback_claim_corpus, "Rollback claim", repo_type="dataset"
            )
            admission.admit(
                users[0][0],
                rollback_claim_corpus,
                rollback_claim_op,
                expected_sequence=0,
                expected_parent_sha=None,
                input_sha256="a" * 64,
            )
            rollback_finalize_corpus, rollback_finalize_op = str(uuid4()), str(uuid4())
            editor.reserve(
                users[0][0], rollback_finalize_corpus, "Rollback finalize", repo_type="dataset"
            )
            admission.admit(
                users[0][0],
                rollback_finalize_corpus,
                rollback_finalize_op,
                expected_sequence=0,
                expected_parent_sha=None,
                input_sha256="a" * 64,
            )

            race_corpus, race_op = str(uuid4()), str(uuid4())
            editor.reserve(users[1][0], race_corpus, "Race", repo_type="dataset")
            race_entered, race_release, race_logout_done = (
                threading.Event(),
                threading.Event(),
                threading.Event(),
            )
            race_results, race_failures = [], []

            class PausedAdmission:
                def run(self, claims, operation):
                    def pause_after_insert(owner, connection):
                        result = operation(owner, connection)
                        race_entered.set()
                        if not race_release.wait(15):
                            raise RuntimeError("test release timed out")
                        return result

                    return lease.run(claims, pause_after_insert)

            def admit_before_logout():
                try:
                    result = SessionBoundCorpusSaveAdmission(PausedAdmission()).admit(
                        users[1][0],
                        race_corpus,
                        race_op,
                        expected_sequence=0,
                        expected_parent_sha=None,
                        input_sha256="a" * 64,
                    )
                    race_results.append(str(result.operation_id))
                except Exception as error:
                    race_failures.append(error)
                    race_entered.set()

            def race_logout():
                try:
                    with httpx.Client(timeout=15) as client:
                        response = client.post(
                            url + "/auth/v1/logout",
                            headers={"apikey": anon, "Authorization": "Bearer " + users[1][1]},
                        )
                        race_results.append(response.status_code)
                except Exception as error:
                    race_failures.append(error)
                finally:
                    race_logout_done.set()

            admission_thread = threading.Thread(target=admit_before_logout, daemon=True)
            signout_thread = threading.Thread(target=race_logout, daemon=True)
            admission_thread.start()
            try:
                assert race_entered.wait(10) and not race_failures
                signout_thread.start()
                assert not race_logout_done.wait(0.5)
            finally:
                race_release.set()
                admission_thread.join(timeout=10)
                if signout_thread.ident is not None:
                    signout_thread.join(timeout=10)
            assert (
                not admission_thread.is_alive()
                and not signout_thread.is_alive()
                and not race_failures
            )
            assert sorted(str(value) for value in race_results) == sorted(["204", race_op])
            assert db.execute(
                "SELECT state FROM public.corpora_corpus_write_ops WHERE id=%s", (race_op,)
            ).fetchone() == ("admitted",)
            with pytest.raises(HTTPException) as revoked_race:
                admission.admit(
                    users[1][0],
                    race_corpus,
                    str(uuid4()),
                    expected_sequence=0,
                    expected_parent_sha=None,
                    input_sha256="a" * 64,
                )
            assert revoked_race.value.status_code == 401

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
            with pytest.raises(HTTPException) as revoked_reservation:
                editor.reserve(users[0][0], str(uuid4()), "After logout", repo_type="dataset")
            assert revoked_reservation.value.status_code == 401
            with pytest.raises(HTTPException) as revoked_get:
                editor.get(users[0][0], identifier)
            assert revoked_get.value.status_code == 401
            with pytest.raises(HTTPException) as revoked_list:
                editor.list_owned(users[0][0])
            assert revoked_list.value.status_code == 401
            with pytest.raises(HTTPException) as revoked_save:
                admission.admit(
                    users[0][0],
                    identifier,
                    operation_id,
                    expected_sequence=0,
                    expected_parent_sha=None,
                    input_sha256="a" * 64,
                )
            assert revoked_save.value.status_code == 401
            with pytest.raises(HTTPException) as revoked_invalid_save:
                admission.admit(
                    users[0][0],
                    identifier,
                    "bad-id",
                    expected_sequence=-1,
                    expected_parent_sha=None,
                    input_sha256="bad",
                )
            assert revoked_invalid_save.value.status_code == 401
            assert db.execute(
                "SELECT state FROM public.corpora_corpus_write_ops WHERE id=%s", (operation_id,)
            ).fetchone() == ("admitted",)
            claimer = CorpusSaveClaimer(
                lambda: psycopg.connect(dsn, autocommit=True), assume_service_role=True
            )
            with pytest.raises(CorpusMetadataError, match="reconcile"):
                claimer.claim(operation_id)
            assert db.execute(
                "SELECT state,worker_claim FROM public.corpora_corpus_write_ops WHERE id=%s",
                (operation_id,),
            ).fetchone() == ("admitted", None)
            # Synthetic bootstrap rows simulate a trusted provisioning result.
            for cid in (
                identifier,
                lost_claim_corpus,
                rollback_claim_corpus,
                rollback_finalize_corpus,
                race_corpus,
            ):
                provision, claim = str(uuid4()), str(uuid4())
                db.execute(
                    "INSERT INTO public.corpora_corpus_provision_ops "
                    "(id,corpus_id,actor_id,session_id,repo_id,repo_type) "
                    "SELECT %s,id,owner_id,%s,hf_repo_id,hf_repo_type "
                    "FROM public.corpora_corpus_metadata WHERE id=%s",
                    (provision, str(uuid4()), cid),
                )
                db.execute(
                    "UPDATE public.corpora_corpus_provision_ops SET state='executing', "
                    "worker_claim=%s WHERE id=%s",
                    (claim, provision),
                )
                with db.transaction():
                    db.execute(
                        "INSERT INTO public.corpora_corpus_bootstrap "
                        "(corpus_id,operation_id,worker_claim,commit_sha) VALUES (%s,%s,%s,%s)",
                        (cid, provision, claim, "9" * 40),
                    )
                    db.execute(
                        "UPDATE public.corpora_corpus_provision_ops SET state='completed' "
                        "WHERE id=%s",
                        (provision,),
                    )
            claim_barrier = threading.Barrier(2)

            def compete_for_claim(_):
                claim_barrier.wait(timeout=10)
                return claimer.claim(operation_id)

            with ThreadPoolExecutor(max_workers=2) as pool:
                claims = list(pool.map(compete_for_claim, range(2)))
            winners = [item for item in claims if item is not None]
            assert len(winners) == 1 and sum(item is None for item in claims) == 1
            assert str(winners[0].operation_id) == operation_id
            assert winners[0].input_sha256 == "a" * 64
            assert winners[0].hub_parent_sha == "9" * 40
            assert claimer.claim(operation_id) is None
            assert db.execute(
                "SELECT state,worker_claim FROM public.corpora_corpus_write_ops WHERE id=%s",
                (operation_id,),
            ).fetchone() == ("executing", winners[0].claim_id)
            with pytest.raises(CorpusMetadataConflict):
                admission.admit(
                    users[2][0],
                    identifier,
                    str(uuid4()),
                    expected_sequence=0,
                    expected_parent_sha=None,
                    input_sha256="a" * 64,
                )

            def candidate_for(claim, sha):
                return TrustedCandidateAttestation(
                    operation_id=claim.operation_id,
                    claim_id=claim.claim_id,
                    corpus_id=claim.corpus_id,
                    repo_id=claim.repo_id,
                    repo_type=claim.repo_type,
                    parent_sha=claim.expected_parent_sha,
                    hub_parent_sha=claim.hub_parent_sha,
                    commit_sha=sha,
                    input_sha256=claim.input_sha256,
                    retained_ref=claim.retained_ref,
                )

            finalizer = CorpusSaveFinalizer(
                lambda: psycopg.connect(dsn, autocommit=True), assume_service_role=True
            )
            uncertain_claim = claimer.claim(race_op)
            assert uncertain_claim is not None
            db.execute(
                "UPDATE public.corpora_corpus_write_ops SET state='uncertain' WHERE id=%s",
                (race_op,),
            )
            with pytest.raises(CorpusMetadataConflict, match="reconciliation"):
                finalizer.finalize_trusted(candidate_for(uncertain_claim, "f" * 40))
            assert db.execute(
                "SELECT state FROM public.corpora_corpus_write_ops WHERE id=%s", (race_op,)
            ).fetchone() == ("uncertain",)
            # Synthetic assertions exercise database mechanics, not actual Hub verification.
            first_candidate = candidate_for(winners[0], "b" * 40)
            with pytest.raises(CorpusMetadataConflict):
                finalizer.finalize_trusted(replace(first_candidate, claim_id=uuid4()))
            with pytest.raises(CorpusMetadataConflict):
                finalizer.finalize_trusted(replace(first_candidate, input_sha256="c" * 64))
            with pytest.raises(CorpusMetadataConflict):
                finalizer.finalize_trusted(replace(first_candidate, hub_parent_sha="8" * 40))
            final_barrier = threading.Barrier(2)

            def finalize_same(_):
                final_barrier.wait(timeout=10)
                return finalizer.finalize_trusted(first_candidate)

            with ThreadPoolExecutor(max_workers=2) as pool:
                versions = list(pool.map(finalize_same, range(2)))
            assert versions[0] == versions[1] and versions[0].sequence == 1
            assert metadata.get(users[2][0]["sub"], identifier).state == "ready"
            assert (
                db.execute(
                    "SELECT count(*) FROM public.corpora_corpus_versions WHERE operation_id=%s",
                    (operation_id,),
                ).fetchone()[0]
                == 1
            )
            with pytest.raises(CorpusMetadataConflict):
                finalizer.finalize_trusted(replace(first_candidate, commit_sha="d" * 40))

            next_op = str(uuid4())
            admission.admit(
                users[2][0],
                identifier,
                next_op,
                expected_sequence=1,
                expected_parent_sha="b" * 40,
                input_sha256="a" * 64,
            )
            next_claim = claimer.claim(next_op)
            assert next_claim is not None
            assert next_claim.hub_parent_sha == "b" * 40
            next_version = finalizer.finalize_trusted(candidate_for(next_claim, "c" * 40))
            assert next_version.sequence == 2
            assert finalizer.finalize_trusted(first_candidate) == versions[0]

            competing_op = str(uuid4())
            admission.admit(
                users[2][0],
                identifier,
                competing_op,
                expected_sequence=2,
                expected_parent_sha="c" * 40,
                input_sha256="a" * 64,
            )
            competing_claim = claimer.claim(competing_op)
            assert competing_claim is not None
            competing_barrier = threading.Barrier(2)

            def finalize_different(sha):
                competing_barrier.wait(timeout=10)
                try:
                    finalizer.finalize_trusted(candidate_for(competing_claim, sha))
                    return "completed"
                except CorpusMetadataConflict:
                    return "conflict"

            with ThreadPoolExecutor(max_workers=2) as pool:
                outcomes = list(pool.map(finalize_different, ("1" * 40, "2" * 40)))
            assert sorted(outcomes) == ["completed", "conflict"]
            assert (
                db.execute(
                    "SELECT count(*) FROM public.corpora_corpus_versions WHERE operation_id=%s",
                    (competing_op,),
                ).fetchone()[0]
                == 1
            )

            class LostClaimResponse:
                def __enter__(self):
                    self.db = psycopg.connect(dsn, autocommit=True)
                    return self.db

                def __exit__(self, exc_type, exc, traceback):
                    self.db.close()
                    if exc_type is None:
                        raise OSError("simulated lost commit acknowledgement")
                    return False

            with pytest.raises(CorpusMetadataError, match="reconcile"):
                CorpusSaveClaimer(LostClaimResponse, assume_service_role=True).claim(lost_claim_op)
            assert db.execute(
                "SELECT state FROM public.corpora_corpus_write_ops WHERE id=%s", (lost_claim_op,)
            ).fetchone() == ("executing",)
            assert claimer.claim(lost_claim_op) is None

            class TerminateClaimConnection:
                def __enter__(self):
                    self.db = psycopg.connect(dsn, autocommit=True)
                    return self

                def __exit__(self, exc_type, exc, traceback):
                    self.db.close()
                    return False

                def transaction(self):
                    return self.db.transaction()

                def execute(self, query, params=None):
                    cursor = self.db.execute(query, params)
                    if query.startswith("UPDATE public.corpora_corpus_write_ops"):
                        pid = self.db.execute("SELECT pg_backend_pid()").fetchone()[0]
                        assert db.execute("SELECT pg_terminate_backend(%s)", (pid,)).fetchone()[0]
                    return cursor

            with pytest.raises(CorpusMetadataError, match="reconcile"):
                CorpusSaveClaimer(TerminateClaimConnection, assume_service_role=True).claim(
                    rollback_claim_op
                )
            assert db.execute(
                "SELECT state,worker_claim FROM public.corpora_corpus_write_ops WHERE id=%s",
                (rollback_claim_op,),
            ).fetchone() == ("admitted", None)
            recovered_claim = claimer.claim(rollback_claim_op)
            assert recovered_claim is not None
            lost_candidate = candidate_for(recovered_claim, "d" * 40)
            with pytest.raises(CorpusMetadataError, match="reconcile"):
                CorpusSaveFinalizer(LostClaimResponse, assume_service_role=True).finalize_trusted(
                    lost_candidate
                )
            assert db.execute(
                "SELECT state FROM public.corpora_corpus_write_ops WHERE id=%s",
                (rollback_claim_op,),
            ).fetchone() == ("completed",)
            assert finalizer.finalize_trusted(lost_candidate).sequence == 1
            assert (
                db.execute(
                    "SELECT count(*) FROM public.corpora_corpus_versions WHERE operation_id=%s",
                    (rollback_claim_op,),
                ).fetchone()[0]
                == 1
            )

            finalize_claim = claimer.claim(rollback_finalize_op)
            assert finalize_claim is not None
            rollback_candidate = candidate_for(finalize_claim, "e" * 40)

            class TerminateFinalizeConnection(TerminateClaimConnection):
                def execute(self, query, params=None):
                    cursor = self.db.execute(query, params)
                    if query.startswith("INSERT INTO public.corpora_corpus_versions"):
                        pid = self.db.execute("SELECT pg_backend_pid()").fetchone()[0]
                        assert db.execute("SELECT pg_terminate_backend(%s)", (pid,)).fetchone()[0]
                    return cursor

            with pytest.raises(CorpusMetadataError, match="reconcile"):
                CorpusSaveFinalizer(
                    TerminateFinalizeConnection, assume_service_role=True
                ).finalize_trusted(rollback_candidate)
            assert db.execute(
                "SELECT state FROM public.corpora_corpus_write_ops WHERE id=%s",
                (rollback_finalize_op,),
            ).fetchone() == ("executing",)
            assert (
                db.execute(
                    "SELECT count(*) FROM public.corpora_corpus_versions WHERE operation_id=%s",
                    (rollback_finalize_op,),
                ).fetchone()[0]
                == 0
            )
            assert (
                db.execute(
                    "SELECT count(*) FROM public.corpora_corpus_heads WHERE corpus_id=%s",
                    (rollback_finalize_corpus,),
                ).fetchone()[0]
                == 0
            )
            assert (
                metadata.get(users[2][0]["sub"], rollback_finalize_corpus).state == "provisioning"
            )
            assert finalizer.finalize_trusted(rollback_candidate).sequence == 1
            assert metadata.get(users[2][0]["sub"], identifier).title == "Committed"
        finally:
            for table in (
                "corpora_corpus_heads",
                "corpora_corpus_versions",
                "corpora_corpus_write_ops",
                "corpora_corpus_bootstrap",
                "corpora_corpus_provision_ops",
            ):
                db.execute("DROP TABLE IF EXISTS public." + table + " CASCADE")
            for fn in (
                "corpora_corpus_validate_version",
                "corpora_corpus_validate_head",
                "corpora_corpus_preserve_history",
                "corpora_corpus_validate_operation",
                "corpora_corpus_validate_finalized_version",
                "corpora_corpus_validate_provision",
                "corpora_corpus_validate_bootstrap",
                "corpora_corpus_bootstrap_finalized",
            ):
                db.execute("DROP FUNCTION IF EXISTS public." + fn + "()")
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
