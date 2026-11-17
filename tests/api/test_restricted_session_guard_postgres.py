"""Exercise the session guard and its privileges in disposable PostgreSQL."""

import os
import shlex
import socket
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException

from corpora_api.corpus_hub_provision import VerifiedPrivateRepository
from corpora_api.corpus_metadata import CorpusMetadataConflict, CorpusMetadataNotFound
from corpora_api.corpus_provision_dispatch import CorpusProvisionDispatcher
from corpora_api.corpus_provision_finalize import (
    CorpusProvisionFinalizer,
    TrustedBootstrapAttestation,
)
from corpora_api.corpus_provision_observation import (
    CorpusProvisionHubObserver,
    CorpusProvisionObservationLookup,
    CorpusProvisionReconciler,
)
from corpora_api.corpus_provision_uncertainty import CorpusProvisionUncertaintyRecorder
from corpora_api.session_corpus_metadata import SessionBoundMetadataTransaction
from corpora_api.session_corpus_provision import SessionBoundCorpusProvisionAdmission

pytestmark = pytest.mark.skipif(
    not os.getenv("CORPORA_TEST_POSTGRES_BIN"), reason="requires isolated PostgreSQL binaries"
)


def test_session_lock_without_api_session_write_privilege(monkeypatch):
    psycopg = pytest.importorskip("psycopg")
    binaries = Path(os.environ["CORPORA_TEST_POSTGRES_BIN"])
    migrations = Path(__file__).resolve().parents[2] / "supabase/migrations"
    migration = next(migrations.glob("*_restricted_session_guard.sql"))
    admission_migration = next(migrations.glob("*_restricted_provision_admission.sql"))
    outcome_migration = next(migrations.glob("*_restricted_provision_outcomes.sql"))
    monkeypatch.setenv("HF_CORPUS_ORGANIZATION", "fixture")
    with tempfile.TemporaryDirectory(prefix="corpora-session-guard-", dir="/tmp") as tmp:
        cluster = Path(tmp) / "data"
        init = [str(binaries / "initdb"), "-D", str(cluster), "-U", "cluster_admin", "-A", "trust"]
        if share := os.getenv("CORPORA_TEST_POSTGRES_SHARE"):
            init.extend(["-L", share])
        subprocess.run(init, check=True, capture_output=True, timeout=60)
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        ctl = str(binaries / "pg_ctl")
        options = shlex.join(["-k", tmp, "-p", str(port), "-c", "listen_addresses="])
        subprocess.run(
            [ctl, "-D", str(cluster), "-l", str(Path(tmp) / "pg.log"), "-o", options, "start"],
            check=True,
            capture_output=True,
            timeout=30,
        )
        try:
            dsn = f"host={tmp} port={port} dbname=postgres user=postgres"
            admin_dsn = f"host={tmp} port={port} dbname=postgres user=cluster_admin"
            user, session, foreign, foreign_session = (uuid4() for _ in range(4))
            expires = int(time.time()) + 3600
            with psycopg.connect(admin_dsn, autocommit=True) as admin:
                admin.execute(
                    "CREATE ROLE postgres LOGIN CREATEROLE BYPASSRLS; "
                    "CREATE ROLE anon; CREATE ROLE authenticated; "
                    "CREATE ROLE service_role BYPASSRLS; "
                    "CREATE ROLE supabase_auth_admin; "
                    "GRANT service_role,authenticated TO postgres; "
                    "GRANT CREATE ON DATABASE postgres TO postgres; "
                    "GRANT CREATE ON SCHEMA public TO postgres"
                )
                admin.execute(
                    "CREATE SCHEMA auth AUTHORIZATION supabase_auth_admin; "
                    "CREATE TABLE auth.users (id uuid PRIMARY KEY); "
                    "ALTER TABLE auth.users OWNER TO supabase_auth_admin; "
                    "CREATE TABLE auth.sessions "
                    "(id uuid PRIMARY KEY,user_id uuid,not_after timestamptz); "
                    "ALTER TABLE auth.sessions OWNER TO supabase_auth_admin; "
                    "ALTER TABLE auth.sessions ENABLE ROW LEVEL SECURITY; "
                    "GRANT USAGE ON SCHEMA auth TO postgres; "
                    "GRANT SELECT (id,user_id,not_after), UPDATE (not_after) "
                    "ON auth.sessions TO postgres; "
                    "GRANT REFERENCES ON auth.users TO postgres"
                )
                admin.execute("INSERT INTO auth.users VALUES (%s),(%s)", (user, foreign))
                admin.execute("INSERT INTO auth.sessions VALUES (%s,%s,NULL)", (session, user))
                admin.execute(
                    "INSERT INTO auth.sessions VALUES (%s,%s,NULL)", (foreign_session, foreign)
                )
            with psycopg.connect(dsn, autocommit=True) as db:
                assert db.execute(
                    "SELECT NOT rolsuper AND rolbypassrls FROM pg_roles WHERE rolname='postgres'"
                ).fetchone()[0]
                assert not db.execute(
                    "SELECT has_column_privilege("
                    "'postgres','auth.sessions','not_after','UPDATE WITH GRANT OPTION')"
                ).fetchone()[0]
                db.execute(migration.read_text())
                for suffix in ("corpus_metadata_registry", "corpus_content_versions"):
                    db.execute(next(migrations.glob(f"*_{suffix}.sql")).read_text())
                db.execute(admission_migration.read_text())
                db.execute(outcome_migration.read_text())
                for function in (
                    "lock_verified_session(uuid,uuid,bigint)",
                    "admit_provision(uuid,uuid,bigint,uuid)",
                    "mark_provision_uncertain(uuid,uuid)",
                    "finalize_provision(uuid,uuid,uuid,text,text,text)",
                ):
                    name = "corpora_session_guard." + function
                    assert db.execute(
                        "SELECT has_function_privilege('service_role',%s,'EXECUTE')", (name,)
                    ).fetchone()[0]
                    for role in ("anon", "authenticated"):
                        assert not db.execute(
                            "SELECT has_function_privilege(%s,%s,'EXECUTE')", (role, name)
                        ).fetchone()[0]
                assert not db.execute(
                    "SELECT has_column_privilege("
                    "'service_role','auth.sessions','not_after','UPDATE')"
                ).fetchone()[0]
                assert not db.execute(
                    "SELECT pg_has_role('service_role','postgres','MEMBER')"
                ).fetchone()[0]
                assert db.execute(
                    "SELECT pg_get_userbyid(proowner),prosecdef,proconfig "
                    "FROM pg_proc WHERE oid="
                    "'corpora_session_guard.lock_verified_session(uuid,uuid,bigint)'::regprocedure"
                ).fetchone() == ("postgres", True, ['search_path=""'])
                with db.transaction():
                    db.execute("SET LOCAL ROLE service_role")
                    db.execute(
                        "SELECT corpora_session_guard.lock_verified_session(%s,%s,%s)",
                        (user, session, expires),
                    )
                    with pytest.raises(psycopg.errors.InsufficientPrivilege):
                        with db.transaction():
                            db.execute(
                                "UPDATE auth.sessions SET not_after=NULL WHERE id=%s", (session,)
                            )
                lease = SessionBoundMetadataTransaction(
                    lambda: psycopg.connect(dsn), assume_service_role=True
                )
                timeout_setting = lease.run(
                    {"sub": str(user), "session_id": str(session), "exp": expires},
                    lambda _actor, connection: (
                        connection.execute(
                            "SELECT current_setting('idle_in_transaction_session_timeout')"
                        )
                        .fetchone()[0]
                        .encode()
                    ),
                )
                assert timeout_setting == b"10s"
                admission = SessionBoundCorpusProvisionAdmission(lease)
                corpora = [uuid4() for _ in range(3)]
                for corpus in corpora:
                    db.execute(
                        "INSERT INTO public.corpora_corpus_metadata "
                        "(id,owner_id,title,hf_repo_id,hf_repo_type) "
                        "VALUES (%s,%s,'Test',%s,'dataset')",
                        (corpus, user, f"fixture/corpus-{corpus}"),
                    )
                owner_claims = {"sub": str(user), "session_id": str(session), "exp": expires}
                foreign_claims = {
                    "sub": str(foreign),
                    "session_id": str(foreign_session),
                    "exp": expires,
                }
                with pytest.raises(CorpusMetadataNotFound):
                    admission.claim(foreign_claims, str(corpora[0]))
                claimed = admission.claim(owner_claims, str(corpora[0]))
                assert claimed.actor_id == user and claimed.session_id == session
                with pytest.raises(CorpusMetadataConflict):
                    admission.claim(owner_claims, str(corpora[0]))
                with db.transaction():
                    db.execute("SET LOCAL ROLE service_role")
                    with pytest.raises(psycopg.errors.InsufficientPrivilege):
                        with db.transaction():
                            db.execute(
                                "INSERT INTO public.corpora_corpus_provision_ops "
                                "(id,corpus_id,actor_id,session_id,repo_id,repo_type) "
                                "VALUES (%s,%s,%s,%s,%s,'dataset')",
                                (uuid4(), corpora[0], user, session, "fixture/wrong"),
                            )
                    with pytest.raises(psycopg.errors.InsufficientPrivilege):
                        with db.transaction():
                            db.execute(
                                "UPDATE public.corpora_corpus_provision_ops SET state='uncertain'"
                            )
                    with pytest.raises(psycopg.errors.InsufficientPrivilege):
                        with db.transaction():
                            db.execute(
                                "INSERT INTO public.corpora_corpus_bootstrap "
                                "(corpus_id,operation_id,worker_claim,commit_sha) "
                                "VALUES (%s,%s,%s,%s)",
                                (
                                    claimed.corpus_id,
                                    claimed.operation_id,
                                    claimed.claim_id,
                                    "a" * 40,
                                ),
                            )
                    with pytest.raises(psycopg.errors.InsufficientPrivilege):
                        with db.transaction():
                            db.execute(
                                "INSERT INTO corpora_session_guard.superadmins VALUES (%s)",
                                (foreign,),
                            )
                db.execute("INSERT INTO corpora_session_guard.superadmins VALUES (%s)", (foreign,))
                admin_claim = admission.claim(foreign_claims, str(corpora[1]))
                assert admin_claim.actor_id == foreign and admin_claim.corpus_id == corpora[1]
                finalizer = CorpusProvisionFinalizer(
                    lambda: psycopg.connect(dsn), assume_service_role=True
                )
                candidate = TrustedBootstrapAttestation(
                    claimed.corpus_id,
                    claimed.operation_id,
                    claimed.claim_id,
                    claimed.repo_id,
                    claimed.repo_type,
                    "a" * 40,
                )
                assert finalizer.finalize_trusted(candidate) == "a" * 40
                assert finalizer.finalize_trusted(candidate) == "a" * 40
                assert finalizer.find_finalized_trusted(candidate) == "a" * 40
                wrong_claim = TrustedBootstrapAttestation(
                    candidate.corpus_id,
                    candidate.operation_id,
                    uuid4(),
                    candidate.repo_id,
                    candidate.repo_type,
                    candidate.commit_sha,
                )
                with pytest.raises(CorpusMetadataConflict):
                    finalizer.finalize_trusted(wrong_claim)
                assert finalizer.find_finalized_trusted(wrong_claim) is None
                recorder = CorpusProvisionUncertaintyRecorder(
                    lambda: psycopg.connect(dsn), assume_service_role=True
                )
                recorder.record_trusted(admin_claim.operation_id, admin_claim.claim_id)
                recorder.record_trusted(admin_claim.operation_id, admin_claim.claim_id)
                uncertain_candidate = TrustedBootstrapAttestation(
                    admin_claim.corpus_id,
                    admin_claim.operation_id,
                    admin_claim.claim_id,
                    admin_claim.repo_id,
                    admin_claim.repo_type,
                    "b" * 40,
                )
                with pytest.raises(CorpusMetadataConflict):
                    finalizer.finalize_trusted(uncertain_candidate)
                with pytest.raises(CorpusMetadataConflict):
                    recorder.record_trusted(claimed.operation_id, claimed.claim_id)

                def compete(_):
                    try:
                        return admission.claim(owner_claims, str(corpora[2]))
                    except CorpusMetadataConflict:
                        return None

                with ThreadPoolExecutor(max_workers=2) as pool:
                    claims = list(pool.map(compete, range(2)))
                assert sum(claim is not None for claim in claims) == 1
                dispatched_corpus, dispatch_session = uuid4(), uuid4()
                db.execute(
                    "INSERT INTO public.corpora_corpus_metadata "
                    "(id,owner_id,title,hf_repo_id,hf_repo_type) "
                    "VALUES (%s,%s,'Test',%s,'dataset')",
                    (dispatched_corpus, user, f"fixture/corpus-{dispatched_corpus}"),
                )
                with psycopg.connect(admin_dsn, autocommit=True) as admin:
                    admin.execute(
                        "INSERT INTO auth.sessions VALUES (%s,%s,NULL)", (dispatch_session, user)
                    )
                dispatch_claims = {
                    "sub": str(user),
                    "session_id": str(dispatch_session),
                    "exp": expires,
                }
                repo = f"fixture/corpus-{dispatched_corpus}"
                calls = []

                def worker(actor, corpus, *, timeout):
                    with psycopg.connect(admin_dsn, autocommit=True) as admin:
                        admin.execute("DELETE FROM auth.sessions WHERE id=%s", (dispatch_session,))
                    calls.append("worker")
                    return VerifiedPrivateRepository(
                        dispatched_corpus, 1, repo, "dataset", "c" * 40
                    )

                client = httpx.Client(
                    transport=httpx.MockTransport(
                        lambda _: httpx.Response(
                            200, json={"id": repo, "private": True, "sha": "c" * 40}
                        )
                    )
                )
                observer = CorpusProvisionHubObserver(client, "test-token-placeholder")
                lookup = CorpusProvisionObservationLookup(
                    lambda: psycopg.connect(dsn), assume_service_role=True
                )

                class LostAcknowledgement:
                    def finalize_trusted(self, item):
                        finalizer.finalize_trusted(item)
                        raise OSError("simulated acknowledgement loss")

                    def find_finalized_trusted(self, item):
                        return finalizer.find_finalized_trusted(item)

                dispatcher = CorpusProvisionDispatcher(
                    admission,
                    recorder,
                    CorpusProvisionReconciler(lookup, observer),
                    LostAcknowledgement(),
                    worker=worker,
                )
                dispatched = dispatcher.dispatch_trusted(dispatch_claims, str(dispatched_corpus))
                assert dispatched.bootstrap_sha == "c" * 40 and calls == ["worker"]
                with pytest.raises(HTTPException) as revoked_request:
                    admission.claim(dispatch_claims, str(uuid4()))
                assert revoked_request.value.status_code == 401
                wrong_link = uuid4()
                db.execute(
                    "INSERT INTO public.corpora_corpus_metadata "
                    "(id,owner_id,title,hf_repo_id,hf_repo_type) "
                    "VALUES (%s,%s,'Test','fixture/wrong','dataset')",
                    (wrong_link, user),
                )
                with pytest.raises(HTTPException) as invalid:
                    admission.claim(owner_claims, str(wrong_link))
                assert invalid.value.status_code == 503
                assert (
                    db.execute(
                        "SELECT count(*) FROM public.corpora_corpus_provision_ops "
                        "WHERE corpus_id=%s",
                        (wrong_link,),
                    ).fetchone()[0]
                    == 0
                )
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    with db.transaction():
                        db.execute("SET LOCAL ROLE authenticated")
                        db.execute(
                            "SELECT corpora_session_guard.admit_provision(%s,%s,%s,%s)",
                            (user, session, expires, corpora[0]),
                        )
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    with db.transaction():
                        db.execute("SET LOCAL ROLE authenticated")
                        db.execute(
                            "SELECT corpora_session_guard.lock_verified_session(%s,%s,%s)",
                            (user, session, expires),
                        )
                for owner, token_expiry in ((uuid4(), expires), (user, 1)):
                    with pytest.raises(psycopg.errors.RaiseException, match="AUTH_SESSION_INVALID"):
                        with db.transaction():
                            db.execute("SET LOCAL ROLE service_role")
                            db.execute(
                                "SELECT corpora_session_guard.lock_verified_session(%s,%s,%s)",
                                (owner, session, token_expiry),
                            )
                with psycopg.connect(admin_dsn, autocommit=True) as admin:
                    admin.execute(
                        "UPDATE auth.sessions SET not_after=clock_timestamp()-interval '1 second' "
                        "WHERE id=%s",
                        (session,),
                    )
                with pytest.raises(psycopg.errors.RaiseException, match="AUTH_SESSION_INVALID"):
                    with db.transaction():
                        db.execute("SET LOCAL ROLE service_role")
                        db.execute(
                            "SELECT corpora_session_guard.lock_verified_session(%s,%s,%s)",
                            (user, session, expires),
                        )
                with psycopg.connect(admin_dsn, autocommit=True) as admin:
                    admin.execute("UPDATE auth.sessions SET not_after=NULL WHERE id=%s", (session,))

                locked, release, finished = threading.Event(), threading.Event(), threading.Event()
                failures = []

                def hold_lock():
                    try:
                        with psycopg.connect(dsn) as conn:
                            conn.execute("SET LOCAL ROLE service_role")
                            conn.execute(
                                "SELECT corpora_session_guard.lock_verified_session(%s,%s,%s)",
                                (user, session, expires),
                            )
                            locked.set()
                            assert release.wait(5)
                    except Exception as error:
                        failures.append(error)
                        locked.set()

                def sign_out():
                    try:
                        with psycopg.connect(admin_dsn) as conn:
                            conn.execute("DELETE FROM auth.sessions WHERE id=%s", (session,))
                    except Exception as error:
                        failures.append(error)
                    finally:
                        finished.set()

                holder = threading.Thread(target=hold_lock, daemon=True)
                logout = threading.Thread(target=sign_out, daemon=True)
                holder.start()
                try:
                    assert locked.wait(5) and not failures
                    logout.start()
                    assert not finished.wait(0.25)
                finally:
                    release.set()
                    holder.join(5)
                    if logout.ident is not None:
                        logout.join(5)
                assert finished.is_set() and not failures
                with pytest.raises(psycopg.errors.RaiseException, match="AUTH_SESSION_INVALID"):
                    with db.transaction():
                        db.execute("SET LOCAL ROLE service_role")
                        db.execute(
                            "SELECT corpora_session_guard.lock_verified_session(%s,%s,%s)",
                            (user, session, expires),
                        )
        finally:
            subprocess.run(
                [ctl, "-D", str(cluster), "-m", "immediate", "stop"],
                check=True,
                capture_output=True,
                timeout=30,
            )
