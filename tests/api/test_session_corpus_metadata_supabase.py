"""Opt-in session-bound metadata writes using disposable local Supabase."""

from __future__ import annotations

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlparse
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import HTTPException

from corpora_api.auth import verify_user_token
from corpora_api.corpus_hub_stage import StagedCorpusArchive
from corpora_api.corpus_metadata import (
    CorpusMetadataConflict,
    CorpusMetadataError,
    CorpusMetadataNotFound,
    CorpusMetadataStore,
)
from corpora_api.corpus_provision_finalize import (
    CorpusProvisionFinalizer,
    TrustedBootstrapAttestation,
)
from corpora_api.corpus_publication_claim import CorpusPublicationClaimer
from corpora_api.corpus_save_claim import CorpusSaveClaimer
from corpora_api.corpus_save_finalize import CorpusSaveFinalizer, TrustedCandidateAttestation
from corpora_api.corpus_stage_observation import CorpusStageObservationLookup
from corpora_api.corpus_stage_receipt import CorpusStageReceiptRecorder
from corpora_api.session_corpus_metadata import (
    SessionBoundCorpusMetadata,
    SessionBoundMetadataTransaction,
)
from corpora_api.session_corpus_provision import SessionBoundCorpusProvisionAdmission
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
            # Test-only privilege: production requires a restricted write boundary.
            db.execute(
                "GRANT INSERT (id,corpus_id,actor_id,session_id,repo_id,repo_type), "
                "UPDATE (state,worker_claim) ON public.corpora_corpus_provision_ops TO service_role"
            )
            db.execute(
                "GRANT INSERT (corpus_id,operation_id,worker_claim,commit_sha) "
                "ON public.corpora_corpus_bootstrap TO service_role"
            )
            db.execute("GRANT INSERT ON public.corpora_corpus_staged_inputs TO service_role")
            db.execute("GRANT INSERT ON public.corpora_corpus_publication_claims TO service_role")
            assert not db.execute(
                "SELECT has_column_privilege('service_role',"
                "'public.corpora_corpus_write_ops','id','INSERT')"
            ).fetchone()[0]
            # Test-only DML privilege; this is not a proposed hosted grant.
            db.execute(
                "GRANT INSERT (id,corpus_id,actor_id,session_id,expected_sequence,"
                "expected_parent_sha,input_sha256,input_format) ON public.corpora_corpus_write_ops "
                "TO service_role"
            )
            db.execute(
                "GRANT UPDATE (state,worker_claim) ON public.corpora_corpus_write_ops "
                "TO service_role"
            )
            db.execute(
                "GRANT INSERT (corpus_id,sequence,commit_sha,parent_sha,operation_id,"
                "publication_claim,retained_ref) "
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
                input_format="zip",
            )
            assert str(saved.operation_id) == operation_id and saved.state == "admitted"
            assert saved.input_format == "zip"
            stage_lookup = CorpusStageObservationLookup(
                lambda: psycopg.connect(dsn, autocommit=True), assume_service_role=True
            )
            with pytest.raises(CorpusMetadataConflict):
                stage_lookup.load_trusted(UUID(operation_id))
            with pytest.raises(CorpusMetadataConflict):
                stage_lookup.load_trusted(uuid4())
            assert saved.retained_ref == "corpora-op-" + operation_id
            assert (
                admission.admit(
                    users[0][0],
                    identifier,
                    operation_id,
                    expected_sequence=0,
                    expected_parent_sha=None,
                    input_sha256="a" * 64,
                    input_format="zip",
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
                    input_format="zip",
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
                    input_sha256="a" * 64,
                    input_format="corpus",
                )
            with pytest.raises(CorpusMetadataConflict):
                admission.admit(
                    users[0][0],
                    identifier,
                    operation_id,
                    expected_sequence=0,
                    expected_parent_sha=None,
                    input_sha256="b" * 64,
                    input_format="zip",
                )
            with pytest.raises(CorpusMetadataConflict):
                admission.admit(
                    users[0][0],
                    identifier,
                    str(uuid4()),
                    expected_sequence=0,
                    expected_parent_sha=None,
                    input_sha256="a" * 64,
                    input_format="zip",
                )
            with pytest.raises(CorpusMetadataNotFound):
                admission.admit(
                    users[1][0],
                    identifier,
                    str(uuid4()),
                    expected_sequence=0,
                    expected_parent_sha=None,
                    input_sha256="a" * 64,
                    input_format="zip",
                )
            admin_save = admission.admit(
                users[2][0],
                duplicate_id,
                str(uuid4()),
                expected_sequence=0,
                expected_parent_sha=None,
                input_sha256="a" * 64,
                input_format="zip",
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
                        input_format="zip",
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
            provision_admission = SessionBoundCorpusProvisionAdmission(lease)
            provision_corpus = str(uuid4())
            editor.reserve(users[0][0], provision_corpus, "Provision claim", repo_type="dataset")
            with pytest.raises(CorpusMetadataNotFound):
                provision_admission.claim(users[1][0], provision_corpus)
            provision_barrier = threading.Barrier(2)

            def competing_provision(claims):
                provision_barrier.wait(timeout=10)
                try:
                    return provision_admission.claim(claims, provision_corpus)
                except CorpusMetadataConflict:
                    return None

            with ThreadPoolExecutor(max_workers=2) as pool:
                claims = list(pool.map(competing_provision, (users[0][0], users[2][0])))
            winning_claims = [claim for claim in claims if claim is not None]
            assert len(winning_claims) == 1
            winning_claim = winning_claims[0]
            assert str(winning_claim.corpus_id) == provision_corpus
            assert winning_claim.repo_id == "fixture-org/corpus-" + provision_corpus
            assert db.execute(
                "SELECT state,worker_claim,actor_id,session_id "
                "FROM public.corpora_corpus_provision_ops "
                "WHERE corpus_id=%s",
                (provision_corpus,),
            ).fetchone() == (
                "executing",
                winning_claim.claim_id,
                winning_claim.actor_id,
                winning_claim.session_id,
            )
            with pytest.raises(CorpusMetadataConflict):
                provision_admission.claim(users[0][0], provision_corpus)
            provision_finalizer = CorpusProvisionFinalizer(
                lambda: psycopg.connect(dsn, autocommit=True), assume_service_role=True
            )
            provision_candidate = TrustedBootstrapAttestation(
                winning_claim.corpus_id,
                winning_claim.operation_id,
                winning_claim.claim_id,
                winning_claim.repo_id,
                winning_claim.repo_type,
                "9" * 40,
            )
            for bad in (
                replace(provision_candidate, claim_id=uuid4()),
                replace(provision_candidate, corpus_id=uuid4()),
                replace(provision_candidate, repo_id="fixture-org/wrong"),
            ):
                with pytest.raises(CorpusMetadataConflict):
                    provision_finalizer.finalize_trusted(bad)
            with pytest.raises(CorpusMetadataError):
                provision_finalizer.finalize_trusted(replace(provision_candidate, commit_sha="bad"))

            class FailProvisionCompletion:
                def __enter__(self):
                    self.db = psycopg.connect(dsn, autocommit=True)
                    return self

                def __exit__(self, exc_type, exc, traceback):
                    self.db.close()

                def transaction(self):
                    return self.db.transaction()

                def execute(self, query, *args):
                    if query.startswith("UPDATE public.corpora_corpus_provision_ops"):
                        raise OSError("simulated completion failure")
                    return self.db.execute(query, *args)

            with pytest.raises(CorpusMetadataError, match="reconcile"):
                CorpusProvisionFinalizer(
                    FailProvisionCompletion, assume_service_role=True
                ).finalize_trusted(provision_candidate)
            assert db.execute(
                "SELECT state FROM public.corpora_corpus_provision_ops WHERE id=%s",
                (str(provision_candidate.operation_id),),
            ).fetchone() == ("executing",)
            assert (
                db.execute(
                    "SELECT 1 FROM public.corpora_corpus_bootstrap WHERE corpus_id=%s",
                    (provision_corpus,),
                ).fetchone()
                is None
            )
            finalization_barrier = threading.Barrier(2)

            def finalize_provision(_):
                finalization_barrier.wait(timeout=10)
                return provision_finalizer.finalize_trusted(provision_candidate)

            with ThreadPoolExecutor(max_workers=2) as pool:
                assert list(pool.map(finalize_provision, range(2))) == ["9" * 40] * 2
            with pytest.raises(CorpusMetadataConflict):
                provision_finalizer.finalize_trusted(
                    replace(provision_candidate, commit_sha="8" * 40)
                )
            test_save_op, test_save_claim = str(uuid4()), str(uuid4())
            db.execute(
                "INSERT INTO public.corpora_corpus_write_ops "
                "(id,corpus_id,actor_id,session_id,expected_sequence,"
                "expected_parent_sha,input_sha256,input_format) "
                "VALUES (%s,%s,%s,%s,0,NULL,%s,'zip')",
                (test_save_op, provision_corpus, users[0][0]["sub"], str(uuid4()), "a" * 64),
            )
            db.execute(
                "UPDATE public.corpora_corpus_write_ops SET state='executing',worker_claim=%s "
                "WHERE id=%s",
                (test_save_claim, test_save_op),
            )
            db.execute(
                "INSERT INTO public.corpora_corpus_staged_inputs "
                "(operation_id,corpus_id,worker_claim,repo_id,repo_type,input_format,"
                "input_sha256,archive_size,requested_base_sha,stage_sha,stage_ref,stage_path) "
                "VALUES (%s,%s,%s,%s,'dataset','zip',%s,100,%s,%s,%s,%s)",
                (
                    test_save_op,
                    provision_corpus,
                    test_save_claim,
                    "fixture-org/corpus-" + provision_corpus,
                    "a" * 64,
                    "9" * 40,
                    "8" * 40,
                    "corpora-stage-" + test_save_op,
                    ".corpora-staging/" + test_save_op + "/" + test_save_claim + ".zip",
                ),
            )
            test_publication_claim = str(uuid4())
            db.execute(
                "INSERT INTO public.corpora_corpus_publication_claims "
                "(operation_id,corpus_id,stage_claim,publication_claim) VALUES (%s,%s,%s,%s)",
                (test_save_op, provision_corpus, test_save_claim, test_publication_claim),
            )
            db.execute(
                "UPDATE public.corpora_corpus_metadata SET state='ready' WHERE id=%s",
                (provision_corpus,),
            )
            with db.transaction():
                db.execute(
                    "INSERT INTO public.corpora_corpus_versions "
                    "(corpus_id,sequence,commit_sha,parent_sha,operation_id,"
                    "retained_ref,publication_claim) "
                    "VALUES (%s,1,%s,NULL,%s,%s,%s)",
                    (
                        provision_corpus,
                        "a" * 40,
                        test_save_op,
                        "corpora-op-" + test_save_op,
                        test_publication_claim,
                    ),
                )
                db.execute(
                    "INSERT INTO public.corpora_corpus_heads VALUES (%s,1,%s)",
                    (provision_corpus, "a" * 40),
                )
                db.execute(
                    "UPDATE public.corpora_corpus_write_ops SET state='completed' WHERE id=%s",
                    (test_save_op,),
                )
            assert provision_finalizer.finalize_trusted(provision_candidate) == "9" * 40
            different_corpus = str(uuid4())
            editor.reserve(
                users[0][0], different_corpus, "Different candidates", repo_type="dataset"
            )
            different_claim = provision_admission.claim(users[0][0], different_corpus)
            different_candidate = TrustedBootstrapAttestation(
                different_claim.corpus_id,
                different_claim.operation_id,
                different_claim.claim_id,
                different_claim.repo_id,
                different_claim.repo_type,
                "1" * 40,
            )
            different_barrier = threading.Barrier(2)

            def finalize_different_provision(sha):
                different_barrier.wait(timeout=10)
                try:
                    provision_finalizer.finalize_trusted(
                        replace(different_candidate, commit_sha=sha)
                    )
                    return "completed"
                except CorpusMetadataConflict:
                    return "conflict"

            with ThreadPoolExecutor(max_workers=2) as pool:
                assert sorted(pool.map(finalize_different_provision, ("1" * 40, "2" * 40))) == [
                    "completed",
                    "conflict",
                ]
            admin_provision_corpus = str(uuid4())
            editor.reserve(users[0][0], admin_provision_corpus, "Admin claim", repo_type="dataset")
            admin_claim = provision_admission.claim(users[2][0], admin_provision_corpus)
            assert str(admin_claim.actor_id) == users[2][0]["sub"]
            snapshot_corpus = str(uuid4())
            editor.reserve(users[0][0], snapshot_corpus, "Snapshot", repo_type="dataset")
            mutable_claims = dict(users[0][0])

            class MutatingLease:
                def run(self, claims, callback):
                    def mutate_then_run(actor, connection):
                        mutable_claims["sub"] = users[1][0]["sub"]
                        mutable_claims["session_id"] = users[1][0]["session_id"]
                        return callback(actor, connection)

                    return lease.run(claims, mutate_then_run)

            snapshot = SessionBoundCorpusProvisionAdmission(MutatingLease()).claim(
                mutable_claims, snapshot_corpus
            )
            assert str(snapshot.actor_id) == users[0][0]["sub"]
            assert str(snapshot.session_id) == users[0][0]["session_id"]
            assert db.execute(
                "SELECT actor_id,session_id FROM public.corpora_corpus_provision_ops "
                "WHERE corpus_id=%s",
                (snapshot_corpus,),
            ).fetchone() == (snapshot.actor_id, snapshot.session_id)
            mutable_claims = dict(users[0][0])
            snapshot_save = SessionBoundCorpusSaveAdmission(MutatingLease()).admit(
                mutable_claims,
                snapshot_corpus,
                str(uuid4()),
                expected_sequence=0,
                expected_parent_sha=None,
                input_sha256="a" * 64,
                input_format="zip",
            )
            assert str(snapshot_save.actor_id) == users[0][0]["sub"]
            assert str(snapshot_save.session_id) == users[0][0]["session_id"]
            assert db.execute(
                "SELECT actor_id,session_id FROM public.corpora_corpus_write_ops WHERE id=%s",
                (str(snapshot_save.operation_id),),
            ).fetchone() == (snapshot_save.actor_id, snapshot_save.session_id)
            db.execute(
                "UPDATE public.corpora_corpus_provision_ops SET state='uncertain' WHERE id=%s",
                (str(snapshot.operation_id),),
            )
            uncertain_bootstrap = TrustedBootstrapAttestation(
                snapshot.corpus_id,
                snapshot.operation_id,
                snapshot.claim_id,
                snapshot.repo_id,
                snapshot.repo_type,
                "9" * 40,
            )
            with pytest.raises(CorpusMetadataConflict, match="reconciliation"):
                provision_finalizer.finalize_trusted(uncertain_bootstrap)
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
                    input_format="zip",
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
                    input_format="zip",
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
                input_format="zip",
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
                input_format="zip",
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
                input_format="zip",
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
                        input_format="zip",
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
                    input_format="zip",
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

            # Simulate a trusted worker already dispatched while the session
            # was live. A claim alone is not proof of actual Hub dispatch.
            in_flight_corpus = str(uuid4())
            editor.reserve(users[0][0], in_flight_corpus, "In flight", repo_type="dataset")
            in_flight_claim = provision_admission.claim(users[0][0], in_flight_corpus)
            in_flight_result = TrustedBootstrapAttestation(
                in_flight_claim.corpus_id,
                in_flight_claim.operation_id,
                in_flight_claim.claim_id,
                in_flight_claim.repo_id,
                in_flight_claim.repo_type,
                "a" * 40,
            )

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
                    input_format="zip",
                )
            assert revoked_save.value.status_code == 401
            with pytest.raises(HTTPException) as revoked_provision:
                provision_admission.claim(users[0][0], provision_corpus)
            assert revoked_provision.value.status_code == 401
            # An already-started operation can finish after sign-out; the
            # trusted result here is synthetic and does not establish dispatch.
            assert provision_finalizer.finalize_trusted(in_flight_result) == "a" * 40
            with pytest.raises(HTTPException) as revoked_invalid_provision:
                provision_admission.claim(users[0][0], "invalid")
            assert revoked_invalid_provision.value.status_code == 401
            with pytest.raises(HTTPException) as revoked_invalid_save:
                admission.admit(
                    users[0][0],
                    identifier,
                    "bad-id",
                    expected_sequence=-1,
                    expected_parent_sha=None,
                    input_sha256="bad",
                    input_format="zip",
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
                    input_format="zip",
                )

            publication_claims = {}
            publication_claimer = CorpusPublicationClaimer(
                lambda: psycopg.connect(dsn, autocommit=True), assume_service_role=True
            )

            def candidate_for(claim, sha):
                return TrustedCandidateAttestation(
                    operation_id=claim.operation_id,
                    claim_id=claim.claim_id,
                    publication_claim=publication_claims.get(claim.operation_id, uuid4()),
                    corpus_id=claim.corpus_id,
                    repo_id=claim.repo_id,
                    repo_type=claim.repo_type,
                    parent_sha=claim.expected_parent_sha,
                    hub_parent_sha=claim.hub_parent_sha,
                    commit_sha=sha,
                    input_sha256=claim.input_sha256,
                    retained_ref=claim.retained_ref,
                    input_format=claim.input_format,
                )

            finalizer = CorpusSaveFinalizer(
                lambda: psycopg.connect(dsn, autocommit=True), assume_service_role=True
            )
            stage_recorder = CorpusStageReceiptRecorder(
                lambda: psycopg.connect(dsn, autocommit=True), assume_service_role=True
            )

            def stage_for(claim):
                return StagedCorpusArchive(
                    claim.corpus_id,
                    claim.operation_id,
                    claim.claim_id,
                    claim.repo_id,
                    claim.repo_type,
                    claim.input_format,
                    100,
                    claim.input_sha256,
                    claim.hub_parent_sha,
                    "corpora-stage-" + str(claim.operation_id),
                    "8" * 40,
                    f".corpora-staging/{claim.operation_id}/{claim.claim_id}.{claim.input_format}",
                )

            uncertain_claim = claimer.claim(race_op)
            assert uncertain_claim is not None
            observation_target = stage_lookup.load_trusted(UUID(race_op))
            assert observation_target.operation_id == uncertain_claim.operation_id
            assert observation_target.claim_id == uncertain_claim.claim_id
            assert observation_target.observed_state == "executing"
            assert observation_target.input_sha256 == uncertain_claim.input_sha256
            with monkeypatch.context() as modified:
                modified.setenv("HF_CORPUS_ORGANIZATION", "wrong-org")
                with pytest.raises(CorpusMetadataError):
                    stage_lookup.load_trusted(UUID(race_op))
            uncertain_stage = stage_for(uncertain_claim)
            assert stage_recorder.record_trusted(uncertain_stage) == uncertain_stage
            with pytest.raises(CorpusMetadataConflict):
                stage_lookup.load_trusted(UUID(race_op))
            assert (
                stage_recorder.load_trusted(uncertain_stage.operation_id, uncertain_stage.claim_id)
                == uncertain_stage
            )
            db.execute(
                "UPDATE public.corpora_corpus_write_ops SET state='uncertain' WHERE id=%s",
                (race_op,),
            )
            with pytest.raises(CorpusMetadataConflict):
                stage_recorder.record_trusted(uncertain_stage)
            with pytest.raises(CorpusMetadataConflict):
                stage_recorder.load_trusted(uncertain_stage.operation_id, uncertain_stage.claim_id)
            with pytest.raises(CorpusMetadataConflict, match="reconciliation"):
                finalizer.finalize_trusted(candidate_for(uncertain_claim, "f" * 40))
            assert db.execute(
                "SELECT state FROM public.corpora_corpus_write_ops WHERE id=%s", (race_op,)
            ).fetchone() == ("uncertain",)
            # Synthetic assertions exercise database mechanics, not actual Hub verification.
            first_candidate = candidate_for(winners[0], "b" * 40)
            with pytest.raises(CorpusMetadataConflict, match="publication claim"):
                finalizer.finalize_trusted(first_candidate)
            with pytest.raises(CorpusMetadataConflict):
                publication_claimer.claim(winners[0].operation_id)
            first_stage = stage_for(winners[0])
            with pytest.raises(CorpusMetadataConflict):
                stage_recorder.load_trusted(first_stage.operation_id, first_stage.claim_id)
            direct_insert = (
                "INSERT INTO public.corpora_corpus_staged_inputs "
                "(operation_id,corpus_id,worker_claim,repo_id,repo_type,input_format,"
                "input_sha256,archive_size,requested_base_sha,stage_sha,stage_ref,stage_path) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
            )
            wrong_claim_id = uuid4()
            for bad in (
                replace(first_stage, archive_sha256="f" * 64),
                replace(
                    first_stage,
                    input_format="corpus",
                    stage_path=f".corpora-staging/{first_stage.operation_id}/{first_stage.claim_id}.corpus",
                ),
                replace(first_stage, requested_base_sha="7" * 40),
                replace(
                    first_stage,
                    claim_id=wrong_claim_id,
                    stage_path=f".corpora-staging/{first_stage.operation_id}/{wrong_claim_id}.zip",
                ),
                replace(first_stage, stage_ref="incorrect"),
                replace(first_stage, stage_path="incorrect"),
            ):
                with pytest.raises(psycopg.Error):
                    db.execute(
                        direct_insert,
                        (
                            str(bad.operation_id),
                            str(bad.corpus_id),
                            str(bad.claim_id),
                            bad.repo_id,
                            bad.repo_type,
                            bad.input_format,
                            bad.archive_sha256,
                            bad.archive_size,
                            bad.requested_base_sha,
                            bad.stage_sha,
                            bad.stage_ref,
                            bad.stage_path,
                        ),
                    )

            class FailStageTransaction:
                def __enter__(self):
                    self.db = psycopg.connect(dsn, autocommit=True)
                    return self

                def __exit__(self, exc_type, exc, traceback):
                    self.db.close()

                def transaction(self):
                    return self.db.transaction()

                def execute(self, query, *args):
                    result = self.db.execute(query, *args)
                    if query.startswith("INSERT INTO public.corpora_corpus_staged_inputs"):
                        raise OSError("simulated connection loss")
                    return result

            with pytest.raises(CorpusMetadataError, match="reconcile"):
                CorpusStageReceiptRecorder(
                    FailStageTransaction, assume_service_role=True
                ).record_trusted(first_stage)
            assert (
                db.execute(
                    "SELECT 1 FROM public.corpora_corpus_staged_inputs WHERE operation_id=%s",
                    (str(first_stage.operation_id),),
                ).fetchone()
                is None
            )
            for bad in (
                replace(first_stage, archive_sha256="f" * 64),
                replace(first_stage, requested_base_sha="7" * 40),
                replace(first_stage, repo_id="fixture-org/wrong"),
            ):
                with pytest.raises(CorpusMetadataConflict):
                    stage_recorder.record_trusted(bad)
            stage_barrier = threading.Barrier(2)

            def record_same_stage(_):
                stage_barrier.wait(timeout=10)
                return stage_recorder.record_trusted(first_stage)

            with ThreadPoolExecutor(max_workers=2) as pool:
                assert list(pool.map(record_same_stage, range(2))) == [first_stage, first_stage]
            assert stage_recorder.record_trusted(first_stage) == first_stage
            publication_barrier = threading.Barrier(2)

            def claim_publication(_):
                publication_barrier.wait(timeout=10)
                return publication_claimer.claim(winners[0].operation_id)

            with ThreadPoolExecutor(max_workers=2) as pool:
                publication_results = list(pool.map(claim_publication, range(2)))
            publication_winners = [value for value in publication_results if value is not None]
            assert len(publication_winners) == 1
            publication = publication_winners[0]
            assert publication.stage == first_stage
            assert publication.publication_claim != first_stage.claim_id
            assert publication_claimer.claim(winners[0].operation_id) is None
            with pytest.raises(psycopg.errors.RaiseException):
                db.execute(
                    "DELETE FROM public.corpora_corpus_publication_claims WHERE operation_id=%s",
                    (str(winners[0].operation_id),),
                )
            with pytest.raises(psycopg.errors.RaiseException):
                db.execute(
                    "UPDATE public.corpora_corpus_publication_claims "
                    "SET publication_claim=%s WHERE operation_id=%s",
                    (str(uuid4()), str(winners[0].operation_id)),
                )
            publication_claims[winners[0].operation_id] = publication.publication_claim
            first_candidate = candidate_for(winners[0], "b" * 40)
            with pytest.raises(CorpusMetadataConflict):
                finalizer.finalize_trusted(replace(first_candidate, publication_claim=uuid4()))
            with pytest.raises(CorpusMetadataConflict, match="Staging commit"):
                finalizer.finalize_trusted(
                    replace(first_candidate, commit_sha=first_stage.stage_sha)
                )
            with pytest.raises(CorpusMetadataConflict):
                finalizer.finalize_trusted(
                    replace(first_candidate, publication_claim=first_stage.claim_id)
                )
            assert (
                stage_recorder.load_trusted(first_stage.operation_id, first_stage.claim_id)
                == first_stage
            )
            with pytest.raises(CorpusMetadataConflict):
                stage_recorder.load_trusted(first_stage.operation_id, uuid4())
            with pytest.raises(CorpusMetadataConflict):
                stage_recorder.record_trusted(replace(first_stage, stage_sha="7" * 40))
            with pytest.raises(psycopg.errors.RaiseException):
                db.execute(
                    "DELETE FROM public.corpora_corpus_staged_inputs WHERE operation_id=%s",
                    (str(first_stage.operation_id),),
                )
            with pytest.raises(CorpusMetadataConflict):
                finalizer.finalize_trusted(replace(first_candidate, claim_id=uuid4()))
            with pytest.raises(CorpusMetadataConflict):
                finalizer.finalize_trusted(replace(first_candidate, input_sha256="c" * 64))
            with pytest.raises(CorpusMetadataConflict):
                finalizer.finalize_trusted(replace(first_candidate, input_format="corpus"))
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
                input_format="zip",
            )
            next_claim = claimer.claim(next_op)
            assert next_claim is not None
            stage_recorder.record_trusted(stage_for(next_claim))
            next_publication = publication_claimer.claim(next_claim.operation_id)
            assert next_publication is not None
            publication_claims[next_claim.operation_id] = next_publication.publication_claim
            assert next_claim.hub_parent_sha == "b" * 40
            next_version = finalizer.finalize_trusted(candidate_for(next_claim, "c" * 40))
            assert next_version.sequence == 2
            assert finalizer.finalize_trusted(first_candidate) == versions[0]
            assert stage_recorder.record_trusted(first_stage) == first_stage
            with pytest.raises(CorpusMetadataConflict):
                stage_recorder.load_trusted(first_stage.operation_id, first_stage.claim_id)
            with pytest.raises(CorpusMetadataConflict):
                stage_lookup.load_trusted(first_stage.operation_id)

            competing_op = str(uuid4())
            admission.admit(
                users[2][0],
                identifier,
                competing_op,
                expected_sequence=2,
                expected_parent_sha="c" * 40,
                input_sha256="a" * 64,
                input_format="zip",
            )
            competing_claim = claimer.claim(competing_op)
            assert competing_claim is not None
            stage_recorder.record_trusted(stage_for(competing_claim))
            competing_publication = publication_claimer.claim(competing_claim.operation_id)
            assert competing_publication is not None
            publication_claims[competing_claim.operation_id] = (
                competing_publication.publication_claim
            )
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

            lost_pub_corpus, lost_pub_op = str(uuid4()), str(uuid4())
            editor.reserve(users[2][0], lost_pub_corpus, "Lost publication", repo_type="dataset")
            seed_provision, seed_claim = str(uuid4()), str(uuid4())
            db.execute(
                "INSERT INTO public.corpora_corpus_provision_ops "
                "(id,corpus_id,actor_id,session_id,repo_id,repo_type) "
                "SELECT %s,id,owner_id,%s,hf_repo_id,hf_repo_type "
                "FROM public.corpora_corpus_metadata WHERE id=%s",
                (seed_provision, str(uuid4()), lost_pub_corpus),
            )
            db.execute(
                "UPDATE public.corpora_corpus_provision_ops SET state='executing',worker_claim=%s "
                "WHERE id=%s",
                (seed_claim, seed_provision),
            )
            with db.transaction():
                db.execute(
                    "INSERT INTO public.corpora_corpus_bootstrap "
                    "(corpus_id,operation_id,worker_claim,commit_sha) VALUES (%s,%s,%s,%s)",
                    (lost_pub_corpus, seed_provision, seed_claim, "9" * 40),
                )
                db.execute(
                    "UPDATE public.corpora_corpus_provision_ops SET state='completed' WHERE id=%s",
                    (seed_provision,),
                )
            admission.admit(
                users[2][0],
                lost_pub_corpus,
                lost_pub_op,
                expected_sequence=0,
                expected_parent_sha=None,
                input_sha256="a" * 64,
                input_format="zip",
            )
            lost_pub_stage_claim = claimer.claim(lost_pub_op)
            assert lost_pub_stage_claim is not None
            stage_recorder.record_trusted(stage_for(lost_pub_stage_claim))

            class FailPublicationTransaction:
                def __enter__(self):
                    self.db = psycopg.connect(dsn, autocommit=True)
                    return self

                def __exit__(self, exc_type, exc, traceback):
                    self.db.close()

                def transaction(self):
                    return self.db.transaction()

                def execute(self, query, *args):
                    result = self.db.execute(query, *args)
                    if query.startswith("INSERT INTO public.corpora_corpus_publication_claims"):
                        raise OSError("simulated connection loss")
                    return result

            with pytest.raises(CorpusMetadataError, match="reconcile"):
                CorpusPublicationClaimer(
                    FailPublicationTransaction, assume_service_role=True
                ).claim(UUID(lost_pub_op))
            assert (
                db.execute(
                    "SELECT 1 FROM public.corpora_corpus_publication_claims WHERE operation_id=%s",
                    (lost_pub_op,),
                ).fetchone()
                is None
            )
            with pytest.raises(CorpusMetadataError, match="reconcile"):
                CorpusPublicationClaimer(LostClaimResponse, assume_service_role=True).claim(
                    UUID(lost_pub_op)
                )
            assert (
                db.execute(
                    "SELECT 1 FROM public.corpora_corpus_publication_claims WHERE operation_id=%s",
                    (lost_pub_op,),
                ).fetchone()
                is not None
            )
            assert publication_claimer.claim(UUID(lost_pub_op)) is None

            lost_pub_token = db.execute(
                "SELECT publication_claim FROM public.corpora_corpus_publication_claims "
                "WHERE operation_id=%s",
                (lost_pub_op,),
            ).fetchone()[0]
            locked, release, writer_done = threading.Event(), threading.Event(), threading.Event()
            race_results = []

            def mark_uncertain():
                with psycopg.connect(dsn, autocommit=True) as connection:
                    with connection.transaction():
                        connection.execute(
                            "SELECT 1 FROM public.corpora_corpus_metadata WHERE id=%s FOR UPDATE",
                            (lost_pub_corpus,),
                        )
                        connection.execute(
                            "UPDATE public.corpora_corpus_write_ops SET state='uncertain' "
                            "WHERE id=%s",
                            (lost_pub_op,),
                        )
                        locked.set()
                        assert release.wait(10)

            def try_uncertain_version():
                try:
                    with psycopg.connect(dsn, autocommit=True) as connection:
                        connection.execute(
                            "INSERT INTO public.corpora_corpus_versions "
                            "(corpus_id,sequence,commit_sha,parent_sha,operation_id,"
                            "retained_ref,publication_claim) VALUES (%s,1,%s,NULL,%s,%s,%s)",
                            (
                                lost_pub_corpus,
                                "b" * 40,
                                lost_pub_op,
                                "corpora-op-" + lost_pub_op,
                                lost_pub_token,
                            ),
                        )
                    race_results.append("inserted")
                except psycopg.errors.RaiseException as error:
                    race_results.append(error.diag.message_primary)
                finally:
                    writer_done.set()

            changing_thread = threading.Thread(target=mark_uncertain, daemon=True)
            writing_thread = threading.Thread(target=try_uncertain_version, daemon=True)
            changing_thread.start()
            try:
                assert locked.wait(10)
                writing_thread.start()
                assert not writer_done.wait(0.3)
            finally:
                release.set()
                changing_thread.join(timeout=10)
                if writing_thread.ident is not None:
                    writing_thread.join(timeout=10)
            assert not changing_thread.is_alive() and not writing_thread.is_alive()
            assert race_results == ["CORPUS_VERSION_OPERATION_INVALID"]

            lost_bootstrap = TrustedBootstrapAttestation(
                admin_claim.corpus_id,
                admin_claim.operation_id,
                admin_claim.claim_id,
                admin_claim.repo_id,
                admin_claim.repo_type,
                "9" * 40,
            )
            with pytest.raises(CorpusMetadataError, match="reconcile"):
                CorpusProvisionFinalizer(
                    LostClaimResponse, assume_service_role=True
                ).finalize_trusted(lost_bootstrap)
            assert provision_finalizer.finalize_trusted(lost_bootstrap) == "9" * 40

            lost_provision_corpus = str(uuid4())
            editor.reserve(
                users[2][0], lost_provision_corpus, "Lost provision", repo_type="dataset"
            )
            lost_provision = SessionBoundCorpusProvisionAdmission(
                SessionBoundMetadataTransaction(LostClaimResponse, assume_service_role=True)
            )
            with pytest.raises(HTTPException) as lost_provision_error:
                lost_provision.claim(users[2][0], lost_provision_corpus)
            assert lost_provision_error.value.status_code == 503
            assert db.execute(
                "SELECT state FROM public.corpora_corpus_provision_ops WHERE corpus_id=%s",
                (lost_provision_corpus,),
            ).fetchone() == ("executing",)
            with pytest.raises(CorpusMetadataConflict):
                provision_admission.claim(users[2][0], lost_provision_corpus)

            with pytest.raises(CorpusMetadataError, match="reconcile"):
                CorpusSaveClaimer(LostClaimResponse, assume_service_role=True).claim(lost_claim_op)
            assert db.execute(
                "SELECT state FROM public.corpora_corpus_write_ops WHERE id=%s", (lost_claim_op,)
            ).fetchone() == ("executing",)
            assert claimer.claim(lost_claim_op) is None
            assert stage_lookup.load_trusted(UUID(lost_claim_op)).observed_state == "executing"
            db.execute(
                "UPDATE public.corpora_corpus_write_ops SET state='uncertain' WHERE id=%s",
                (lost_claim_op,),
            )
            assert stage_lookup.load_trusted(UUID(lost_claim_op)).observed_state == "uncertain"
            db.execute(
                "UPDATE public.corpora_corpus_write_ops SET state='failed' WHERE id=%s",
                (lost_claim_op,),
            )
            with pytest.raises(CorpusMetadataConflict):
                stage_lookup.load_trusted(UUID(lost_claim_op))

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
            recovered_stage = stage_for(recovered_claim)
            with pytest.raises(CorpusMetadataError, match="reconcile"):
                CorpusStageReceiptRecorder(
                    LostClaimResponse, assume_service_role=True
                ).record_trusted(recovered_stage)
            assert stage_recorder.record_trusted(recovered_stage) == recovered_stage
            recovered_publication = publication_claimer.claim(recovered_claim.operation_id)
            assert recovered_publication is not None
            publication_claims[recovered_claim.operation_id] = (
                recovered_publication.publication_claim
            )
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
            stage_recorder.record_trusted(stage_for(finalize_claim))
            finalize_publication = publication_claimer.claim(finalize_claim.operation_id)
            assert finalize_publication is not None
            publication_claims[finalize_claim.operation_id] = finalize_publication.publication_claim
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
                "corpora_corpus_publication_claims",
                "corpora_corpus_staged_inputs",
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
                "corpora_corpus_validate_staged_input",
                "corpora_corpus_validate_publication_claim",
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
